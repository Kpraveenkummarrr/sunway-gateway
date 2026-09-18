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
    prepare_tts_for_playback,
    read_wav_info,
    _read_pcm,
)

SUPPORTED_G711_CODECS = ("ulaw", "alaw")


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
    }
