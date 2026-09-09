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
from app.services.call_controller import AICallController
from tests.fake_ari import FakeAriClient, hangup_event, recording_finished_event, stasis_start_event


def _make_controller(tmp_path: Path, **overrides) -> AICallController:
    settings = Settings(
        ai_test_extension="700",
        ai_language="en",
        ai_call_timeout_seconds=120,
        ai_audio_timeout_seconds=8,
        ai_welcome_message="Hello, please ask your question.",
        rag_top_k=4,
        rag_similarity_threshold=-1.0,
        asterisk_recording_spool_path=str(tmp_path),
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
