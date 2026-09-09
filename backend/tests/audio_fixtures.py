"""Real, valid WAV audio fixtures for testing the audio normalization
layer and provider error paths — genuine playable audio (silence, a
sine tone, noise), not the Phase 5/6 mock convention of "audio bytes are
just UTF-8 text".

These are NOT spoken-word test phrases — synthesizing real speech
without a live TTS call (pending explicit approval — see the Phase 7
report) or a recorded human voice isn't possible here. What these
fixtures exercise: WAV parsing, sample-rate/channel/width detection,
resampling correctness, and silence/duration validation — all real
audio-processing logic, independent of what the waveform "says".
"""

import io
import math
import struct
import wave


def make_silence_wav(*, duration_seconds: float = 1.0, sample_rate: int = 8000, channels: int = 1) -> bytes:
    n_samples = int(duration_seconds * sample_rate)
    frames = b"\x00\x00" * n_samples * channels
    return _wrap_wav(frames, sample_rate=sample_rate, channels=channels, sample_width=2)


def make_tone_wav(
    *,
    duration_seconds: float = 1.0,
    frequency_hz: float = 440.0,
    sample_rate: int = 8000,
    channels: int = 1,
    amplitude: int = 12000,
) -> bytes:
    """A real sine-wave tone — genuine audio energy (not silence), useful
    for testing that non-silent audio is correctly NOT flagged as
    silent, and for testing resampling with real signal content."""
    n_samples = int(duration_seconds * sample_rate)
    samples = []
    for i in range(n_samples):
        value = int(amplitude * math.sin(2 * math.pi * frequency_hz * i / sample_rate))
        for _ in range(channels):
            samples.append(value)
    frames = struct.pack(f"<{len(samples)}h", *samples)
    return _wrap_wav(frames, sample_rate=sample_rate, channels=channels, sample_width=2)


def make_noise_wav(*, duration_seconds: float = 1.0, sample_rate: int = 8000, seed: int = 42) -> bytes:
    """Deterministic pseudo-random noise (not true silence, not a clean
    tone) — for testing that noisy-but-real audio passes the silence
    check and flows into the pipeline."""
    import random

    rng = random.Random(seed)
    n_samples = int(duration_seconds * sample_rate)
    samples = [rng.randint(-8000, 8000) for _ in range(n_samples)]
    frames = struct.pack(f"<{len(samples)}h", *samples)
    return _wrap_wav(frames, sample_rate=sample_rate, channels=1, sample_width=2)


def make_empty_wav() -> bytes:
    return _wrap_wav(b"", sample_rate=8000, channels=1, sample_width=2)


def _wrap_wav(frames: bytes, *, sample_rate: int, channels: int, sample_width: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(frames)
    return buf.getvalue()
