"""Audio format, quality and timing processing for the telephone AI pipeline.

Format map (verified empirically against the real Asterisk install —
see docs/asterisk.md#audio-formats):

  Asterisk ARI recordings (caller audio) -> WAV, 8kHz, mono, 16-bit
    signed PCM. This is what `channel.record(format="wav")` produces for
    a call on a ulaw/alaw channel.

  STT input -> OpenAI Whisper accepts audio at any rate (it resamples
    server-side), so it's passed through unchanged. Bhashini's conformer
    ASR is sent 16kHz mono 16-bit PCM with leading/trailing silence trimmed
    (`prepare_for_asr`).

  TTS output -> WAV at the vendor's native rate: OpenAI (response_format
    ="wav") is 24kHz; Bhashini returned 48kHz in live testing. Asterisk
    plays 8kHz/16kHz only, and the GSM leg is 8kHz, so playback audio is
    always produced at 8kHz (`prepare_tts_for_playback`).

Resampling uses a windowed-sinc low-pass filter. The previous
`audioop.ratecv` path did no anti-alias filtering: downsampling Bhashini's
48kHz speech folded everything above 4kHz (sibilants) straight into the
telephone band at full strength — measured 0.0 dB alias level — which
callers hear as hiss/harshness.
"""

import audioop
import io
import math
import struct
import time
import wave
from dataclasses import dataclass

import numpy as np

ASTERISK_SAMPLE_RATE = 8000
ASTERISK_PLAYABLE_RATES = (8000, 16000)
ASR_SAMPLE_RATE = 16000
MIN_TURN_DURATION_SECONDS = 0.3
DEFAULT_SILENCE_RMS_THRESHOLD = 150  # on a 16-bit PCM scale (0-32767)

_FRAME_SECONDS = 0.010
_PLAYBACK_PEAK_DBFS = -3.0
_PLAYBACK_MAX_GAIN_DB = 9.0
_SILENT_PEAK_DBFS = -50.0


class AudioFormatError(Exception):
    """Raised when audio bytes aren't a readable/convertible WAV file."""


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    duration_seconds: float


@dataclass(frozen=True)
class AudioLevels:
    duration_seconds: float
    peak_dbfs: float
    rms_dbfs: float
    noise_floor_dbfs: float
    clipped_ratio: float
    leading_silence_seconds: float
    trailing_silence_seconds: float


def read_wav_info(wav_bytes: bytes) -> WavInfo:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return WavInfo(
                sample_rate=rate,
                channels=wf.getnchannels(),
                sample_width=wf.getsampwidth(),
                duration_seconds=(frames / rate) if rate else 0.0,
            )
    except (wave.Error, EOFError) as exc:
        raise AudioFormatError(f"Not a readable WAV file: {exc}") from exc


def is_asterisk_playable(info: WavInfo) -> bool:
    return info.sample_width == 2 and info.channels == 1 and info.sample_rate in ASTERISK_PLAYABLE_RATES


# ---- PCM <-> float ----


def _read_pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Any 8/16/24/32-bit PCM WAV -> (mono float64 samples in [-1, 1), rate)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
    except (wave.Error, EOFError) as exc:
        raise AudioFormatError(f"Not a readable WAV file: {exc}") from exc

    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw[: len(raw) - len(raw) % 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        samples = np.where(value >= 1 << 23, value - (1 << 24), value).astype(np.float64) / float(1 << 23)
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    else:
        raise AudioFormatError(f"Unsupported sample width: {width} bytes")

    if channels < 1:
        raise AudioFormatError(f"Unsupported channel count: {channels}")
    if channels > 1:
        samples = samples[: len(samples) - len(samples) % channels].reshape(-1, channels).mean(axis=1)
    if not rate:
        raise AudioFormatError("WAV has a zero sample rate")
    return samples, rate


def _write_pcm(samples: np.ndarray, rate: int) -> bytes:
    pcm = np.clip(np.round(samples * 32767.0), -32768, 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return out.getvalue()


# ---- DSP ----


def _lowpass(samples: np.ndarray, rate: int, cutoff_hz: float, transition_hz: float) -> np.ndarray:
    taps = int(math.ceil(3.3 * rate / transition_hz)) | 1  # Hamming window, ~53 dB stopband
    n = np.arange(taps) - (taps - 1) / 2
    kernel = np.sinc(2.0 * cutoff_hz / rate * n) * np.hamming(taps)
    kernel /= kernel.sum()
    filtered = np.convolve(samples, kernel, mode="full")
    start = (taps - 1) // 2
    return filtered[start : start + len(samples)]


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Band-limited resampling: low-pass below the lower Nyquist frequency
    (before decimating, or after interpolating), so no energy aliases into
    the output band. Passband reaches 85% of that Nyquist (3.4kHz at 8kHz —
    the full telephone band)."""
    if source_rate == target_rate or len(samples) == 0:
        return samples
    nyquist = min(source_rate, target_rate) / 2.0
    cutoff, transition = 0.85 * nyquist, 0.15 * nyquist
    if target_rate < source_rate:
        samples = _lowpass(samples, source_rate, cutoff, transition)
    n_out = int(round(len(samples) * target_rate / source_rate))
    positions = np.arange(n_out) * (source_rate / target_rate)
    resampled = np.interp(positions, np.arange(len(samples)), samples)
    if target_rate > source_rate:
        resampled = _lowpass(resampled, target_rate, cutoff, transition)
    return resampled


def _frame_rms(samples: np.ndarray, rate: int) -> tuple[np.ndarray, int]:
    frame = max(1, int(_FRAME_SECONDS * rate))
    count = len(samples) // frame
    if count == 0:
        return np.zeros(0), frame
    frames = samples[: count * frame].reshape(count, frame)
    return np.sqrt(np.mean(frames**2, axis=1)), frame


def _voiced_bounds(samples: np.ndarray, rate: int, *, db_below_peak: float, floor_dbfs: float) -> tuple[int, int] | None:
    """(start, end) sample bounds of the non-silent region, or None if silent."""
    rms, frame = _frame_rms(samples, rate)
    if len(rms) == 0 or rms.max() <= 0:
        return None
    threshold = max(rms.max() * 10 ** (-db_below_peak / 20), 10 ** (floor_dbfs / 20))
    voiced = np.nonzero(rms >= threshold)[0]
    if len(voiced) == 0:
        return None
    return int(voiced[0] * frame), int(min(len(samples), (voiced[-1] + 1) * frame))


def trim_silence(
    samples: np.ndarray,
    rate: int,
    *,
    db_below_peak: float = 40.0,
    floor_dbfs: float = -55.0,
    pad_seconds: float = 0.06,
) -> np.ndarray:
    """Removes leading/trailing silence, keeping `pad_seconds` on each side.
    Returns the input unchanged if no frame rises above the threshold."""
    bounds = _voiced_bounds(samples, rate, db_below_peak=db_below_peak, floor_dbfs=floor_dbfs)
    if bounds is None:
        return samples
    pad = int(pad_seconds * rate)
    return samples[max(0, bounds[0] - pad) : min(len(samples), bounds[1] + pad)]


def time_stretch(samples: np.ndarray, rate: int, speed: float) -> np.ndarray:
    """Pitch-preserving tempo change (WSOLA): `speed` 1.15 plays 15% faster
    at the same pitch. Output duration is len(samples) / speed."""
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed must be positive")
    frame = int(0.030 * rate) & ~1
    if abs(speed - 1.0) < 0.01 or len(samples) < 3 * frame:
        return samples
    hop = frame // 2
    tolerance = int(0.008 * rate)
    window = np.hanning(frame)

    n_frames = int((len(samples) - frame - tolerance) / (hop * speed))
    out = np.zeros(n_frames * hop + frame)
    weight = np.zeros_like(out)
    previous = 0
    written = 0
    for k in range(max(n_frames, 0)):
        if k == 0:
            best = 0
        else:
            # Pick the analysis segment near the target position whose shape
            # best continues the previously placed segment (no phase clicks).
            natural = previous + hop
            template = samples[natural : natural + frame]
            target = int(round(k * hop * speed))
            lo = max(0, target - tolerance)
            hi = min(len(samples) - frame, target + tolerance)
            if hi < lo or len(template) < frame:
                break
            correlation = np.correlate(samples[lo : hi + frame], template, mode="valid")
            best = lo + int(np.argmax(correlation))
        segment = samples[best : best + frame]
        if len(segment) < frame:
            break
        position = k * hop
        out[position : position + frame] += segment * window
        weight[position : position + frame] += window
        previous = best
        written = position + frame
    covered = weight > 1e-3
    out[covered] /= weight[covered]
    return out[:written]


def _normalize_peak(samples: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak <= 1e-6:
        return samples
    gain = min(10 ** (_PLAYBACK_PEAK_DBFS / 20) / peak, 10 ** (_PLAYBACK_MAX_GAIN_DB / 20))
    return samples * gain


def _fade_edges(samples: np.ndarray, rate: int, seconds: float = 0.005) -> np.ndarray:
    n = min(int(seconds * rate), len(samples) // 2)
    if n <= 0:
        return samples
    samples = samples.copy()
    ramp = np.linspace(0.0, 1.0, n)
    samples[:n] *= ramp
    samples[-n:] *= ramp[::-1]
    return samples


def _dbfs(value: float) -> float:
    return 20 * math.log10(value) if value > 1e-9 else -120.0


# ---- public conversions ----


def normalize_for_asterisk_playback(wav_bytes: bytes) -> bytes:
    """Any mono/stereo PCM WAV -> 8kHz mono 16-bit PCM (band-limited). A
    no-op (input returned unchanged) if it's already in that format."""
    info = read_wav_info(wav_bytes)
    if (info.sample_rate, info.channels, info.sample_width) == (ASTERISK_SAMPLE_RATE, 1, 2):
        return wav_bytes
    samples, rate = _read_pcm(wav_bytes)
    return _write_pcm(resample(samples, rate, ASTERISK_SAMPLE_RATE), ASTERISK_SAMPLE_RATE)


def prepare_tts_for_playback(wav_bytes: bytes, *, speed: float = 1.0) -> bytes:
    """TTS output -> telephone-ready 8kHz mono 16-bit PCM: band-limited
    resample, leading/trailing silence trimmed, tempo scaled by `speed`
    (pitch preserved), peak level normalized to -3 dBFS (never clipping),
    5 ms edge fades so chunks join without clicks. Raises AudioFormatError
    for audio that is effectively silent — there is nothing worth playing."""
    samples, rate = _read_pcm(wav_bytes)
    samples = resample(samples, rate, ASTERISK_SAMPLE_RATE)
    if len(samples) == 0 or _dbfs(float(np.max(np.abs(samples)))) < _SILENT_PEAK_DBFS:
        raise AudioFormatError("TTS audio is silent")
    samples = trim_silence(samples, ASTERISK_SAMPLE_RATE, db_below_peak=40.0, pad_seconds=0.06)
    samples = time_stretch(samples, ASTERISK_SAMPLE_RATE, speed)
    samples = _fade_edges(_normalize_peak(samples), ASTERISK_SAMPLE_RATE)
    return _write_pcm(samples, ASTERISK_SAMPLE_RATE)


# ---- profile-based TTS preparation with per-clip diagnostics ----

AUDIO_PROFILES = ("clean", "legacy")
_TEMPO_BYPASS_TOLERANCE = 0.01  # |speed - 1| below this: the samples are not touched at all
_ACTIVE_SPEECH_WINDOW_DB = 30.0  # frames within this of the loudest count as "speech" for loudness
_CLEAN_TRIM_DB_BELOW_PEAK = 50.0
_CLEAN_TRIM_PAD_SECONDS = 0.08
_CLEAN_FADE_SECONDS = 0.010
_CLEAN_MAX_BOOST_DB = 9.0
_CLEAN_MAX_CUT_DB = 12.0


@dataclass(frozen=True)
class ClipDiagnostics:
    """What was done to one synthesized clip, and what came out. Logged for
    every clip so a bad-sounding call can be traced to a stage instead of
    guessed at."""

    profile: str
    source_rate: int
    source_channels: int
    source_bits: int
    source_duration_s: float
    final_rate: int
    final_duration_s: float
    tempo: float
    tempo_applied: bool
    resampled: bool
    trimmed_s: float
    gain_db: float
    source_peak_dbfs: float
    source_rms_dbfs: float
    peak_dbfs: float
    rms_dbfs: float
    clipped_ratio: float
    noise_floor_dbfs: float
    processing_ms: int

    def summary(self) -> str:
        return (
            f"profile={self.profile} src={self.source_rate}Hz/{self.source_channels}ch/{self.source_bits}bit "
            f"src_dur={self.source_duration_s:.2f}s -> out={self.final_rate}Hz dur={self.final_duration_s:.2f}s "
            f"tempo={self.tempo:.2f}x tempo_applied={self.tempo_applied} resampled={self.resampled} "
            f"trimmed={self.trimmed_s:.2f}s gain={self.gain_db:+.1f}dB "
            f"src_peak={self.source_peak_dbfs:.1f}dBFS src_rms={self.source_rms_dbfs:.1f}dBFS "
            f"peak={self.peak_dbfs:.1f}dBFS rms={self.rms_dbfs:.1f}dBFS clipped={self.clipped_ratio:.4f} "
            f"noise_floor={self.noise_floor_dbfs:.1f}dBFS prep={self.processing_ms}ms"
        )


@dataclass(frozen=True)
class PreparedClip:
    audio: bytes
    diagnostics: ClipDiagnostics


def _fade_edges_cosine(samples: np.ndarray, rate: int, seconds: float) -> np.ndarray:
    n = min(int(seconds * rate), len(samples) // 2)
    if n <= 0:
        return samples
    samples = samples.copy()
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n))
    samples[:n] *= ramp
    samples[-n:] *= ramp[::-1]
    return samples


def _active_speech_rms(samples: np.ndarray, rate: int) -> float:
    """RMS of the speech itself, ignoring pauses: a clip that is half silence
    must not be judged quiet - or boosted into the noise floor - because of it."""
    rms, frame = _frame_rms(samples, rate)
    if len(rms) == 0 or rms.max() <= 0:
        return 0.0
    active = rms >= rms.max() * 10 ** (-_ACTIVE_SPEECH_WINDOW_DB / 20)
    frames = samples[: len(rms) * frame].reshape(len(rms), frame)[active]
    return float(np.sqrt(np.mean(frames**2))) if frames.size else 0.0


def prepare_tts_clip(
    wav_bytes: bytes,
    *,
    speed: float = 1.0,
    profile: str = "clean",
    target_rms_dbfs: float = -18.0,
    peak_ceiling_dbfs: float = -3.0,
) -> PreparedClip:
    """TTS output -> 8 kHz mono 16-bit PCM for Asterisk, plus diagnostics.

    "clean" (production default) does the least that is needed:
      * ONE band-limited resample, and none at all if the source is already 8 kHz;
      * NO tempo processing unless `speed` differs from 1.0 - at 1.0 the samples
        are not touched by any time-stretch code;
      * gentle trim (silence 50 dB below peak, 80 ms kept each side) so soft
        consonant tails are not cut;
      * level matched by the loudness of the speech (not by its highest
        peak), so consecutive chunks of one reply sound equally loud, with a
        hard peak ceiling so nothing can clip;
      * 10 ms raised-cosine edges.
    "legacy" reproduces the previous chain exactly (for A/B comparison).
    Raises AudioFormatError for effectively silent audio.
    """
    if profile not in AUDIO_PROFILES:
        raise ValueError(f"audio profile must be one of {AUDIO_PROFILES}")
    started = time.perf_counter()
    source, source_rate = _read_pcm(wav_bytes)
    source_info = read_wav_info(wav_bytes)
    source_levels = levels_of(source, source_rate)

    if profile == "legacy":
        audio = prepare_tts_for_playback(wav_bytes, speed=speed)
        out, out_rate = _read_pcm(audio)
        tempo_applied = abs(speed - 1.0) >= 0.01
        trimmed = max(0.0, source_levels.duration_seconds / (speed if tempo_applied else 1.0) - len(out) / out_rate)
        out_levels = levels_of(out, out_rate)
        gain_db = out_levels.peak_dbfs - source_levels.peak_dbfs
        return PreparedClip(audio, _diagnostics(
            "legacy", source_info, source_levels, speed, tempo_applied, source_rate != ASTERISK_SAMPLE_RATE,
            trimmed, gain_db, out, out_rate, started))

    samples = resample(source, source_rate, ASTERISK_SAMPLE_RATE)
    resampled = source_rate != ASTERISK_SAMPLE_RATE
    if len(samples) == 0 or _dbfs(float(np.max(np.abs(samples)))) < _SILENT_PEAK_DBFS:
        raise AudioFormatError("TTS audio is silent")
    before_trim = len(samples)
    samples = trim_silence(
        samples, ASTERISK_SAMPLE_RATE, db_below_peak=_CLEAN_TRIM_DB_BELOW_PEAK, pad_seconds=_CLEAN_TRIM_PAD_SECONDS
    )
    trimmed = (before_trim - len(samples)) / ASTERISK_SAMPLE_RATE

    tempo_applied = abs(speed - 1.0) >= _TEMPO_BYPASS_TOLERANCE
    if tempo_applied:
        samples = time_stretch(samples, ASTERISK_SAMPLE_RATE, speed)

    speech_rms = _active_speech_rms(samples, ASTERISK_SAMPLE_RATE)
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    gain = 1.0
    if speech_rms > 1e-6 and peak > 1e-6:
        gain = min(
            10 ** (target_rms_dbfs / 20) / speech_rms,
            10 ** (peak_ceiling_dbfs / 20) / peak,
            10 ** (_CLEAN_MAX_BOOST_DB / 20),
        )
        gain = max(gain, 10 ** (-_CLEAN_MAX_CUT_DB / 20))
    samples = _fade_edges_cosine(samples * gain, ASTERISK_SAMPLE_RATE, _CLEAN_FADE_SECONDS)
    audio = _write_pcm(samples, ASTERISK_SAMPLE_RATE)
    return PreparedClip(audio, _diagnostics(
        "clean", source_info, source_levels, speed, tempo_applied, resampled, trimmed,
        20 * math.log10(gain) if gain > 0 else -120.0, samples, ASTERISK_SAMPLE_RATE, started))


def _diagnostics(
    profile: str,
    source_info: WavInfo,
    source_levels: AudioLevels,
    speed: float,
    tempo_applied: bool,
    resampled: bool,
    trimmed_s: float,
    gain_db: float,
    out: np.ndarray,
    out_rate: int,
    started: float,
) -> ClipDiagnostics:
    out_levels = levels_of(out, out_rate)
    return ClipDiagnostics(
        profile=profile,
        source_rate=source_info.sample_rate,
        source_channels=source_info.channels,
        source_bits=source_info.sample_width * 8,
        source_duration_s=source_info.duration_seconds,
        final_rate=out_rate,
        final_duration_s=len(out) / out_rate,
        tempo=speed,
        tempo_applied=tempo_applied,
        resampled=resampled,
        trimmed_s=trimmed_s,
        gain_db=gain_db,
        source_peak_dbfs=source_levels.peak_dbfs,
        source_rms_dbfs=source_levels.rms_dbfs,
        peak_dbfs=out_levels.peak_dbfs,
        rms_dbfs=out_levels.rms_dbfs,
        clipped_ratio=out_levels.clipped_ratio,
        noise_floor_dbfs=out_levels.noise_floor_dbfs,
        processing_ms=int((time.perf_counter() - started) * 1000),
    )


def resample_for_asr(wav_bytes: bytes, *, target_rate: int = ASR_SAMPLE_RATE) -> bytes:
    """PCM WAV -> mono 16-bit PCM at `target_rate` (band-limited). Returns
    the input unchanged if it's already in that format."""
    info = read_wav_info(wav_bytes)
    if (info.sample_rate, info.channels, info.sample_width) == (target_rate, 1, 2):
        return wav_bytes
    samples, rate = _read_pcm(wav_bytes)
    return _write_pcm(resample(samples, rate, target_rate), target_rate)


def prepare_for_asr(wav_bytes: bytes, *, target_rate: int = ASR_SAMPLE_RATE) -> bytes:
    """Caller recording -> ASR input: leading/trailing silence trimmed (the
    recording always ends with the end-of-speech silence window, and often
    starts with a pause) keeping 250 ms of context, then resampled to
    `target_rate`. Less audio to upload and transcribe; the full recording
    is kept if trimming would leave under 0.3 s."""
    samples, rate = _read_pcm(wav_bytes)
    trimmed = trim_silence(samples, rate, db_below_peak=35.0, floor_dbfs=-50.0, pad_seconds=0.25)
    if len(trimmed) < MIN_TURN_DURATION_SECONDS * rate:
        trimmed = samples
    return _write_pcm(resample(trimmed, rate, target_rate), target_rate)


def measure_levels(wav_bytes: bytes) -> AudioLevels:
    """Level diagnostics for a clip: peak/RMS/noise floor in dBFS, share of
    clipped samples, and leading/trailing silence."""
    samples, rate = _read_pcm(wav_bytes)
    return levels_of(samples, rate)


def levels_of(samples: np.ndarray, rate: int) -> AudioLevels:
    """`measure_levels` for samples already in memory (float, -1..1)."""
    duration = len(samples) / rate
    if len(samples) == 0:
        return AudioLevels(0.0, -120.0, -120.0, -120.0, 0.0, 0.0, 0.0)
    rms_frames, _ = _frame_rms(samples, rate)
    bounds = _voiced_bounds(samples, rate, db_below_peak=40.0, floor_dbfs=-55.0)
    leading, trailing = (duration, 0.0) if bounds is None else (bounds[0] / rate, (len(samples) - bounds[1]) / rate)
    return AudioLevels(
        duration_seconds=duration,
        peak_dbfs=_dbfs(float(np.max(np.abs(samples)))),
        rms_dbfs=_dbfs(float(np.sqrt(np.mean(samples**2)))),
        noise_floor_dbfs=_dbfs(float(np.percentile(rms_frames, 10))) if len(rms_frames) else -120.0,
        clipped_ratio=float(np.mean(np.abs(samples) >= 0.999)),
        leading_silence_seconds=leading,
        trailing_silence_seconds=trailing,
    )


def is_effectively_silent(wav_bytes: bytes, *, rms_threshold: int = DEFAULT_SILENCE_RMS_THRESHOLD) -> bool:
    """Cheap noise-floor check so a turn with dead air doesn't burn an
    STT call. Deliberately simple (RMS over the whole clip) — good enough
    to skip near-total silence, not a voice-activity detector."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
            sample_width = wf.getsampwidth()
    except (wave.Error, EOFError) as exc:
        raise AudioFormatError(f"Not a readable WAV file: {exc}") from exc

    if not raw:
        return True
    return audioop.rms(raw, sample_width) < rms_threshold


def is_too_short(info: WavInfo, *, minimum_seconds: float = MIN_TURN_DURATION_SECONDS) -> bool:
    return info.duration_seconds < minimum_seconds


def make_short_beep_wav(
    *, duration_seconds: float = 0.3, frequency_hz: float = 1000.0, sample_rate: int = ASTERISK_SAMPLE_RATE
) -> bytes:
    """A short, genuinely bounded beep — 8kHz mono 16-bit PCM, directly
    Asterisk-playable with no normalization needed. Used as the
    caller-facing cue in test mode (TTS_PROVIDER=mock) instead of an
    indications.conf `tone:` reference: `tone:record`'s actual cadence
    (1400Hz for 80ms, then ~15s of silence per indications.conf) turned
    out — confirmed live — to make ARI Playback take ~15s to report
    PlaybackFinished, which is a poor fit for "prompt the caller and move
    on". A real short WAV file has a predictable, bounded duration.
    """
    n_samples = int(duration_seconds * sample_rate)
    amplitude = 10000
    samples = [int(amplitude * math.sin(2 * math.pi * frequency_hz * i / sample_rate)) for i in range(n_samples)]
    frames = struct.pack(f"<{len(samples)}h", *samples)

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames)
    return out.getvalue()
