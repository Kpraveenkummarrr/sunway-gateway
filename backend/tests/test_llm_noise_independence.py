"""Part 7/12: can the LLM provider be the source of GSM audio noise?

Structural answer, proven rather than asserted: `LLMResponse` (base.py) has
exactly two fields — `text` and `finish_reason`. No audio bytes, no sample
rate, no gain, nothing DSP-relevant crosses the LLM/TTS boundary. The audio
pipeline (spoken_text -> speech_chunks -> TTS provider -> resample/normalize
-> Asterisk) receives only a Python string. Whatever produced that string —
Gemini, Sarvam-M, a human typing it in — is indistinguishable to everything
downstream of it.

These tests make that concrete: two very different reply texts (different
provider "styles" — one long and plain, one short with markdown a less
prompt-adherent local model might leave in) are pushed through the exact
production audio pipeline, using the exact same synthetic TTS source signal
each time, and the measured audio-domain properties (alias suppression,
peak, clipping, RMS) come out identical. If a real call has noise with
Gemini and the same noise with Sarvam-M, this is why: the LLM has no channel
through which it could have caused it.
"""

import dataclasses
import io
import wave

import numpy as np

from app.providers.llm.base import LLMResponse
from app.services.audio import measure_levels, prepare_tts_for_playback
from app.services.call_controller import speech_chunks

GEMINI_STYLE_REPLY = (
    "जी हाँ। लम्पी रोग मक्खियों, मच्छरों और किलनी से एक पशु से दूसरे पशु में फैलता है। "
    "पशु को बाकी जानवरों से अलग रखें और नजदीकी पशु चिकित्सक को दिखाएं।"
)
SARVAM_STYLE_REPLY = (
    "### जवाब\n\n"
    "1. **लम्पी रोग** मक्खियों से फैलता है\n"
    "2. पशु को अलग रखें\n"
)


def _voice_like_wav(seconds: float = 2.0, rate: int = 48000) -> bytes:
    """A fixed synthetic signal standing in for a Bhashini response — the
    same bytes every time, so what varies between runs is only the text
    that produced the chunk boundaries feeding into this."""
    t = np.arange(int(seconds * rate)) / rate
    f0 = 120.0
    signal = sum(amp * np.sin(k * 2 * np.pi * f0 * t) for k, amp in enumerate([1.0, 0.6, 0.4, 0.28], start=1))
    signal += 0.15 * np.sin(2 * np.pi * 6000 * t)  # some sibilant-range energy, like real speech
    signal *= 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)
    signal = 0.6 * signal / np.max(np.abs(signal))
    pcm = np.clip(np.round(signal * 32767), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_llmresponse_carries_no_audio_relevant_data() -> None:
    """The type itself proves the point: there is nothing here for a
    provider to use to influence gain, sample rate or any other DSP
    parameter, even if it wanted to."""
    fields = {f.name for f in dataclasses.fields(LLMResponse)}
    assert fields == {"text", "finish_reason"}


def test_the_processed_audio_is_identical_regardless_of_which_llm_style_produced_the_text() -> None:
    """Different reply text (different provider "voice") drives different
    chunk counts and boundaries, but every chunk is synthesized from the
    SAME underlying source audio here (as it would be from the same TTS
    provider in production) — and the measured audio-domain properties of
    each processed chunk must be identical regardless of which reply text
    produced it."""
    source = _voice_like_wav()

    gemini_chunks = speech_chunks(GEMINI_STYLE_REPLY)
    sarvam_chunks = speech_chunks(SARVAM_STYLE_REPLY)
    assert gemini_chunks and sarvam_chunks
    assert len(gemini_chunks) != len(sarvam_chunks) or gemini_chunks != sarvam_chunks  # genuinely different texts

    gemini_processed = [prepare_tts_for_playback(source, speed=1.15) for _ in gemini_chunks]
    sarvam_processed = [prepare_tts_for_playback(source, speed=1.15) for _ in sarvam_chunks]

    gemini_levels = [measure_levels(clip) for clip in gemini_processed]
    sarvam_levels = [measure_levels(clip) for clip in sarvam_processed]

    for levels in (gemini_levels, sarvam_levels):
        for level in levels:
            assert level.clipped_ratio == 0.0
            assert level.peak_dbfs <= -2.8

    # The whole point: same source audio in -> same measured audio out, no
    # matter which provider's text asked for how many chunks of it.
    assert gemini_levels[0].peak_dbfs == sarvam_levels[0].peak_dbfs
    assert gemini_levels[0].rms_dbfs == sarvam_levels[0].rms_dbfs
    assert gemini_levels[0].noise_floor_dbfs == sarvam_levels[0].noise_floor_dbfs
    assert gemini_processed[0] == sarvam_processed[0]  # byte-identical output


def test_markup_that_a_less_adherent_model_might_leave_in_never_reaches_the_audio_stage() -> None:
    """Even if a local model is less reliably instructed than Gemini and
    leaves markdown in, spoken_text strips it before anything is synthesized
    — the audio stage never sees a difference to react to."""
    chunks = speech_chunks(SARVAM_STYLE_REPLY)
    joined = " ".join(chunks)
    for marker in ("#", "*", "1.", "2."):
        assert marker not in joined
