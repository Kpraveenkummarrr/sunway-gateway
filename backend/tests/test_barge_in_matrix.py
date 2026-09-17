"""The interruption state machine, scenario by scenario.

Live Asterisk proved the happy path (a caller interrupting mid-sentence is
detected in ~116 ms and the AI stops). What a live call cannot cheaply prove
is what happens at the edges: an interruption on the very first chunk or the
very last one, two interruptions in a row, a talk event that arrives after
the call is gone, or a spurious one caused by line noise while nobody is
speaking. Those are here, where the state is observable.

What must never happen after an interruption:
  * a queued chunk of the old reply still plays (stale audio);
  * the caller's next words are missed because no recording was started;
  * the generation counters drift, so a later barge-in is ignored.
"""

import asyncio
import tempfile
import uuid
from pathlib import Path

import pytest

from app.providers.llm.base import LLMProvider, LLMResponse
from app.services.call_controller import CallState
from tests.fake_ari import hangup_event, recording_finished_event, talking_started_event
from tests.test_call_controller import _make_controller, _poll_until
from tests.test_call_lifecycle import (  # noqa: F401 - logged_errors is a fixture
    WAIT,
    GatedTTS,
    _channel,
    _cleanup,
    _make,
    _start_call_and_record_question,
    logged_errors,
)

FOUR_CHUNK_REPLY = (
    "The first sentence is long enough to be one complete spoken chunk. "
    "The second sentence is also long enough to stand on its own here. "
    "The third sentence continues the answer for the caller as well. "
    "The fourth sentence finishes the answer that was being spoken."
)
TWO_CHUNK_REPLY = (
    "The first sentence is long enough to be one complete spoken chunk. "
    "The second sentence should never be played once the caller interrupts."
)


class _Recorded(LLMProvider):
    """Answers a fixed text and remembers what it was asked."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.questions: list[str] = []

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:
        last_user = [m for m in history if m.role == "user"]
        if last_user:
            self.questions.append(last_user[-1].content)
        return LLMResponse(text=self.text, finish_reason="stop")


def _state(controller, channel_id: str) -> CallState:
    state = CallState(channel_id, uuid.uuid4(), uuid.uuid4())
    controller._calls[channel_id] = state
    return state


# ---- where in the reply the interruption lands ----


async def _wait(predicate) -> None:
    """_poll_until with a bound, so a broken expectation fails instead of hanging."""
    await asyncio.wait_for(_poll_until(predicate), timeout=WAIT)


async def _advance_to_chunk(controller, ari, target: int) -> None:
    """Let playback finish chunk by chunk until `target` chunks have started."""
    await _wait(lambda: len(ari.played) >= 1)
    while len(ari.played) < target:
        current = len(ari.played)
        controller._on_playback_finished({"playback": {"id": f"playback-{current}"}})
        await _wait(lambda: len(ari.played) > current)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks_played",
    [1, 2, 4],
    ids=["near the beginning", "in the middle", "on the last chunk"],
)
async def test_an_interruption_stops_the_reply_wherever_it_lands(chunks_played) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        ari.on_play_started = None  # playback stays active until we advance it
        channel_id = f"PJSIP/700-BARGE-{chunks_played}"
        state = _state(controller, channel_id)

        speech = asyncio.create_task(controller._speak_reply(state, FOUR_CHUNK_REPLY))
        try:
            await _advance_to_chunk(controller, ari, chunks_played)
            await controller.dispatch_event(talking_started_event(channel_id))
            stats = await asyncio.wait_for(speech, timeout=WAIT)
        finally:
            controller._calls.pop(channel_id, None)

        assert stats["chunks"] == 4
        assert state.barge_in_generation == 1
        # Nothing further was played: the remaining chunks were discarded.
        assert len(ari.played) == chunks_played
        assert state.active_playback_id is None


@pytest.mark.asyncio
async def test_no_chunk_of_the_old_reply_is_played_after_the_interruption() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        ari.on_play_started = None
        channel_id = "PJSIP/700-BARGE-STALE"
        state = _state(controller, channel_id)

        speech = asyncio.create_task(controller._speak_reply(state, TWO_CHUNK_REPLY))
        await _wait(lambda: bool(ari.played))
        await controller.dispatch_event(talking_started_event(channel_id))
        await asyncio.wait_for(speech, timeout=WAIT)

        played_media = [media for _, media in ari.played]
        controller._calls.pop(channel_id, None)

        assert len(played_media) == 1
        assert ari.stopped_playbacks == ["playback-1"]
        assert state.active_playback_id is None


# ---- repeated and spurious events ----


@pytest.mark.asyncio
async def test_two_interruptions_in_a_row_are_both_counted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        ari.on_play_started = None
        channel_id = "PJSIP/700-BARGE-TWICE"
        state = _state(controller, channel_id)

        for expected_generation in (1, 2):
            speech = asyncio.create_task(controller._speak_reply(state, TWO_CHUNK_REPLY))
            await _wait(lambda: len(ari.played) == expected_generation)
            await controller.dispatch_event(talking_started_event(channel_id))
            await asyncio.wait_for(speech, timeout=WAIT)
            assert state.barge_in_generation == expected_generation

        controller._calls.pop(channel_id, None)
        assert len(ari.stopped_playbacks) == 2


@pytest.mark.asyncio
async def test_line_noise_while_nothing_is_playing_changes_nothing() -> None:
    """TALK_DETECT also fires on GSM noise. With no playback there is nothing
    to stop, and the call must not be nudged into a new state."""
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-BARGE-NOISE"
        state = _state(controller, channel_id)

        for _ in range(3):
            await controller.dispatch_event(talking_started_event(channel_id))

        controller._calls.pop(channel_id, None)
        assert state.barge_in_generation == 0
        assert ari.stopped_playbacks == []
        assert ari.recorded == []


@pytest.mark.asyncio
async def test_a_talk_event_for_an_unknown_channel_is_ignored(logged_errors) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        await controller.dispatch_event(talking_started_event("PJSIP/700-NOT-A-CALL"))
        assert ari.stopped_playbacks == []
        assert logged_errors == []


@pytest.mark.asyncio
async def test_a_talk_event_after_the_call_ended_is_ignored(logged_errors) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-BARGE-ENDED"
        state = _state(controller, channel_id)
        state.ended.set()

        await controller.dispatch_event(talking_started_event(channel_id))

        controller._calls.pop(channel_id, None)
        assert state.barge_in_generation == 0
        assert ari.stopped_playbacks == []
        assert logged_errors == []


# ---- the turn that follows the interruption ----


@pytest.mark.asyncio
async def test_the_caller_is_recorded_and_answered_after_interrupting(tmp_path, logged_errors) -> None:
    """The whole point of barge-in: the caller's new question is the one that
    gets answered, and the interrupted answer is not repeated."""
    llm = _Recorded(TWO_CHUNK_REPLY)
    controller, ari = _make(tmp_path, GatedTTS(), llm_provider=llm)
    channel = _channel()
    try:
        recording = await _start_call_and_record_question(controller, ari, tmp_path, channel)
        await asyncio.wait_for(
            controller.dispatch_event(recording_finished_event(recording)), timeout=WAIT
        )
        recordings_before = len(ari.recorded)

        await controller.dispatch_event(talking_started_event(channel))

        # The interruption's own recording completes and becomes a new turn.
        new_recording = ari.recorded[-1][1]
        (tmp_path / "recording" / f"{new_recording}.wav").write_bytes("dusra sawal".encode("utf-8"))
        await asyncio.wait_for(
            controller.dispatch_event(recording_finished_event(new_recording)), timeout=WAIT
        )

        assert len(ari.recorded) > recordings_before, "the caller's interruption was never recorded"
        assert len(llm.questions) == 2, "the interrupting question was not answered"
        assert logged_errors == []
    finally:
        await controller.dispatch_event(hangup_event(channel))
        await _cleanup(channel)
