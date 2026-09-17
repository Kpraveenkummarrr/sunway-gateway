"""Latency regressions for the reply path.

The client's first complaint was the pause before the AI says anything. Two
things keep that pause short, and both are easy to undo by accident:

* the reply is split into chunks, and the *first* chunk is allowed to be
  shorter than the rest, because synthesis time scales with length;
* chunk N+1 is synthesized while chunk N is already playing.

These measure both against a TTS stand-in whose synthesis time is
proportional to the text length, which is how a real engine behaves. They
assert relative timings, never absolute ones, so they do not turn into
flaky wall-clock tests on a loaded machine.
"""

import asyncio
import time

import pytest

from app.providers.tts.base import SynthesisResult, TTSProvider
from app.services.call_controller import (
    FIRST_SPEECH_CHUNK_CHARS,
    MIN_SPEECH_CHUNK_CHARS,
    speech_chunks,
)
from tests.audio_fixtures import make_tone_wav
from tests.fake_ari import hangup_event, recording_finished_event, stasis_start_event
from tests.test_call_lifecycle import (  # noqa: F401 - logged_errors is a fixture
    WAIT,
    FixedLLM,
    _channel,
    _cleanup,
    _make,
    _start_call_and_record_question,
    logged_errors,
)

SECONDS_PER_CHAR = 0.002

THREE_SENTENCE_REPLY = (
    "जी हाँ। "
    "लम्पी रोग मक्खियों, मच्छरों और किलनी से एक पशु से दूसरे पशु में फैलता है। "
    "पशु को बाकी जानवरों से अलग रखें और नजदीकी पशु चिकित्सक को दिखाएं।"
)


class TimedTTS(TTSProvider):
    """Synthesis takes time in proportion to the text, like a real engine."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.started_at: list[float] = []
        self.finished_at: list[float] = []

    async def synthesize(self, text, *, voice=None, language=None) -> SynthesisResult:
        self.texts.append(text)
        self.started_at.append(time.monotonic())
        await asyncio.sleep(SECONDS_PER_CHAR * len(text))
        self.finished_at.append(time.monotonic())
        return SynthesisResult(make_tone_wav(duration_seconds=0.05, sample_rate=48000), "wav")

    @property
    def whole_reply_seconds(self) -> float:
        return SECONDS_PER_CHAR * len(THREE_SENTENCE_REPLY)


# ---- chunking ----


def test_the_first_chunk_is_shorter_than_the_rest() -> None:
    chunks = speech_chunks(THREE_SENTENCE_REPLY)

    assert len(chunks) > 1, "a three-sentence reply must not be one TTS request"
    # The first chunk ends at the first sentence boundary past the minimum:
    # the closing sentence waits for its own request instead of delaying the
    # audio the caller is waiting to hear.
    assert "पशु को बाकी जानवरों" not in chunks[0]
    assert FIRST_SPEECH_CHUNK_CHARS <= len(chunks[0]) < len(THREE_SENTENCE_REPLY)


def test_a_speakable_closing_sentence_is_not_folded_into_the_first_chunk() -> None:
    """The regression: a tail below MIN_SPEECH_CHUNK_CHARS used to be merged
    backwards, which made the caller wait for the whole reply's synthesis."""
    reply = "जी हाँ। लम्पी रोग मक्खियों और मच्छरों से फैलता है। पशु को अलग रखें और डॉक्टर को दिखाएं।"
    chunks = speech_chunks(reply)

    assert len(chunks) == 2
    assert FIRST_SPEECH_CHUNK_CHARS <= len(chunks[1]) < MIN_SPEECH_CHUNK_CHARS


def test_a_tail_too_short_to_speak_alone_is_still_merged() -> None:
    chunks = speech_chunks("लम्पी रोग मक्खियों और मच्छरों से फैलता है। जी हाँ।")
    assert len(chunks) == 1


# ---- pipeline ----


@pytest.mark.asyncio
async def test_the_caller_hears_audio_before_the_whole_reply_is_synthesized(
    tmp_path, logged_errors
) -> None:
    tts = TimedTTS()
    controller, ari = _make(tmp_path, tts, llm_provider=FixedLLM(THREE_SENTENCE_REPLY))
    channel = _channel()
    play_times: list[float] = []
    original_play = ari.play

    async def timed_play(channel_id, *, media):
        play_times.append(time.monotonic())
        return await original_play(channel_id, media=media)

    ari.play = timed_play
    try:
        recording = await _start_call_and_record_question(controller, ari, tmp_path, channel)
        welcome_plays = len(play_times)
        welcome_calls = len(tts.texts)

        await asyncio.wait_for(
            controller.dispatch_event(recording_finished_event(recording)), timeout=WAIT
        )
        # Measured from the moment synthesis of the reply begins: ASR, RAG and
        # the LLM come before it and are not what chunking affects.
        first_reply_audio = play_times[welcome_plays] - tts.started_at[welcome_calls]

        assert len(tts.texts) > welcome_calls + 1, "the reply was synthesized in one request"
        assert first_reply_audio < tts.whole_reply_seconds, (
            f"first audio took {first_reply_audio:.3f}s, "
            f"whole-reply synthesis alone is {tts.whole_reply_seconds:.3f}s"
        )
        assert logged_errors == []
    finally:
        ari.play = original_play
        await controller.dispatch_event(hangup_event(channel))
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_the_next_chunk_is_synthesized_while_the_previous_one_plays(
    tmp_path, logged_errors
) -> None:
    tts = TimedTTS()
    controller, ari = _make(tmp_path, tts, llm_provider=FixedLLM(THREE_SENTENCE_REPLY))
    channel = _channel()
    play_times: list[float] = []
    original_play = ari.play

    async def timed_play(channel_id, *, media):
        play_times.append(time.monotonic())
        return await original_play(channel_id, media=media)

    ari.play = timed_play
    try:
        recording = await _start_call_and_record_question(controller, ari, tmp_path, channel)
        welcome_calls = len(tts.texts)

        await asyncio.wait_for(
            controller.dispatch_event(recording_finished_event(recording)), timeout=WAIT
        )

        reply_chunks = tts.texts[welcome_calls:]
        assert len(reply_chunks) >= 2
        # Chunk 2's synthesis finished after chunk 1 started playing: the two
        # overlap instead of running one after the other.
        second_chunk_ready = tts.finished_at[welcome_calls + 1]
        first_chunk_playing = play_times[-len(reply_chunks)]
        assert second_chunk_ready > first_chunk_playing
        assert logged_errors == []
    finally:
        ari.play = original_play
        await controller.dispatch_event(hangup_event(channel))
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_a_second_call_waits_on_no_tts_for_the_welcome(tmp_path, logged_errors) -> None:
    """The welcome is a fixed phrase: synthesizing it per call is pure delay."""
    tts = TimedTTS()
    controller, _ = _make(tmp_path, tts)
    first, second = _channel(), _channel()
    try:
        await controller.dispatch_event(stasis_start_event(first))
        after_first = len(tts.texts)

        started = time.monotonic()
        await controller.dispatch_event(stasis_start_event(second))
        elapsed = time.monotonic() - started

        assert len(tts.texts) == after_first
        assert elapsed < SECONDS_PER_CHAR * len(tts.texts[0])
        assert logged_errors == []
    finally:
        await controller.dispatch_event(hangup_event(first))
        await controller.dispatch_event(hangup_event(second))
        await _cleanup(first, second)
