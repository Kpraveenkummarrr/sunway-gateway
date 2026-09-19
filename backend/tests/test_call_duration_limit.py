"""The call duration limit: why calls were cut at about 2:45, and what replaced it.

AI_CALL_TIMEOUT_SECONDS was 120 and was evaluated only when a caller turn
finished. With ~55 s per exchange the checks landed at 55 s, 110 s and 165 s, so
the first one past 120 s cut the call at 2:45 - abruptly, with no goodbye. Now
the limit is 900 s by default, is enforced by a timer, and ends with a polite
closing message. (Reproduced on Asterisk 18.10 before the change: a call cut at
143 s with "Call timeout reached" after two answers.)
"""

import asyncio
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.call_controller import CallState
from tests.fake_ari import recording_finished_event
from tests.test_call_controller import _make_controller


def _state(controller, channel: str, *, age_seconds: float) -> CallState:
    state = CallState(channel, uuid.uuid4(), uuid.uuid4())
    state.started_monotonic = time.monotonic() - age_seconds
    controller._calls[channel] = state
    return state


def test_the_default_limit_no_longer_cuts_a_call_at_two_minutes() -> None:
    settings = Settings(_env_file=None)
    assert settings.ai_call_timeout_seconds == 900
    assert settings.ai_call_timeout_seconds > 165  # the point at which the old limit actually fired


@pytest.mark.asyncio
async def test_a_call_past_its_limit_is_closed_politely_with_a_goodbye() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=60)
        state = _state(controller, "PJSIP/700-LIM1", age_seconds=75)

        assert await controller._end_if_over_duration(state) is True

        assert ari.played, "the caller must hear a closing message, not a silent hangup"
        assert state.end_status == "completed"
        assert state.hangup_cause == "max_duration"
        assert "PJSIP/700-LIM1" in ari.hungup


@pytest.mark.asyncio
async def test_a_call_within_its_limit_is_left_alone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=900)
        state = _state(controller, "PJSIP/700-LIM2", age_seconds=170)  # past 2:45

        assert await controller._end_if_over_duration(state) is False
        assert ari.hungup == [] and state.is_active
        controller._calls.pop("PJSIP/700-LIM2", None)


@pytest.mark.asyncio
async def test_zero_means_no_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=0)
        state = _state(controller, "PJSIP/700-LIM3", age_seconds=10_000)
        assert await controller._end_if_over_duration(state) is False
        controller._calls.pop("PJSIP/700-LIM3", None)


@pytest.mark.asyncio
async def test_the_timer_ends_a_listening_call_without_waiting_for_a_turn_boundary() -> None:
    """The old check only ran when a turn finished. The timer stops the open
    recording at the limit, and the ordinary recording-finished path closes the call."""
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=1, ai_endpoint_monitor=False)
        state = _state(controller, "PJSIP/700-LIM4", age_seconds=0)
        await controller._start_next_recording(state)
        timer = asyncio.create_task(controller._call_deadline(state))
        deadline = time.monotonic() + 4
        while not ari.stopped_recordings and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await timer
        assert ari.stopped_recordings == [state.recording_name], "the timer never ended the turn"

        # Asterisk answers the stop with RecordingFinished; the call then closes with a goodbye
        await controller.dispatch_event(recording_finished_event(state.recording_name))
        assert state.hangup_cause == "max_duration"
        assert ari.played


@pytest.mark.asyncio
async def test_the_timer_does_nothing_when_the_call_ends_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=1, ai_endpoint_monitor=False)
        state = _state(controller, "PJSIP/700-LIM5", age_seconds=0)
        await controller._start_next_recording(state)
        timer = asyncio.create_task(controller._call_deadline(state))
        await asyncio.sleep(0.1)
        state.ended.set()  # the caller hung up
        await asyncio.wait_for(timer, timeout=2)
        assert ari.stopped_recordings == []


@pytest.mark.asyncio
async def test_a_call_that_is_thinking_or_speaking_is_not_interrupted_by_the_timer() -> None:
    """No open recording (the AI is answering): the answer finishes, and the call
    closes when it would listen next."""
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=1)
        state = _state(controller, "PJSIP/700-LIM6", age_seconds=0)
        state.recording_seq = 1
        state.consumed_recording_seq = 1  # the last turn was consumed: nothing is being recorded
        state.recording_name = "ai-agent__x__1"
        await asyncio.wait_for(controller._call_deadline(state), timeout=4)
        assert ari.stopped_recordings == []
        assert state.is_active


@pytest.mark.asyncio
async def test_the_next_listening_turn_after_the_limit_closes_instead_of_recording() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_call_timeout_seconds=30)
        state = _state(controller, "PJSIP/700-LIM7", age_seconds=45)
        await controller._start_next_recording(state)
        assert ari.recorded == [], "no new recording after the limit"
        assert state.hangup_cause == "max_duration"
