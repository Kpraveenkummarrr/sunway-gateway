"""Objective diagnostics for the narrow-band telephony voice path.

These helpers deliberately do not assign a subjective quality score.  They
produce comparable WAV files and measurements so a Hindi-speaking listener
can identify the stage where intelligibility or naturalness degrades.
"""

from __future__ import annotations

import audioop
import hashlib
import io
import wave
import math
from dataclasses import asdict

import numpy as np

from app.services.audio import (
    ASTERISK_SAMPLE_RATE,
    AudioFormatError,
    measure_levels,
    normalize_for_asterisk_playback,
    prepare_tts_clip,
    prepare_tts_for_playback,
    read_wav_info,
    _read_pcm,
)

SUPPORTED_G711_CODECS = ("ulaw", "alaw")


def roughness_metrics(samples: np.ndarray, rate: int) -> dict[str, float | int | None]:
    """Objective roughness measures of voiced speech: frame-to-frame F0 change
    (jitter) and harmonic-to-noise ratio, from a normalised autocorrelation on
    30 ms frames every 10 ms. These are diagnostics for comparing two renderings
    of the SAME utterance (does a processing stage make it rougher?), not a
    naturalness score - only a listener can give that.

    Measured on genuine speech (Windows SAPI, English, 48 kHz): the previous
    chain's 1.15x WSOLA stage moved median F0 jitter 1.83% -> 1.94% and mean HNR
    by -0.2 dB, i.e. barely; that is why tempo was not blamed for the robotic
    sound without listening evidence."""
    frame, hop = int(0.030 * rate), int(0.010 * rate)
    lo, hi = int(rate / 400), int(rate / 70)
    window = np.hanning(frame)
    f0s: list[float] = []
    hnrs: list[float] = []
    for start in range(0, max(0, len(samples) - frame - hi), hop):
        segment = samples[start : start + frame + hi]
        head = segment[:frame] * window
        if np.sqrt(np.mean(head**2)) < 0.01:
            f0s.append(np.nan)
            hnrs.append(np.nan)
            continue
        energy = float(np.dot(head, head))
        best, best_lag = -1.0, lo
        for lag in range(lo, hi):
            tail = segment[lag : lag + frame] * window
            r = float(np.dot(head, tail)) / math.sqrt(energy * float(np.dot(tail, tail)) + 1e-12)
            if r > best:
                best, best_lag = r, lag
        if best < 0.55:
            f0s.append(np.nan)
            hnrs.append(np.nan)
            continue
        f0s.append(rate / best_lag)
        peak = min(best, 0.999)
        hnrs.append(10 * math.log10(peak / (1 - peak)))
    f0 = np.array(f0s)
    ok = ~np.isnan(f0)
    pairs = ok[:-1] & ok[1:]
    if int(ok.sum()) < 3 or not pairs.any():
        return {"voiced_frames": int(ok.sum()), "f0_jitter_median_pct": None, "f0_jitter_p90_pct": None, "hnr_mean_db": None}
    change = np.abs(np.log(f0[1:][pairs] / f0[:-1][pairs]))
    return {
        "voiced_frames": int(ok.sum()),
        "f0_jitter_median_pct": float(np.median(change)) * 100,
        "f0_jitter_p90_pct": float(np.percentile(change, 90)) * 100,
        "hnr_mean_db": float(np.nanmean(np.array(hnrs))),
    }


def describe_wav(wav_bytes: bytes, *, word_count: int = 0) -> dict[str, int | float | str | None]:
    """Return safe, objective WAV measurements suitable for a JSON report."""
    info = read_wav_info(wav_bytes)
    levels = measure_levels(wav_bytes)
    report: dict[str, int | float | str | None] = {
        "sha256": hashlib.sha256(wav_bytes).hexdigest(),
        "sample_rate_hz": info.sample_rate,
        "channels": info.channels,
        "sample_width_bits": info.sample_width * 8,
        **asdict(levels),
        "crest_factor_db": levels.peak_dbfs - levels.rms_dbfs,
    }
    if word_count > 0 and info.duration_seconds > 0:
        report["words_per_minute"] = word_count / info.duration_seconds * 60.0
    samples, rate = _read_pcm(wav_bytes)
    frame = max(1, int(0.04 * rate))
    centroids, pitches = [], []
    for offset in range(0, max(0, len(samples) - frame + 1), frame):
        chunk = samples[offset:offset + frame]
        chunk = chunk - chunk.mean()
        if np.sqrt(np.mean(chunk ** 2)) < 0.003:
            continue
        spectrum = np.abs(np.fft.rfft(chunk * np.hanning(frame)))
        frequencies = np.fft.rfftfreq(frame, 1 / rate)
        if spectrum.sum() > 0:
            centroids.append(float(np.dot(frequencies, spectrum) / spectrum.sum()))
        # FFT autocorrelation: diagnostic F0 estimate, not a naturalness score.
        fft = np.fft.rfft(chunk, n=2 * frame)
        correlation = np.fft.irfft(fft * fft.conj())[:frame]
        lo, hi = max(1, int(rate / 400)), min(frame, int(rate / 70))
        if hi > lo and correlation[0] > 0:
            lag = lo + int(np.argmax(correlation[lo:hi]))
            if correlation[lag] / correlation[0] >= 0.6:
                pitches.append(rate / lag)
    report["spectral_centroid_hz"] = float(np.median(centroids)) if centroids else None
    report["estimated_f0_hz"] = float(np.median(pitches)) if pitches else None
    report["f0_voiced_frames"] = len(pitches)
    report["noise_floor_method"] = "10th percentile frame RMS; not isolated background noise"
    full_spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2 if len(samples) else np.zeros(1)
    if full_spectrum.sum() > 0:
        cumulative = np.cumsum(full_spectrum) / full_spectrum.sum()
        report["spectral_rolloff_95_hz"] = float(np.fft.rfftfreq(len(samples), 1 / rate)[int(np.searchsorted(cumulative, 0.95))])
    else:
        report["spectral_rolloff_95_hz"] = None
    report.update(roughness_metrics(samples, rate))
    return report


def g711_roundtrip(wav_bytes: bytes, *, codec: str) -> bytes:
    """Encode/decode an 8 kHz PCM WAV through G.711 for an offline preview.

    This previews codec quantisation only.  It is not evidence of the codec
    actually negotiated by Asterisk and does not simulate RTP loss, jitter,
    the SMG4008, a mobile network, or a handset speaker.
    """
    if codec not in SUPPORTED_G711_CODECS:
        raise ValueError(f"codec must be one of {SUPPORTED_G711_CODECS}")
    info = read_wav_info(wav_bytes)
    if (info.sample_rate, info.channels, info.sample_width) != (ASTERISK_SAMPLE_RATE, 1, 2):
        raise AudioFormatError("G.711 preview requires 8kHz mono 16-bit PCM WAV")

    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        frames = source.readframes(source.getnframes())
    if codec == "ulaw":
        decoded = audioop.ulaw2lin(audioop.lin2ulaw(frames, 2), 2)
    else:
        decoded = audioop.alaw2lin(audioop.lin2alaw(frames, 2), 2)

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(ASTERISK_SAMPLE_RATE)
        target.writeframes(decoded)
    return output.getvalue()


def build_voice_ab_variants(
    source_wav: bytes,
    *,
    current_speed: float,
    candidate_speed: float = 1.0,
) -> dict[str, bytes]:
    """Create a controlled stage-A/B/G.711 comparison from one TTS result.

    Both processed variants come from the exact same provider response.  The
    only difference between ``current`` and ``candidate`` is local tempo, so
    a listener can attribute a preference without model/output randomness.
    """
    if not all(math.isfinite(s) and 0.8 <= s <= 1.6 for s in (current_speed, candidate_speed)):
        raise ValueError("speeds must be finite and between 0.8 and 1.6")

    resampled = normalize_for_asterisk_playback(source_wav)
    current = prepare_tts_for_playback(source_wav, speed=current_speed)
    candidate = prepare_tts_for_playback(source_wav, speed=candidate_speed)
    # The two arms of the noisy/robotic-voice question, from the same source:
    #   A = the chain production ran before (legacy, 1.15x WSOLA tempo)
    #   B = what production runs now (clean chain, speed 1.0: no tempo processing at all)
    previous_production = prepare_tts_clip(source_wav, speed=1.15, profile="legacy").audio
    native_clean = prepare_tts_clip(source_wav, speed=1.0, profile="clean").audio
    return {
        "A_original_bhashini.wav": source_wav,
        "B0_resample_only_8k.wav": resampled,
        "B1_current_8k.wav": current,
        "B2_candidate_native_cadence_8k.wav": candidate,
        "B3_tempo_1_10_8k.wav": prepare_tts_for_playback(source_wav, speed=1.10),
        "B4_tempo_1_15_8k.wav": prepare_tts_for_playback(source_wav, speed=1.15),
        "C1_current_ulaw_preview.wav": g711_roundtrip(current, codec="ulaw"),
        "C2_candidate_ulaw_preview.wav": g711_roundtrip(candidate, codec="ulaw"),
        "C3_current_alaw_preview.wav": g711_roundtrip(current, codec="alaw"),
        "C4_candidate_alaw_preview.wav": g711_roundtrip(candidate, codec="alaw"),
        "D1_A_previous_production_legacy_1_15x_8k.wav": previous_production,
        "D2_B_native_clean_1_0x_8k.wav": native_clean,
        "D3_clean_chain_at_current_speed_8k.wav": prepare_tts_clip(source_wav, speed=current_speed, profile="clean").audio,
        "D4_A_previous_production_ulaw_preview.wav": g711_roundtrip(previous_production, codec="ulaw"),
        "D5_B_native_clean_ulaw_preview.wav": g711_roundtrip(native_clean, codec="ulaw"),
    }
