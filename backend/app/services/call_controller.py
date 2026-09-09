"""AI call controller: bridges Asterisk ARI call events to the Phase 5
conversation service.

Responsibility split (per docs/architecture.md):
    Asterisk owns  — SIP, RTP, channel lifecycle, answering, media playback/recording.
    This module owns — mapping a channel to an AI session, driving the
        turn loop (record -> conversation service -> synthesize -> play),
        and cleaning up on hangup/error/timeout.

No AI business logic (RAG, LLM, prompt policy) lives here — all of that
stays in app.services.conversation, unmodified from Phase 5. This module
only decides *when* to call it and how to get audio in and out of Asterisk.

TEST MODE vs PRODUCTION MEDIA MODE (see docs/architecture.md):
When TTS_PROVIDER=mock, synthesized "audio" is placeholder bytes that
Asterisk cannot play as real speech — the controller marks the message as
delivered (for conversation-loop/session-state testing) without invoking
ARI playback for it, and plays a short tone instead so the call-control
mechanism (the Playback API call itself) is still exercised. This is not
a fallback from a *configured* real provider to mock — TTS_PROVIDER is
never silently swapped; it only affects how already-mock output is
handled downstream.
"""

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.ai import AISession
from app.models.calls import Call
from app.providers.embeddings.base import EmbeddingProvider, EmbeddingProviderError
from app.providers.llm.base import LLMProvider, LLMProviderError
from app.providers.stt.base import STTProvider, STTProviderError
from app.providers.tts.base import TTSProvider, TTSProviderError
from app.services.ari_client import AriClient, AriError
from app.services.conversation import ConversationError, create_session, end_session, handle_text_turn

logger = get_logger(__name__)

# "tone:record" is an indications.conf tone (always available, no sound
# file needed) — this environment's asterisk-core-sounds-en package turned
# out to ship no actual audio files, confirmed live, so a sound: URI can't
# be relied on. Stands in for real TTS audio in test mode.
_TONE_MEDIA = "tone:record"
_RECORDING_NAME_PREFIX = "ai-agent__"
_SAFE_ERROR_MESSAGE = (
    "Sorry, I'm having trouble right now. Please try again later or contact us directly."
)


class CallState:
    """In-memory bookkeeping for one active call, keyed by Asterisk channel
    id. The database (Call + AISession rows) remains the durable source of
    truth — this is just what the controller needs while the call is live
    (recording sequence number, turn count for the timeout/turn cap)."""

    __slots__ = ("channel_id", "call_row_id", "session_id", "started_monotonic", "recording_seq")

    def __init__(self, channel_id: str, call_row_id: uuid.UUID, session_id: uuid.UUID) -> None:
        self.channel_id = channel_id
        self.call_row_id = call_row_id
        self.session_id = session_id
        self.started_monotonic = time.monotonic()
        self.recording_seq = 0


class AICallController:
    """Owns the ARI event loop and the channel_id -> (Call, AISession)
    mapping for every AI test call currently in progress.

    One controller instance handles arbitrarily many concurrent calls —
    state is per-channel (in `_calls`, keyed by channel id), never global,
    so sessions cannot leak into each other (see tests/test_call_controller.py).
    """

    def __init__(
        self,
        *,
        ari: AriClient,
        settings: Settings,
        session_factory,
        embedding_provider: EmbeddingProvider,
        llm_provider: LLMProvider,
        stt_provider: STTProvider,
        tts_provider: TTSProvider,
    ) -> None:
        self._ari = ari
        self._settings = settings
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._llm_provider = llm_provider
        self._stt_provider = stt_provider
        self._tts_provider = tts_provider
        self._calls: dict[str, CallState] = {}

    @property
    def active_call_count(self) -> int:
        return len(self._calls)

    async def run_forever(self) -> None:
        """Consumes the ARI event stream until the connection closes."""
        async for event in self._ari.events():
            await self.dispatch_event(event)

    async def dispatch_event(self, event: dict) -> None:
        """Handles one ARI event. Public (not just used by run_forever)
        so tests can drive the controller deterministically, one event at
        a time, without a real ARI WebSocket connection."""
        event_type = event.get("type")
        logger.info("ARI event: %s", event_type)
        try:
            if event_type == "StasisStart":
                await self._on_stasis_start(event)
            elif event_type == "RecordingFinished":
                await self._on_recording_finished(event)
            elif event_type in ("StasisEnd", "ChannelHangupRequest", "ChannelDestroyed"):
                await self._on_hangup(event)
        except Exception:  # noqa: BLE001 - one bad event must not kill the loop
            logger.exception("Unhandled error processing ARI event %s", event_type)

    # ---- call lifecycle ----

    async def _on_stasis_start(self, event: dict) -> None:
        channel = event["channel"]
        channel_id = channel["id"]
        if channel_id in self._calls:
            return  # duplicate StasisStart (e.g. reconnect) — ignore

        caller_number = (channel.get("caller") or {}).get("number") or None
        called_extension = (channel.get("dialplan") or {}).get("exten") or self._settings.ai_test_extension

        async with self._session_factory() as db:
            call_row = Call(
                asterisk_channel_id=channel_id,
                direction="inbound",
                caller_number=caller_number,
                called_number=called_extension,
                status="in_progress",
                ai_handled=True,
                started_at=datetime.now(timezone.utc),
            )
            db.add(call_row)
            await db.flush()

            session = await create_session(db, language=self._settings.ai_language, call_id=call_row.id)
            await db.commit()

        state = CallState(channel_id=channel_id, call_row_id=call_row.id, session_id=session.id)
        self._calls[channel_id] = state
        logger.info(
            "Call started: channel=%s call_id=%s session_id=%s", channel_id, call_row.id, session.id
        )

        try:
            await self._ari.answer(channel_id)
        except AriError:
            logger.exception("Failed to answer channel %s", channel_id)
            await self._finish_call(channel_id, status="failed", hangup_cause="answer_failed")
            return

        async with self._session_factory() as db:
            call_row = await db.get(Call, state.call_row_id)
            if call_row is not None:
                call_row.answered_at = datetime.now(timezone.utc)
                await db.commit()

        await self._play_welcome(state)
        await self._start_next_recording(state)

    async def _play_welcome(self, state: CallState) -> None:
        try:
            await self._synthesize_and_play(state, self._settings.ai_welcome_message)
        except (TTSProviderError, AriError):
            logger.exception("Welcome message playback failed for channel %s", state.channel_id)
            # Not fatal — continue into the conversation loop even if the
            # welcome prompt couldn't be played.

    async def _start_next_recording(self, state: CallState) -> None:
        state.recording_seq += 1
        # ARI's record `name` maps directly to a filename under Asterisk's
        # recording spool dir — it does NOT create subdirectories for a
        # name containing "/" (confirmed live: that fails with "recording
        # error: No such file or directory"). Keep it flat; "__" as the
        # separator since it can't appear in a UUID.
        name = f"{_RECORDING_NAME_PREFIX}{state.session_id}__{state.recording_seq}"
        try:
            await self._ari.record(
                state.channel_id,
                name=name,
                max_duration_seconds=self._settings.ai_audio_timeout_seconds * 4,
                max_silence_seconds=self._settings.ai_audio_timeout_seconds,
            )
        except AriError:
            logger.exception("Failed to start recording on channel %s", state.channel_id)
            await self._finish_call(state.channel_id, status="failed", hangup_cause="record_failed")

    async def _on_recording_finished(self, event: dict) -> None:
        recording = event.get("recording", {})
        name: str = recording.get("name", "")
        if not name.startswith(_RECORDING_NAME_PREFIX):
            return  # not one of ours

        session_id_str, _, _seq = name[len(_RECORDING_NAME_PREFIX) :].rpartition("__")
        state = next((s for s in self._calls.values() if str(s.session_id) == session_id_str), None)
        if state is None:
            return  # call already ended/cleaned up

        if self._elapsed_seconds(state) > self._settings.ai_call_timeout_seconds:
            logger.info("Call timeout reached for channel %s", state.channel_id)
            await self._finish_call(state.channel_id, status="completed", hangup_cause="timeout")
            return

        audio_format = recording.get("format", "wav")
        recording_path = Path(self._settings.asterisk_recording_spool_path) / f"{name}.{audio_format}"

        try:
            audio_bytes = recording_path.read_bytes()
        except OSError:
            logger.exception("Could not read recording file %s", recording_path)
            await self._finish_call(state.channel_id, status="failed", hangup_cause="recording_unreadable")
            return

        await self._run_turn(state, audio_bytes)

    async def _run_turn(self, state: CallState, audio_bytes: bytes) -> None:
        reply_text: str | None = None
        needs_safe_error = False

        async with self._session_factory() as db:
            session = await db.get(AISession, state.session_id)
            if session is None or session.status != "active":
                return

            try:
                transcription = await self._stt_provider.transcribe(audio_bytes, language=session.language)
            except STTProviderError as exc:
                logger.warning("STT failed for session %s: %s", session.id, exc)
                needs_safe_error = True
            else:
                try:
                    turn = await handle_text_turn(
                        db,
                        session,
                        transcription.text,
                        settings=self._settings,
                        embedding_provider=self._embedding_provider,
                        llm_provider=self._llm_provider,
                    )
                    reply_text = turn.assistant_message.text
                except ConversationError:
                    pass  # e.g. empty transcription — just prompt again, no error tone
                except (EmbeddingProviderError, LLMProviderError) as exc:
                    logger.error("Provider failure mid-call for session %s: %s", session.id, exc)
                    needs_safe_error = True

        if needs_safe_error:
            await self._speak_safe_error(state)
        elif reply_text:
            try:
                await self._synthesize_and_play(state, reply_text)
            except TTSProviderError as exc:
                logger.warning("TTS failed for session %s: %s", state.session_id, exc)

        await self._start_next_recording(state)

    async def _speak_safe_error(self, state: CallState) -> None:
        try:
            await self._synthesize_and_play(state, _SAFE_ERROR_MESSAGE)
        except (TTSProviderError, AriError):
            logger.exception("Could not play safe-error message on channel %s", state.channel_id)

    async def _synthesize_and_play(self, state: CallState, text: str) -> None:
        if not text.strip():
            return
        result = await self._tts_provider.synthesize(text, language=self._settings.ai_language)

        if result.audio_format == "text/mock":
            # Test mode: no real audio to play. Exercise the Playback API
            # with a placeholder tone so the mechanism itself is verified.
            logger.info("TTS (mock) for channel %s: %r", state.channel_id, text)
            await self._ari.play(state.channel_id, media=_TONE_MEDIA)
            return

        # Production mode: real synthesized audio. Written under the same
        # spool root Asterisk already has permission to read from (see
        # asterisk/etc/dialplan/ai_agent.conf), then played by reference.
        sound_dir = Path(self._settings.asterisk_recording_spool_path).parent / "sounds" / "ai-agent"
        sound_dir.mkdir(parents=True, exist_ok=True)
        file_stem = f"{state.session_id}-{int(time.time() * 1000)}"
        (sound_dir / f"{file_stem}.{result.audio_format}").write_bytes(result.audio_bytes)
        await self._ari.play(state.channel_id, media=f"sound:ai-agent/{file_stem}")

    async def _on_hangup(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        if channel_id is None or channel_id not in self._calls:
            return
        await self._finish_call(channel_id, status="completed", hangup_cause="caller_hangup")

    async def _finish_call(self, channel_id: str, *, status: str, hangup_cause: str) -> None:
        state = self._calls.pop(channel_id, None)
        if state is None:
            return

        async with self._session_factory() as db:
            session = await db.get(AISession, state.session_id)
            if session is not None and session.status == "active":
                await end_session(db, session, status=status)

            call_row = await db.get(Call, state.call_row_id)
            if call_row is not None:
                call_row.status = status
                call_row.hangup_cause = hangup_cause
                call_row.ended_at = datetime.now(timezone.utc)
                call_row.duration_seconds = int(self._elapsed_seconds(state))
                await db.commit()

        try:
            await self._ari.hangup(channel_id)
        except AriError:
            pass  # already gone — fine

        logger.info("Call finished: channel=%s status=%s cause=%s", channel_id, status, hangup_cause)

    @staticmethod
    def _elapsed_seconds(state: CallState) -> float:
        return time.monotonic() - state.started_monotonic
