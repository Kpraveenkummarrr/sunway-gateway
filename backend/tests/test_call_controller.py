import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import AsyncSessionLocal
from app.models.ai import AIMessage, AISession
from app.models.calls import Call
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.stt.mock import MockSTTProvider
from app.providers.tts.mock import MockTTSProvider
from app.services.call_controller import AICallController, CallState
from tests.fake_ari import (
    FakeAriClient,
    hangup_event,
    recording_finished_event,
    stasis_start_event,
    talking_started_event,
)


def _make_controller(tmp_path: Path, **overrides) -> AICallController:
    settings = Settings(
        ai_test_extension="700",
        ai_language="en",
        ai_call_timeout_seconds=120,
        ai_welcome_message="Hello, please ask your question.",
        rag_top_k=4,
        rag_similarity_threshold=-1.0,
        asterisk_recording_spool_path=str(tmp_path),
        provider_timeout_seconds=2.0,  # safety net only — playback completion is signaled explicitly below
        **overrides,
    )
    ari = FakeAriClient()
    controller = AICallController(
        ari=ari,
        settings=settings,
        session_factory=AsyncSessionLocal,
        embedding_provider=MockEmbeddingProvider(dimensions=1536),
        llm_provider=MockLLMProvider(),
        stt_provider=MockSTTProvider(),
        tts_provider=MockTTSProvider(),
    )
    # Real Asterisk emits a PlaybackFinished event once audio actually
    # finishes; the fake signals it right after play() so
    # AICallController._play_and_wait doesn't block for the real timeout.
    ari.on_play_started = lambda playback_id: controller._on_playback_finished(
        {"playback": {"id": playback_id}}
    )
    return controller, ari


def _write_recording(tmp_path: Path, name: str, text: str) -> None:
    """Mock STT convention: recorded 'audio' bytes are UTF-8 text."""
    path = tmp_path / f"{name}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


async def _cleanup(channel_state_session_id, call_row_id) -> None:
    async with AsyncSessionLocal() as db:
        session = await db.get(AISession, channel_state_session_id)
        if session is not None:
            result = await db.execute(select(AIMessage).where(AIMessage.session_id == session.id))
            for m in result.scalars().all():
                await db.delete(m)
            await db.delete(session)
        call_row = await db.get(Call, call_row_id)
        if call_row is not None:
            await db.delete(call_row)
        await db.commit()


@pytest.mark.asyncio
async def test_stasis_start_creates_call_and_session_and_answers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-00000001"

        await controller.dispatch_event(stasis_start_event(channel_id))

        assert channel_id in ari.answered
        assert controller.active_call_count == 1
        state = controller._calls[channel_id]

        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, state.call_row_id)
            session = await db.get(AISession, state.session_id)
            assert call_row is not None
            assert call_row.asterisk_channel_id == channel_id
            assert call_row.ai_handled is True
            assert call_row.status == "in_progress"
            assert session is not None
            assert session.status == "active"
            assert session.call_id == call_row.id

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_stasis_start_plays_welcome_and_starts_recording() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-00000002"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        # Mock TTS -> welcome played as a tone, not real synthesized speech.
        assert any(c == channel_id for c, _ in ari.played)
        assert any(c == channel_id for c, _ in ari.recorded)

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_full_turn_transcribes_retrieves_and_responds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        channel_id = "PJSIP/700-00000003"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        _write_recording(tmp_path, recording_name, "what are your business hours?")
        await controller.dispatch_event(recording_finished_event(recording_name))

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AIMessage).where(AIMessage.session_id == state.session_id).order_by(AIMessage.created_at)
            )
            messages = result.scalars().all()
            assert len(messages) == 2
            assert messages[0].role == "caller"
            assert messages[0].text == "what are your business hours?"
            assert messages[1].role == "agent"
            assert messages[1].text

        # A second recording should have been started for the next turn.
        assert len(ari.recorded) == 2

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_hangup_ends_session_and_closes_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-00000004"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        await controller.dispatch_event(hangup_event(channel_id))

        assert controller.active_call_count == 0
        async with AsyncSessionLocal() as db:
            session = await db.get(AISession, state.session_id)
            call_row = await db.get(Call, state.call_row_id)
            assert session.status == "completed"
            assert call_row.status == "completed"
            assert call_row.hangup_cause == "caller_hangup"
            assert call_row.ended_at is not None

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_two_concurrent_calls_are_fully_isolated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        channel_a = "PJSIP/700-0000000A"
        channel_b = "PJSIP/700-0000000B"

        await controller.dispatch_event(stasis_start_event(channel_a, caller_number="15550001"))
        await controller.dispatch_event(stasis_start_event(channel_b, caller_number="15550002"))

        assert controller.active_call_count == 2
        state_a = controller._calls[channel_a]
        state_b = controller._calls[channel_b]
        assert state_a.session_id != state_b.session_id
        assert state_a.call_row_id != state_b.call_row_id

        recording_a = [name for chan, name in ari.recorded if chan == channel_a][-1]
        recording_b = [name for chan, name in ari.recorded if chan == channel_b][-1]
        _write_recording(tmp_path, recording_a, "question from caller A")
        _write_recording(tmp_path, recording_b, "question from caller B")

        await controller.dispatch_event(recording_finished_event(recording_a))
        await controller.dispatch_event(recording_finished_event(recording_b))

        async with AsyncSessionLocal() as db:
            history_a = (
                await db.execute(select(AIMessage).where(AIMessage.session_id == state_a.session_id))
            ).scalars().all()
            history_b = (
                await db.execute(select(AIMessage).where(AIMessage.session_id == state_b.session_id))
            ).scalars().all()

        texts_a = [m.text for m in history_a]
        texts_b = [m.text for m in history_b]
        assert "question from caller A" in texts_a
        assert "question from caller A" not in texts_b
        assert "question from caller B" in texts_b
        assert "question from caller B" not in texts_a

        # Hanging up A must not affect B.
        await controller.dispatch_event(hangup_event(channel_a))
        assert controller.active_call_count == 1
        assert channel_b in controller._calls

        async with AsyncSessionLocal() as db:
            session_b = await db.get(AISession, state_b.session_id)
            assert session_b.status == "active"

        await controller.dispatch_event(hangup_event(channel_b))

        await _cleanup(state_a.session_id, state_a.call_row_id)
        await _cleanup(state_b.session_id, state_b.call_row_id)


@pytest.mark.asyncio
async def test_answer_failure_marks_call_failed_and_cleans_up() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-00000005"
        ari.fail_answer_for.add(channel_id)

        await controller.dispatch_event(stasis_start_event(channel_id))

        assert controller.active_call_count == 0  # cleaned up after answer failure

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Call).where(Call.asterisk_channel_id == channel_id))
            call_row = result.scalar_one()
            assert call_row.status == "failed"
            assert call_row.hangup_cause == "answer_failed"
            await db.delete(call_row)
            result2 = await db.execute(select(AISession).where(AISession.call_id == call_row.id))
            for s in result2.scalars().all():
                await db.delete(s)
            await db.commit()


@pytest.mark.asyncio
async def test_llm_provider_failure_plays_safe_error_and_continues_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        controller._llm_provider = MockLLMProvider(simulate_failure=True)
        channel_id = "PJSIP/700-00000006"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        _write_recording(tmp_path, recording_name, "a question that will fail")
        await controller.dispatch_event(recording_finished_event(recording_name))

        # Call should still be active — provider failure is handled, not fatal.
        assert channel_id in controller._calls
        # A safe-error tone/message should have been played, and the loop continues.
        assert len(ari.played) >= 2  # welcome + safe-error
        assert len(ari.recorded) == 2  # initial + next-turn recording still started

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_stt_failure_does_not_crash_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        channel_id = "PJSIP/700-00000007"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        # Malformed (non-UTF8) bytes -> mock STT raises STTProviderError.
        path = tmp_path / f"{recording_name}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00\x01not-valid-utf8")
        await controller.dispatch_event(recording_finished_event(recording_name))

        assert channel_id in controller._calls
        assert len(ari.recorded) == 2  # loop continued

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_silent_recording_skips_stt_and_reprompts() -> None:
    from tests.audio_fixtures import make_silence_wav

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        channel_id = "PJSIP/700-00000008"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        path = tmp_path / f"{recording_name}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make_silence_wav(duration_seconds=1.0))
        await controller.dispatch_event(recording_finished_event(recording_name))

        assert channel_id in controller._calls
        assert len(ari.recorded) == 2  # a next-turn recording was started, no STT call attempted

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AIMessage).where(AIMessage.session_id == state.session_id))
            assert result.scalars().all() == []  # no turn was recorded — silence never reached STT

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_too_short_recording_skips_stt_and_reprompts() -> None:
    from tests.audio_fixtures import make_tone_wav

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        channel_id = "PJSIP/700-00000009"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        path = tmp_path / f"{recording_name}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make_tone_wav(duration_seconds=0.1))  # below MIN_TURN_DURATION_SECONDS
        await controller.dispatch_event(recording_finished_event(recording_name))

        assert channel_id in controller._calls
        assert len(ari.recorded) == 2

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_consecutive_failures_end_call_gracefully() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path, ai_max_consecutive_failures=2)
        channel_id = "PJSIP/700-0000000C"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]
        call_row_id, session_id = state.call_row_id, state.session_id

        for _ in range(2):
            recording_name = ari.recorded[-1][1]
            path = tmp_path / f"{recording_name}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xfe\x00\x01not-valid-utf8")  # mock STT fails on this
            await controller.dispatch_event(recording_finished_event(recording_name))

        # After hitting the cap (2 consecutive failed turns), the call
        # should have ended itself gracefully rather than looping forever.
        assert channel_id not in controller._calls
        assert channel_id in ari.hungup

        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, call_row_id)
            session = await db.get(AISession, session_id)
            assert call_row.status == "completed"
            assert call_row.hangup_cause == "too_many_failed_turns"
            assert session.status == "completed"

        await _cleanup(session_id, call_row_id)


@pytest.mark.asyncio
async def test_no_input_ends_call_after_timeout() -> None:
    from tests.audio_fixtures import make_silence_wav

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path, ai_no_input_timeout_seconds=2)
        channel_id = "PJSIP/700-0000000F"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]
        call_row_id, session_id = state.call_row_id, state.session_id

        recording_name = ari.recorded[-1][1]
        (tmp_path / f"{recording_name}.wav").write_bytes(make_silence_wav(duration_seconds=1.0))
        await controller.dispatch_event(recording_finished_event(recording_name))
        assert channel_id in controller._calls  # 1s of 2s without input: re-prompted
        assert state.consecutive_failures == 0  # silence is not a failed turn

        recording_name = ari.recorded[-1][1]
        (tmp_path / f"{recording_name}.wav").write_bytes(make_silence_wav(duration_seconds=1.0))
        await controller.dispatch_event(recording_finished_event(recording_name))

        assert channel_id not in controller._calls
        assert channel_id in ari.hungup
        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, call_row_id)
            assert (call_row.status, call_row.hangup_cause) == ("completed", "no_input")

        await _cleanup(session_id, call_row_id)


@pytest.mark.asyncio
async def test_speech_resets_no_input_time() -> None:
    from tests.audio_fixtures import make_silence_wav

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path, ai_no_input_timeout_seconds=2)
        channel_id = "PJSIP/700-00000010"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        (tmp_path / f"{recording_name}.wav").write_bytes(make_silence_wav(duration_seconds=1.5))
        await controller.dispatch_event(recording_finished_event(recording_name))
        assert state.silent_seconds == pytest.approx(1.5)

        recording_name = ari.recorded[-1][1]
        _write_recording(tmp_path, recording_name, "a real question")
        await controller.dispatch_event(recording_finished_event(recording_name))
        assert state.silent_seconds == 0.0
        assert channel_id in controller._calls

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_recording_uses_end_of_speech_and_max_turn_settings() -> None:
    import uuid as _uuid

    from app.services.call_controller import CallState

    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_end_of_speech_silence_seconds=2, ai_max_turn_seconds=20)
        await controller._start_next_recording(CallState("PJSIP/700-REC", _uuid.uuid4(), _uuid.uuid4()))
        assert ari.record_params[-1] == {
            "max_silence_seconds": 2,
            "max_duration_seconds": 20,
            "beep": False,
        }


@pytest.mark.asyncio
async def test_caller_speech_stops_active_playback_for_barge_in() -> None:
    """A TALK_DETECT event cancels the active ARI playback and wakes the
    controller so it can start recording the caller's interruption."""
    import asyncio as _asyncio
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        ari.on_play_started = None  # keep the playback active for the test
        channel_id = "PJSIP/700-BARGE"
        state = CallState(channel_id, _uuid.uuid4(), _uuid.uuid4())

        playback_task = _asyncio.create_task(controller._play_and_wait(state, media="sound:test-reply"))
        await _poll_until(lambda: bool(ari.played))
        playback_id = f"playback-{len(ari.played)}"
        assert state.active_playback_id == playback_id

        controller._calls[channel_id] = state
        await controller.dispatch_event(talking_started_event(channel_id))
        await _asyncio.wait_for(playback_task, timeout=1.0)

        assert ari.stopped_playbacks == [playback_id]
        assert state.barge_in_generation == 1
        assert state.active_playback_id is None


@pytest.mark.asyncio
async def test_barge_in_discards_prefetched_reply_chunks() -> None:
    import asyncio as _asyncio
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        ari.on_play_started = None
        channel_id = "PJSIP/700-BARGE-CHUNKS"
        state = CallState(channel_id, _uuid.uuid4(), _uuid.uuid4())
        controller._calls[channel_id] = state
        reply = (
            "The first sentence is long enough to be a complete spoken chunk. "
            "The second sentence should be discarded after the caller interrupts."
        )

        speech_task = _asyncio.create_task(controller._speak_reply(state, reply))
        await _poll_until(lambda: bool(ari.played))
        await controller.dispatch_event(talking_started_event(channel_id))
        result = await _asyncio.wait_for(speech_task, timeout=1.0)

        assert result["chunks"] == 2
        assert result["tts_ms"] >= 0
        assert result["audio_ms"] >= 0
        assert len(ari.played) == 1
        assert len(ari.stopped_playbacks) == 1
        controller._calls.pop(channel_id, None)


@pytest.mark.asyncio
async def test_talking_event_without_playback_does_not_interrupt_idle_call() -> None:
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-NO-PLAYBACK"
        state = CallState(channel_id, _uuid.uuid4(), _uuid.uuid4())
        controller._calls[channel_id] = state
        await controller.dispatch_event(talking_started_event(channel_id))
        assert state.barge_in_generation == 0
        assert ari.stopped_playbacks == []
        controller._calls.pop(channel_id, None)


@pytest.mark.asyncio
async def test_media_formats_rtp_stats_and_playback_timing_are_measured(caplog) -> None:
    import logging
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp))
        channel_id = "PJSIP/700-MEDIA-DIAGNOSTICS"
        state = CallState(channel_id, _uuid.uuid4(), _uuid.uuid4())
        caplog.set_level(logging.INFO, logger="app.services.call_controller")

        await controller._log_media_formats(channel_id)
        await controller._play_and_wait(
            state,
            media="sound:test-reply",
            expected_duration_seconds=0.1,
        )
        await _poll_until(lambda: bool(ari.rtp_statistics_requests))

        assert ari.requested_channel_variables == [
            (channel_id, "CHANNEL(audionativeformat)"),
            (channel_id, "CHANNEL(audioreadformat)"),
            (channel_id, "CHANNEL(audiowriteformat)"),
        ]
        assert ari.rtp_statistics_requests == [channel_id]
        messages = [record.getMessage() for record in caplog.records]
        assert any("native=ulaw read=slin write=slin" in message for message in messages)
        assert any("RTP statistics:" in message and "txploss=0" in message for message in messages)
        assert any("Playback timing:" in message and "expected_ms=100" in message for message in messages)


@pytest.mark.asyncio
async def test_successful_turn_resets_failure_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path, ai_max_consecutive_failures=2)
        channel_id = "PJSIP/700-0000000D"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        # One failed turn (failure 1 of 2)...
        recording_name = ari.recorded[-1][1]
        path = tmp_path / f"{recording_name}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00\x01not-valid-utf8")  # mock STT fails on this
        await controller.dispatch_event(recording_finished_event(recording_name))
        assert state.consecutive_failures == 1

        # ...then a real, successful turn should reset the counter.
        recording_name = ari.recorded[-1][1]
        _write_recording(tmp_path, recording_name, "a real question")
        await controller.dispatch_event(recording_finished_event(recording_name))
        assert state.consecutive_failures == 0
        assert channel_id in controller._calls  # still going, cap was never hit

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_playback_completes_before_next_recording_starts() -> None:
    """The fake ARI signals PlaybackFinished asynchronously (one event-loop
    tick after play() is called, like real Asterisk would once audio
    actually finishes) — if the controller started the next recording
    without waiting for that, play() and record() calls would race in an
    order that doesn't reflect real playback-then-record sequencing. This
    just confirms the full turn completes cleanly with that timing in
    play — a regression here would show up as a hang (test timeout) or a
    missing recording, not a subtle ordering assertion.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        channel_id = "PJSIP/700-0000000E"

        await controller.dispatch_event(stasis_start_event(channel_id))
        state = controller._calls[channel_id]

        recording_name = ari.recorded[-1][1]
        _write_recording(tmp_path, recording_name, "what are your hours?")
        await controller.dispatch_event(recording_finished_event(recording_name))

        from app.services.call_controller import speech_chunks

        async with AsyncSessionLocal() as db:
            reply = (
                await db.execute(
                    select(AIMessage).where(AIMessage.session_id == state.session_id, AIMessage.role == "agent")
                )
            ).scalar_one()

        # Every reply chunk was played, and only after that did the next recording start.
        assert len(ari.played) == 1 + len(speech_chunks(reply.text))  # welcome + reply chunks
        assert len(ari.recorded) == 2  # initial + next-turn

        await _cleanup(state.session_id, state.call_row_id)


@pytest.mark.asyncio
async def test_slow_call_does_not_block_concurrent_call_via_run_forever() -> None:
    """run_forever() dispatches events as independent tasks specifically
    so a slow provider call on one channel can't stall progress on a
    concurrent one — this proves that, using a real (slow) LLM call on
    channel A racing a fast one on channel B."""
    import asyncio as _asyncio

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        controller, ari = _make_controller(tmp_path)
        _slow_llm_state = {"in_flight": False, "done": False}

        class _SlowLLM(MockLLMProvider):
            async def generate_response(self, **kwargs):
                _slow_llm_state["in_flight"] = True
                await _asyncio.sleep(3.0)
                result = await super().generate_response(**kwargs)
                _slow_llm_state["done"] = True
                return result

        controller._llm_provider = _SlowLLM()

        runner = _asyncio.create_task(controller.run_forever())
        try:
            channel_slow = "PJSIP/700-SLOW"
            channel_fast = "PJSIP/700-FAST"

            await ari.push_event(stasis_start_event(channel_slow))
            await _asyncio.wait_for(
                _poll_until(lambda: any(c == channel_slow for c, _ in ari.recorded)), timeout=2.0
            )
            state_slow = controller._calls[channel_slow]
            recording_name_slow = ari.recorded[-1][1]
            _write_recording(tmp_path, recording_name_slow, "a slow question")
            await ari.push_event(recording_finished_event(recording_name_slow))
            await _asyncio.wait_for(_poll_until(lambda: _slow_llm_state["in_flight"]), timeout=2.0)

            # While channel_slow's turn is still processing (its LLM call
            # won't return for ~0.5s), channel_fast's StasisStart must still
            # get handled — proven by relative ordering (fast channel done
            # BEFORE the slow LLM call returns) rather than an absolute
            # wall-clock deadline, so this isn't flaky under system load —
            # this would time out (or complete only after the slow LLM
            # call, failing the assertion below) if events were still
            # processed strictly sequentially.
            await ari.push_event(stasis_start_event(channel_fast))
            await _asyncio.wait_for(
                _poll_until(lambda: any(c == channel_fast for c, _ in ari.recorded)), timeout=10.0
            )
            assert not _slow_llm_state["done"], (
                "channel_fast only progressed after the slow LLM call finished — "
                "events are not being processed concurrently"
            )

            # Let the slow turn actually finish before tearing down.
            await _asyncio.wait_for(_poll_until(lambda: _slow_llm_state["done"]), timeout=10.0)
            await _asyncio.sleep(0.05)  # let the post-LLM DB commit land

            state_fast = controller._calls[channel_fast]
            await _cleanup(state_slow.session_id, state_slow.call_row_id)
            await _cleanup(state_fast.session_id, state_fast.call_row_id)
        finally:
            await ari.stop_events()
            runner.cancel()
            with pytest.raises(_asyncio.CancelledError):
                await runner
            pending = [t for t in controller._background_tasks if not t.done()]
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except BaseException:  # noqa: BLE001 - best-effort teardown, not asserting on these
                    pass


async def _poll_until(predicate, *, interval: float = 0.01) -> None:
    import asyncio as _asyncio

    while not predicate():
        await _asyncio.sleep(interval)
