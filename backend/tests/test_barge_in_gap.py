"""An interruption in the pause BETWEEN two reply chunks.

A reply is spoken chunk by chunk, and while the next chunk is still being
synthesised nothing is playing. The controller used to act on a caller's speech
only when a playback was active, so an interruption in that pause did nothing and
the AI then played the rest of its reply over the caller (measured on Asterisk
18.10: 2.72 s of overlap). The pause is now recognised as part of the reply.
"""

import asyncio
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from app.providers.tts.mock import MockTTSProvider
from app.services.call_controller import CallState
from tests.fake_ari import talking_started_event
from tests.test_call_controller import _make_controller

REPLY = (
    "The first sentence is long enough to be a complete spoken chunk. "
    "The second sentence should be discarded after the caller interrupts."
)


class GatedTTS(MockTTSProvider):
    """The first chunk synthesises at once; every later chunk waits for `gate`."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.gate = asyncio.Event()

    async def synthesize(self, text, *, voice=None, language=None):  # noqa: ANN001
        self.calls += 1
        if self.calls >= 2:
            await self.gate.wait()
        return await super().synthesize(text, voice=voice, language=language)


async def _until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _reply_paused_between_chunks(tmp: str):
    controller, ari = _make_controller(Path(tmp))
    tts = GatedTTS()
    controller._tts_provider = tts
    state = CallState("PJSIP/700-GAP", uuid.uuid4(), uuid.uuid4())
    controller._calls[state.channel_id] = state
    task = asyncio.create_task(controller._speak_reply(state, REPLY))
    # chunk 1 has played and finished; chunk 2 is still being synthesised: nothing is playing
    assert await _until(lambda: len(ari.played) == 1 and tts.calls == 2 and state.active_playback_id is None)
    assert state.reply_in_progress is True
    return controller, ari, tts, state, task


@pytest.mark.asyncio
async def test_an_interruption_between_chunks_discards_the_rest_of_the_reply() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, tts, state, task = await _reply_paused_between_chunks(tmp)

        await controller.dispatch_event(talking_started_event(state.channel_id))
        assert state.barge_in_generation == 1 and state.barge_in_count == 1

        tts.gate.set()  # chunk 2 finishes synthesising - too late
        result = await asyncio.wait_for(task, timeout=3)

        assert len(ari.played) == 1, "the AI played the next chunk over the caller"
        assert ari.stopped_playbacks == []  # nothing was playing, so nothing to stop
        assert result["chunks"] == 2
        assert state.reply_in_progress is False


@pytest.mark.asyncio
async def test_without_an_interruption_the_reply_plays_in_full() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, tts, state, task = await _reply_paused_between_chunks(tmp)
        tts.gate.set()
        await asyncio.wait_for(task, timeout=3)
        assert len(ari.played) == 2
        assert state.barge_in_generation == 0


@pytest.mark.asyncio
async def test_speech_after_the_reply_has_finished_is_not_a_barge_in() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, tts, state, task = await _reply_paused_between_chunks(tmp)
        tts.gate.set()
        await asyncio.wait_for(task, timeout=3)
        assert state.reply_in_progress is False

        await controller.dispatch_event(talking_started_event(state.channel_id))
        assert state.barge_in_generation == 0 and state.barge_in_count == 0


@pytest.mark.asyncio
async def test_the_reply_flag_is_cleared_even_if_the_call_ends_mid_reply() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, tts, state, task = await _reply_paused_between_chunks(tmp)
        state.ended.set()  # the caller hung up during the pause
        await asyncio.wait_for(task, timeout=3)
        assert state.reply_in_progress is False
        assert len(ari.played) == 1
