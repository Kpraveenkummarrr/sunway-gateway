"""Audio format normalization and validation for the telephone AI pipeline.

Format map (verified empirically against the real Asterisk install —
see docs/asterisk.md#audio-formats):

  Asterisk ARI recordings (caller audio) -> WAV, 8kHz, mono, 16-bit
    signed PCM. This is what `channel.record(format="wav")` produces for
    a call on a ulaw/alaw channel (our PJSIP endpoints — see Phase 3).

  STT input -> passed through unchanged. OpenAI Whisper accepts audio in
    whatever format/sample rate it's given (it resamples server-side), so
    no conversion is needed on the way in.

  TTS output (OpenAI, response_format="wav") -> WAV, 24kHz, mono, 16-bit
    PCM — OpenAI's fixed native rate for that response format.

  Asterisk playback -> WAV, 8kHz or 16kHz mono 16-bit PCM ONLY. Confirmed
    via `asterisk -rx "module show like format"`: format_wav.so's own
    description is literally "Microsoft WAV/WAV16 format (8kHz/16kHz
    Signed Linear)" — 24kHz is not supported and fails to play.

So the one real conversion this project needs is TTS output -> 8kHz
mono 16-bit PCM WAV before Asterisk can play it back. Implemented with
the standard library only (`wave` + `audioop`) — no new dependency.
"""

import audioop
import io
import math
import struct
import wave
from dataclasses import dataclass

ASTERISK_SAMPLE_RATE = 8000
ASTERISK_PLAYABLE_RATES = (8000, 16000)
MIN_TURN_DURATION_SECONDS = 0.3
DEFAULT_SILENCE_RMS_THRESHOLD = 150  # on a 16-bit PCM scale (0-32767)


class AudioFormatError(Exception):
    """Raised when audio bytes aren't a readable/convertible WAV file."""


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    duration_seconds: float


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


def normalize_for_asterisk_playback(wav_bytes: bytes) -> bytes:
    """Converts arbitrary mono/stereo 8/16/32-bit PCM WAV audio to 8kHz
    mono 16-bit PCM — the format Asterisk's format_wav module can
    actually play. A no-op (returns the input unchanged) if it's already
    in a playable format, so this is safe to call unconditionally."""
    info = read_wav_info(wav_bytes)
    if is_asterisk_playable(info):
        return wav_bytes

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            rate = wf.getframerate()

        if sample_width != 2:
            raw = audioop.lin2lin(raw, sample_width, 2)
            sample_width = 2

        if channels == 2:
            raw = audioop.tomono(raw, sample_width, 0.5, 0.5)
            channels = 1

        if rate != ASTERISK_SAMPLE_RATE:
            raw, _ = audioop.ratecv(raw, sample_width, channels, rate, ASTERISK_SAMPLE_RATE, None)
            rate = ASTERISK_SAMPLE_RATE
    except audioop.error as exc:
        raise AudioFormatError(f"Could not convert audio for playback: {exc}") from exc

    out = io.BytesIO()
    with wave.open(out, "wb") as wf_out:
        wf_out.setnchannels(channels)
        wf_out.setsampwidth(sample_width)
        wf_out.setframerate(rate)
        wf_out.writeframes(raw)
    return out.getvalue()


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
