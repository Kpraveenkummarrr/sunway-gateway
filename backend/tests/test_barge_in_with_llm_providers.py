"""Part 8: barge-in must behave identically no matter which LLM provider is
configured.

`_on_talking_started` (the interruption handler) never reads `self._llm_provider`
— structurally, the LLM cannot affect interruption handling at all, since the
LLM only runs later, inside `_run_turn`, after a new recording has already
finished. These prove that empirically for a Gemini-labelled stub and a
Sarvam-M-labelled stub: the caller's interruption genuinely lands mid-reply
(playback frozen and polled, not left to auto-resolve before the talk event
fires), it stops playback exactly once, and the *new* question is answered
by whichever provider is configured, not a stale one.
"""

import asyncio

import pytest

from app.providers.llm.base import LLMProvider, LLMResponse
from tests.fake_ari import hangup_event, recording_finished_event, talking_started_event
from tests.test_call_controller import _poll_until
from tests.test_call_lifecycle import (  # noqa: F401 - logged_errors is a fixture
    WAIT,
    GatedTTS,
    _channel,
    _cleanup,
    _make,
    _start_call_and_record_question,
    logged_errors,
)

TWO_CHUNK_REPLY = (
    "The first sentence is long enough to be one complete spoken chunk. "
    "The second sentence should never be played once the caller interrupts."
)


class _LabelledLLM(LLMProvider):
    """A stub that answers with its own provider name baked into the reply,
    so a test can tell which provider actually produced a given turn."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.questions: list[str] = []

    @property
    def provider_name(self) -> str:
        return self._name

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:
        last_user = [m for m in history if m.role == "user"]
        if last_user:
            self.questions.append(last_user[-1].content)
        return LLMResponse(text=f"[{self._name}] {TWO_CHUNK_REPLY}", finish_reason="stop")


async def _wait(predicate) -> None:
    await asyncio.wait_for(_poll_until(predicate), timeout=WAIT)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["Gemini", "Sarvam-M local"])
async def test_barge_in_works_the_same_regardless_of_which_llm_answered(
    tmp_path, logged_errors, provider_name
) -> None:
    llm = _LabelledLLM(provider_name)
    controller, ari = _make(tmp_path, GatedTTS(), llm_provider=llm)
    channel = _channel()
    try:
        recording = await _start_call_and_record_question(controller, ari, tmp_path, channel)
        recordings_before = len(ari.recorded)

        # Freeze playback resolution so the reply's chunk is genuinely still
        # "playing" when the caller interrupts — otherwise the reply would
        # finish on its own and the interruption would land on an idle call,
        # proving nothing about barge-in specifically.
        played_before = len(ari.played)
        ari.on_play_started = None
        turn_task = asyncio.create_task(controller.dispatch_event(recording_finished_event(recording)))
        await _wait(lambda: len(ari.played) > played_before)

        await controller.dispatch_event(talking_started_event(channel))
        await asyncio.wait_for(turn_task, timeout=WAIT)

        assert len(ari.stopped_playbacks) == 1, "the reply's active playback was not stopped by the interruption"
        assert len(ari.recorded) > recordings_before, "the caller's interruption was never recorded"

        # Restore normal auto-resolving playback for the second (answering)
        # reply — only the first reply needed to be held "in flight".
        ari.on_play_started = lambda pid: controller._on_playback_finished({"playback": {"id": pid}})

        new_recording = ari.recorded[-1][1]
        (tmp_path / "recording" / f"{new_recording}.wav").write_bytes("दूसरा सवाल".encode("utf-8"))
        await asyncio.wait_for(
            controller.dispatch_event(recording_finished_event(new_recording)), timeout=WAIT
        )

        assert len(llm.questions) == 2, "the same configured provider must answer the interrupting question"
        assert logged_errors == []
    finally:
        await controller.dispatch_event(hangup_event(channel))
        await _cleanup(channel)
