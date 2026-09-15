"""Call-lifecycle race tests: a caller can hang up (StasisEnd /
ChannelDestroyed arrive as independent ARI events) while a call's own task
is still waiting on TTS, STT, playback or its database rows. The fake ARI
client behaves like Asterisk: once a channel is gone, /answer, /play and
/record return 404 and are recorded in `ari.rejected`.

Each race is made deterministic by holding one TTS request (or the first
database session) in flight with an asyncio.Event, rather than by sleeping.
"""

import asyncio
import contextlib
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

import app.services.call_controller as call_controller_module
from app.core.config import Settings
from app.core.db import AsyncSessionLocal
from app.models.ai import AIMessage, AISession
from app.models.calls import Call
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.stt.mock import MockSTTProvider
from app.providers.tts.base import SynthesisResult, TTSProvider
from app.services.audio import read_wav_info
from app.services.call_controller import AICallController, CallState
from tests.audio_fixtures import make_tone_wav
from tests.fake_ari import (
    FakeAriClient,
    channel_destroyed_event,
    hangup_event,
    recording_finished_event,
    stasis_start_event,
)

WAIT = 5.0  # generous upper bound for awaiting in-flight work; never a sleep


class GatedTTS(TTSProvider):
    """Returns a real 48 kHz WAV (Bhashini's native rate). Synthesis number
    `block_call` (1-based) waits on `release`, holding it in flight."""

    def __init__(self, *, block_call: int | None = None) -> None:
        self.block_call = block_call
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.completed = 0
        self.cancelled = 0

    async def synthesize(self, text, *, voice=None, language=None) -> SynthesisResult:
        self.calls += 1
        if self.calls == self.block_call:
            self.in_flight.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        self.completed += 1
        return SynthesisResult(make_tone_wav(duration_seconds=0.2, sample_rate=48000), "wav")


class GatedSessionFactory:
    """AsyncSessionLocal whose first session waits on `release` — holds
    StasisStart inside its database-row creation."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._first = True

    @contextlib.asynccontextmanager
    async def __call__(self):
        if self._first:
            self._first = False
            self.entered.set()
            await self.release.wait()
        async with AsyncSessionLocal() as db:
            yield db


def _channel() -> str:
    return f"PJSIP/smg-{uuid.uuid4().hex[:12]}"


def _make(tmp_path: Path, tts: TTSProvider, *, session_factory=AsyncSessionLocal, wire_playback=True, **overrides):
    values = dict(
        _env_file=None,
        ai_language="hi",
        ai_test_extension="700",
        ai_audio_timeout_seconds=8,
        rag_top_k=4,
        rag_similarity_threshold=-1.0,
        asterisk_recording_spool_path=str(tmp_path / "recording"),
        provider_timeout_seconds=2.0,
    )
    values.update(overrides)
    settings = Settings(**values)
    ari = FakeAriClient()
    controller = AICallController(
        ari=ari,
        settings=settings,
        session_factory=session_factory,
        embedding_provider=MockEmbeddingProvider(dimensions=1536),
        llm_provider=MockLLMProvider(),
        stt_provider=MockSTTProvider(),
        tts_provider=tts,
    )
    if wire_playback:
        ari.on_play_started = lambda pid: controller._on_playback_finished({"playback": {"id": pid}})
    return controller, ari


@pytest.fixture
def logged_errors(monkeypatch) -> list[str]:
    """Every error/exception the controller logs (dispatch_event logs any
    unhandled exception this way instead of raising it)."""
    errors: list[str] = []
    logger = call_controller_module.logger
    monkeypatch.setattr(logger, "exception", lambda msg, *a, **k: errors.append(msg % a if a else msg))
    monkeypatch.setattr(logger, "error", lambda msg, *a, **k: errors.append(msg % a if a else msg))
    return errors


def _simulate_asterisk_hangup(ari: FakeAriClient, channel_id: str) -> None:
    ari.gone.add(channel_id)


async def _call_row(channel_id: str) -> tuple[Call | None, AISession | None]:
    async with AsyncSessionLocal() as db:
        call = (await db.execute(select(Call).where(Call.asterisk_channel_id == channel_id))).scalar_one_or_none()
        session = None
        if call is not None:
            session = (await db.execute(select(AISession).where(AISession.call_id == call.id))).scalar_one_or_none()
        return call, session


async def _cleanup(*channel_ids: str) -> None:
    async with AsyncSessionLocal() as db:
        for channel_id in channel_ids:
            calls = (await db.execute(select(Call).where(Call.asterisk_channel_id == channel_id))).scalars().all()
            for call in calls:
                sessions = (await db.execute(select(AISession).where(AISession.call_id == call.id))).scalars().all()
                for session in sessions:
                    await db.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
                    await db.delete(session)
                await db.delete(call)
        await db.commit()


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


def _played_file(media: str) -> Path:
    assert media.startswith("sound:/") or media[len("sound:") :][1:3] == ":/"
    return Path(media[len("sound:") :] + ".wav")


# ---- normal paths ----


@pytest.mark.asyncio
async def test_welcome_plays_normally_while_channel_is_active(tmp_path, logged_errors) -> None:
    tts = GatedTTS()
    controller, ari = _make(tmp_path, tts)
    channel = _channel()
    try:
        await controller.dispatch_event(stasis_start_event(channel))

        assert ari.answered == [channel]
        assert len(ari.played) == 1 and ari.played[0][0] == channel
        info = read_wav_info(_played_file(ari.played[0][1]).read_bytes())
        assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)
        assert len(ari.recorded) == 1  # conversation loop started after the welcome
        assert ari.rejected == []
        assert controller._calls[channel].is_active
        assert logged_errors == []
    finally:
        await controller.dispatch_event(hangup_event(channel))
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_reply_tts_plays_normally_and_next_turn_starts(tmp_path, logged_errors) -> None:
    tts = GatedTTS()
    controller, ari = _make(tmp_path, tts)
    channel = _channel()
    try:
        await controller.dispatch_event(stasis_start_event(channel))
        recording = ari.recorded[-1][1]
        (tmp_path / "recording").mkdir(parents=True, exist_ok=True)
        (tmp_path / "recording" / f"{recording}.wav").write_bytes("लम्पी रोग क्या है?".encode("utf-8"))

        await controller.dispatch_event(recording_finished_event(recording))

        assert tts.completed == 2  # welcome + reply
        assert len(ari.played) == 2
        info = read_wav_info(_played_file(ari.played[1][1]).read_bytes())
        assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)
        assert len(ari.recorded) == 2  # next turn's recording
        assert ari.rejected == []
        assert logged_errors == []
    finally:
        await controller.dispatch_event(hangup_event(channel))
        await _cleanup(channel)


# ---- races ----


@pytest.mark.asyncio
async def test_hangup_during_welcome_tts_cancels_synthesis_and_never_calls_play(tmp_path, logged_errors) -> None:
    """The production bug: StasisEnd/ChannelDestroyed arrive while the
    welcome is being synthesized; the late TTS result must not reach /play."""
    tts = GatedTTS(block_call=1)
    controller, ari = _make(tmp_path, tts)
    channel = _channel()
    try:
        start = asyncio.create_task(controller.dispatch_event(stasis_start_event(channel)))
        await asyncio.wait_for(tts.in_flight.wait(), WAIT)

        _simulate_asterisk_hangup(ari, channel)
        await controller.dispatch_event(hangup_event(channel))
        await controller.dispatch_event(channel_destroyed_event(channel))
        await asyncio.wait_for(start, WAIT)

        assert tts.cancelled == 1 and tts.completed == 0
        assert ari.played == []
        assert ari.rejected == []  # no /play (or /record) was even attempted
        assert ari.recorded == []
        assert ari.hungup == []  # Asterisk already hung it up — no DELETE to a gone channel
        assert controller.active_call_count == 0
        assert logged_errors == []

        call, session = await _call_row(channel)
        assert (call.status, call.hangup_cause) == ("completed", "caller_hangup")
        assert session.status == "completed"
    finally:
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_channel_destroyed_before_reply_tts_completes(tmp_path, logged_errors) -> None:
    tts = GatedTTS(block_call=2)  # welcome completes; the first reply is held in flight
    controller, ari = _make(tmp_path, tts)
    channel = _channel()
    try:
        await controller.dispatch_event(stasis_start_event(channel))
        recording = ari.recorded[-1][1]
        (tmp_path / "recording").mkdir(parents=True, exist_ok=True)
        (tmp_path / "recording" / f"{recording}.wav").write_bytes("लम्पी रोग क्या है?".encode("utf-8"))

        turn = asyncio.create_task(controller.dispatch_event(recording_finished_event(recording)))
        await asyncio.wait_for(tts.in_flight.wait(), WAIT)

        _simulate_asterisk_hangup(ari, channel)
        await controller.dispatch_event(channel_destroyed_event(channel))
        await asyncio.wait_for(turn, WAIT)

        assert tts.cancelled == 1
        assert len(ari.played) == 1  # the welcome only — the reply was never played
        assert ari.rejected == []
        assert len(ari.recorded) == 1  # no next-turn recording on a dead channel
        assert controller.active_call_count == 0
        assert logged_errors == []
    finally:
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_channel_gone_between_check_and_play_ends_call_cleanly(tmp_path, logged_errors) -> None:
    """Asterisk destroys the channel while TTS runs, but the StasisEnd event
    hasn't been processed yet: /play returns 404, which must end the call as
    a hangup — not raise, not log an error, not continue the loop."""
    tts = GatedTTS(block_call=1)
    controller, ari = _make(tmp_path, tts)
    channel = _channel()
    try:
        start = asyncio.create_task(controller.dispatch_event(stasis_start_event(channel)))
        await asyncio.wait_for(tts.in_flight.wait(), WAIT)

        _simulate_asterisk_hangup(ari, channel)  # no event delivered yet
        tts.release.set()
        await asyncio.wait_for(start, WAIT)

        assert ari.rejected == [("play", channel)]  # exactly one attempt, answered 404
        assert ari.played == [] and ari.recorded == []
        assert controller.active_call_count == 0
        assert logged_errors == []

        await controller.dispatch_event(hangup_event(channel))  # the late event is a no-op
        call, session = await _call_row(channel)
        assert (call.status, call.hangup_cause) == ("completed", "caller_hangup")
        assert session.status == "completed"
    finally:
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_playback_skipped_for_a_call_that_already_ended(tmp_path, logged_errors) -> None:
    tts = GatedTTS()
    controller, ari = _make(tmp_path, tts, session_factory=None)
    state = CallState(_channel(), uuid.uuid4(), uuid.uuid4())
    state.ended.set()

    await controller._synthesize_and_play(state, "नमस्ते")
    await controller._play_and_wait(state, media="sound:/tmp/anything")
    await controller._start_next_recording(state)

    assert tts.calls == 0  # not even synthesized
    assert ari.played == [] and ari.rejected == [] and ari.recorded == []
    assert logged_errors == []


@pytest.mark.asyncio
async def test_hangup_during_playback_stops_waiting_immediately(tmp_path, logged_errors) -> None:
    tts = GatedTTS()
    # PlaybackFinished never arrives, and the playback timeout is long:
    # only the hangup can end the wait.
    controller, ari = _make(tmp_path, tts, wire_playback=False, provider_timeout_seconds=30.0)
    channel = _channel()
    try:
        start = asyncio.create_task(controller.dispatch_event(stasis_start_event(channel)))
        await asyncio.wait_for(_until(lambda: controller._playback_finished), WAIT)  # welcome is playing

        started = time.monotonic()
        _simulate_asterisk_hangup(ari, channel)
        await controller.dispatch_event(hangup_event(channel))
        await asyncio.wait_for(start, WAIT)

        assert time.monotonic() - started < 2.0  # not the 30s playback timeout
        assert ari.recorded == [] and ari.rejected == []
        assert controller._playback_finished == {}
        assert logged_errors == []
    finally:
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_hangup_while_call_rows_are_being_created(tmp_path, logged_errors) -> None:
    factory = GatedSessionFactory()
    controller, ari = _make(tmp_path, GatedTTS(), session_factory=factory)
    channel = _channel()
    try:
        start = asyncio.create_task(controller.dispatch_event(stasis_start_event(channel)))
        await asyncio.wait_for(factory.entered.wait(), WAIT)

        _simulate_asterisk_hangup(ari, channel)
        await controller.dispatch_event(hangup_event(channel))
        factory.release.set()
        await asyncio.wait_for(start, WAIT)

        assert ari.answered == [] and ari.played == [] and ari.recorded == []
        assert ari.rejected == []  # never tried to answer a channel that had gone
        assert controller.active_call_count == 0
        assert logged_errors == []

        call, session = await _call_row(channel)
        assert (call.status, call.hangup_cause) == ("completed", "caller_hangup")
        assert call.ended_at is not None
        assert session.status == "completed"
    finally:
        await _cleanup(channel)


@pytest.mark.asyncio
async def test_hangup_of_one_call_does_not_affect_a_concurrent_call(tmp_path, logged_errors) -> None:
    tts = GatedTTS(block_call=1)  # call A's welcome is held; call B's is not
    controller, ari = _make(tmp_path, tts)
    channel_a, channel_b = _channel(), _channel()
    try:
        start_a = asyncio.create_task(controller.dispatch_event(stasis_start_event(channel_a)))
        await asyncio.wait_for(tts.in_flight.wait(), WAIT)
        await controller.dispatch_event(stasis_start_event(channel_b))

        _simulate_asterisk_hangup(ari, channel_a)
        await controller.dispatch_event(hangup_event(channel_a))
        await asyncio.wait_for(start_a, WAIT)

        assert tts.cancelled == 1
        assert [c for c, _ in ari.played] == [channel_b]
        assert [c for c, _ in ari.recorded] == [channel_b]
        assert ari.rejected == []
        assert channel_a not in controller._calls
        assert controller._calls[channel_b].is_active
        assert logged_errors == []
    finally:
        await controller.dispatch_event(hangup_event(channel_b))
        await _cleanup(channel_a, channel_b)
