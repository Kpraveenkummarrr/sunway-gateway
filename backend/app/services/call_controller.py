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

Concurrency: `run_forever` dispatches each ARI event as its own asyncio
task rather than awaiting them one at a time. Without this, a slow turn
(a real provider call) on one call would block event processing — and
therefore progress — for every *other* concurrent call, since they all
share one WebSocket event stream. Per-channel state (`CallState`) is
never shared across channels, so concurrent dispatch doesn't introduce
cross-call races.

Hangup is the one event a channel's own code doesn't cause: StasisEnd /
ChannelDestroyed can arrive while that channel's StasisStart or turn task is
still awaiting a slow provider (e.g. Bhashini TTS for the welcome). Each
CallState therefore carries an `ended` event, set synchronously the moment
the call finishes. In-flight STT, TTS synthesis and playback waits race
against it and are cancelled when it fires, and every ARI media request
(play, record) is guarded by it — so a call that has ended never gets a
late /play or /record. A request that still loses the race by milliseconds
gets ARI 404/409 ("channel not found" / "not in Stasis"), which is handled
as the channel being gone, not as an error.
"""

import asyncio
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
from app.services.audio import (
    AudioFormatError,
    is_effectively_silent,
    is_too_short,
    make_short_beep_wav,
    normalize_for_asterisk_playback,
    read_wav_info,
)
from app.services.conversation import (
    ConversationError,
    ConversationTimeoutError,
    create_session,
    end_session,
    handle_text_turn,
)

logger = get_logger(__name__)

_RECORDING_NAME_PREFIX = "ai-agent__"
# ARI answers "404 Channel not found" or "409 Channel not in a Stasis
# application" once the channel has hung up / left our app.
_CHANNEL_GONE_STATUSES = (404, 409)


def _channel_gone(exc: AriError) -> bool:
    return exc.status_code in _CHANNEL_GONE_STATUSES


class CallState:
    """In-memory bookkeeping for one active call, keyed by Asterisk channel
    id. The database (Call + AISession rows) remains the durable source of
    truth — this is just what the controller needs while the call is live
    (recording sequence number, consecutive-failure count for the retry
    cap, whether the call has ended). Never shared across channels.

    call_row_id / session_id are None only while StasisStart is still
    creating the database rows."""

    __slots__ = (
        "channel_id",
        "call_row_id",
        "session_id",
        "started_monotonic",
        "recording_seq",
        "consecutive_failures",
        "ended",
        "end_status",
        "hangup_cause",
        "records_closed",
    )

    def __init__(
        self, channel_id: str, call_row_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None
    ) -> None:
        self.channel_id = channel_id
        self.call_row_id = call_row_id
        self.session_id = session_id
        self.started_monotonic = time.monotonic()
        self.recording_seq = 0
        self.consecutive_failures = 0
        self.ended = asyncio.Event()
        self.end_status: str | None = None
        self.hangup_cause: str | None = None
        self.records_closed = False

    @property
    def is_active(self) -> bool:
        return not self.ended.is_set()


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
        self._playback_finished: dict[str, asyncio.Event] = {}
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def active_call_count(self) -> int:
        return len(self._calls)

    async def run_forever(self) -> None:
        """Consumes the ARI event stream until the connection closes,
        processing each event as its own task so one slow/stuck call
        can't block progress on other concurrent calls (see module
        docstring)."""
        async for event in self._ari.events():
            task = asyncio.create_task(self.dispatch_event(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

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
            elif event_type == "PlaybackFinished":
                self._on_playback_finished(event)
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

        # Registered before the first await, so a hangup that arrives while
        # the database rows are being created still ends this call.
        state = CallState(channel_id=channel_id)
        self._calls[channel_id] = state

        try:
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
        except Exception:
            self._calls.pop(channel_id, None)
            state.ended.set()
            raise

        state.call_row_id = call_row.id
        state.session_id = session.id
        logger.info(
            "Call started: channel=%s call_id=%s session_id=%s", channel_id, call_row.id, session.id
        )
        if not state.is_active:
            # Hung up while the rows were being created: _finish_call already
            # ran but couldn't close rows that didn't exist yet.
            logger.info("Channel %s hung up before it was answered", channel_id)
            await self._close_records(state)
            return

        try:
            await self._ari.answer(channel_id)
        except AriError as exc:
            if _channel_gone(exc) or not state.is_active:
                logger.info("Channel %s hung up before it could be answered", channel_id)
                await self._finish_call(channel_id, status="completed", hangup_cause="caller_hangup", channel_gone=True)
            else:
                logger.exception("Failed to answer channel %s", channel_id)
                await self._finish_call(channel_id, status="failed", hangup_cause="answer_failed")
            return

        async with self._session_factory() as db:
            call_row = await db.get(Call, state.call_row_id)
            if call_row is not None:
                call_row.answered_at = datetime.now(timezone.utc)
                await db.commit()

        await self._play_welcome(state)
        if state.is_active:  # the caller may hang up during the welcome
            await self._start_next_recording(state)

    async def _play_welcome(self, state: CallState) -> None:
        try:
            await self._synthesize_and_play(state, self._settings.caller_message("welcome"))
        except (TTSProviderError, AriError):
            logger.exception("Welcome message playback failed for channel %s", state.channel_id)
            # Not fatal — continue into the conversation loop even if the
            # welcome prompt couldn't be played.

    async def _start_next_recording(self, state: CallState) -> None:
        if not state.is_active:
            return
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
        except AriError as exc:
            if _channel_gone(exc):
                logger.info("Recording not started on channel %s: channel already gone", state.channel_id)
                await self._finish_call(state.channel_id, status="completed", hangup_cause="caller_hangup", channel_gone=True)
                return
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

        # Skip silence/too-short audio without burning an STT call. Best
        # effort: if the bytes aren't a parseable WAV (e.g. a test double
        # feeding in arbitrary bytes), just fall through to STT as before
        # rather than failing the turn on a format check.
        try:
            wav_info = read_wav_info(audio_bytes)
        except AudioFormatError:
            wav_info = None

        if wav_info is not None and (is_too_short(wav_info) or is_effectively_silent(audio_bytes)):
            logger.info(
                "Turn %d on channel %s was silence/too-short (%.2fs) — skipping STT",
                state.recording_seq,
                state.channel_id,
                wav_info.duration_seconds,
            )
            if not await self._register_failure(state):
                await self._start_next_recording(state)
            return

        await self._run_turn(state, audio_bytes)

    async def _run_turn(self, state: CallState, audio_bytes: bytes) -> None:
        reply_text: str | None = None
        needs_safe_error = False
        succeeded = False
        if not state.is_active:
            return

        async with self._session_factory() as db:
            session = await db.get(AISession, state.session_id)
            if session is None or session.status != "active":
                return

            stt_started = time.monotonic()
            try:
                completed, transcription = await self._unless_call_ends(
                    state, self._stt_provider.transcribe(audio_bytes, language=session.language)
                )
            except STTProviderError as exc:
                logger.warning("STT failed for session %s: %s", session.id, exc)
                needs_safe_error = True
            else:
                if not completed:
                    logger.info("Call on channel %s ended during STT — turn abandoned", state.channel_id)
                    return
                logger.info(
                    "STT ok for session %s: %d chars in %dms",
                    session.id,
                    len(transcription.text),
                    int((time.monotonic() - stt_started) * 1000),
                )
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
                    succeeded = True
                except ConversationTimeoutError as exc:
                    logger.error("AI turn timed out for session %s: %s", session.id, exc)
                    needs_safe_error = True
                except ConversationError:
                    pass  # e.g. empty transcription — just prompt again, no error tone
                except (EmbeddingProviderError, LLMProviderError) as exc:
                    logger.error("Provider failure mid-call for session %s: %s", session.id, exc)
                    needs_safe_error = True

        if succeeded:
            state.consecutive_failures = 0
        elif needs_safe_error and await self._register_failure(state):
            return  # too many consecutive failures — call already ended

        if needs_safe_error:
            await self._speak_safe_error(state)
        elif reply_text:
            try:
                await self._synthesize_and_play(state, reply_text)
            except TTSProviderError as exc:
                logger.warning("TTS failed for session %s: %s", state.session_id, exc)

        await self._start_next_recording(state)

    async def _register_failure(self, state: CallState) -> bool:
        """Tracks a "bad" turn (silence, STT failure, provider failure).
        Returns True if the call was ended because the consecutive-failure
        cap was hit — callers should stop their own turn processing when
        this returns True."""
        if not state.is_active:
            return True
        state.consecutive_failures += 1
        if state.consecutive_failures < self._settings.ai_max_consecutive_failures:
            return False

        logger.info(
            "Channel %s hit %d consecutive failed turns — ending call",
            state.channel_id,
            state.consecutive_failures,
        )
        try:
            await self._synthesize_and_play(state, self._settings.caller_message("goodbye"))
        except (TTSProviderError, AriError):
            logger.exception("Could not play goodbye message on channel %s", state.channel_id)
        await self._finish_call(state.channel_id, status="completed", hangup_cause="too_many_failed_turns")
        return True

    async def _speak_safe_error(self, state: CallState) -> None:
        try:
            await self._synthesize_and_play(state, self._settings.caller_message("error"))
        except (TTSProviderError, AriError):
            logger.exception("Could not play safe-error message on channel %s", state.channel_id)

    async def _synthesize_and_play(self, state: CallState, text: str) -> None:
        if not text.strip():
            return
        if not state.is_active:
            logger.info("Skipping TTS for channel %s: call already ended", state.channel_id)
            return
        tts_started = time.monotonic()
        completed, result = await self._unless_call_ends(
            state, self._tts_provider.synthesize(text, language=self._settings.ai_language)
        )
        if not completed:
            logger.info(
                "Call on channel %s ended during TTS synthesis — synthesis cancelled, nothing played",
                state.channel_id,
            )
            return
        tts_latency_ms = int((time.monotonic() - tts_started) * 1000)

        if result.audio_format == "text/mock":
            # Test mode: no real audio to play. Play a short, genuinely
            # bounded generated beep so the Playback API mechanism is
            # still exercised, and log what would have been spoken.
            logger.info("TTS (mock) for channel %s: %r", state.channel_id, text)
            await self._write_and_play_wav(state, make_short_beep_wav(), audio_format="wav")
            return

        # Production mode: real synthesized audio at the vendor's native rate
        # (OpenAI 24kHz, Bhashini 48kHz), but Asterisk's format_wav module
        # only plays 8kHz/16kHz (confirmed via `module show like format`).
        # Every real clip is normalized; one that can't be is a TTS failure
        # rather than something played as-is, which the caller would hear as
        # silence or noise.
        if result.audio_format != "wav":
            raise TTSProviderError(f"Unsupported TTS audio format for Asterisk playback: {result.audio_format!r}")
        try:
            source_rate = read_wav_info(result.audio_bytes).sample_rate
            audio_bytes = normalize_for_asterisk_playback(result.audio_bytes)
        except AudioFormatError as exc:
            raise TTSProviderError(f"Could not normalize TTS audio for playback: {exc}") from exc

        logger.info(
            "TTS ok for channel %s: %d chars -> %dHz source, %d bytes 8kHz playback, %dms",
            state.channel_id,
            len(text),
            source_rate,
            len(audio_bytes),
            tts_latency_ms,
        )
        await self._write_and_play_wav(state, audio_bytes, audio_format="wav")

    async def _write_and_play_wav(self, state: CallState, audio_bytes: bytes, *, audio_format: str) -> None:
        # Written under the Asterisk spool root, then played by ABSOLUTE path
        # (extension omitted). A relative "sound:ai-agent/..." is resolved
        # against Asterisk's data dir (/usr/share/asterisk/sounds), not the
        # spool — confirmed live: that fails with "does not exist in any
        # format" and ARI reports PlaybackFinished instantly, i.e. silence.
        if not state.is_active:
            logger.info("Skipping playback on channel %s: call already ended", state.channel_id)
            return
        sound_dir = Path(self._settings.asterisk_recording_spool_path).parent / "sounds" / "ai-agent"
        sound_dir.mkdir(parents=True, exist_ok=True)
        file_stem = f"{state.session_id}-{int(time.time() * 1000)}"
        (sound_dir / f"{file_stem}.{audio_format}").write_bytes(audio_bytes)
        await self._play_and_wait(state, media=f"sound:{(sound_dir / file_stem).as_posix()}")

    async def _play_and_wait(self, state: CallState, *, media: str) -> None:
        """Starts ARI playback and waits for it to actually finish before
        returning, so the next recording doesn't start (and potentially
        capture the AI's own voice) while audio is still playing out.
        Never issues /play for a call that has ended, and stops waiting as
        soon as the call ends."""
        if not state.is_active:
            logger.info("Skipping playback on channel %s: call already ended", state.channel_id)
            return
        try:
            playback = await self._ari.play(state.channel_id, media=media)
        except AriError as exc:
            if not _channel_gone(exc):
                raise
            # Hung up in the instant between the liveness check and the request.
            logger.info(
                "Playback not started on channel %s: channel already gone (HTTP %s)",
                state.channel_id,
                exc.status_code,
            )
            await self._finish_call(state.channel_id, status="completed", hangup_cause="caller_hangup", channel_gone=True)
            return
        playback_id = playback.get("id")
        if not playback_id:
            return  # can't track completion — proceed rather than hang

        finished = asyncio.Event()
        self._playback_finished[playback_id] = finished
        try:
            completed, _ = await self._unless_call_ends(
                state, finished.wait(), timeout=self._settings.provider_timeout_seconds
            )
            if not completed:
                logger.info("Call on channel %s ended during playback %s", state.channel_id, playback_id)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for playback %s to finish on channel %s", playback_id, state.channel_id)
        finally:
            self._playback_finished.pop(playback_id, None)

    async def _unless_call_ends(self, state: CallState, awaitable, *, timeout: float | None = None):
        """Awaits `awaitable` unless the call ends first, in which case the
        work is cancelled and (False, None) is returned. Otherwise returns
        (True, result); the work's own exception propagates, and
        asyncio.TimeoutError is raised if `timeout` elapses first."""
        work = asyncio.ensure_future(awaitable)
        ended = asyncio.ensure_future(state.ended.wait())
        try:
            done, _ = await asyncio.wait({work, ended}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (work, ended):
                if not task.done():
                    task.cancel()
        if work in done:
            return True, work.result()
        await asyncio.wait({work})  # let the cancelled work finish unwinding (e.g. close its HTTP request)
        if ended in done:
            return False, None
        raise asyncio.TimeoutError

    def _on_playback_finished(self, event: dict) -> None:
        playback_id = (event.get("playback") or {}).get("id")
        finished = self._playback_finished.get(playback_id) if playback_id else None
        if finished is not None:
            finished.set()

    async def _on_hangup(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        if channel_id is None or channel_id not in self._calls:
            return
        await self._finish_call(channel_id, status="completed", hangup_cause="caller_hangup", channel_gone=True)

    async def _finish_call(
        self, channel_id: str, *, status: str, hangup_cause: str, channel_gone: bool = False
    ) -> None:
        """Ends the call once. `channel_gone` means Asterisk already hung the
        channel up (hangup event, or ARI 404/409), so no hangup request is sent."""
        state = self._calls.pop(channel_id, None)
        if state is None:
            return

        # Set before any await, so every in-flight task for this call sees it
        # immediately: pending STT/TTS/playback waits are cancelled and no
        # further ARI media request is made for this channel.
        state.end_status = status
        state.hangup_cause = hangup_cause
        state.ended.set()

        if state.session_id is not None:
            await self._close_records(state)
        # else: StasisStart is still creating this call's rows and closes them once they exist.

        if not channel_gone:
            try:
                await self._ari.hangup(channel_id)
            except AriError:
                pass  # hung up concurrently — fine

        logger.info("Call finished: channel=%s status=%s cause=%s", channel_id, status, hangup_cause)

    async def _close_records(self, state: CallState) -> None:
        if state.records_closed:
            return
        state.records_closed = True

        async with self._session_factory() as db:
            session = await db.get(AISession, state.session_id)
            if session is not None and session.status == "active":
                await end_session(db, session, status=state.end_status)

            call_row = await db.get(Call, state.call_row_id)
            if call_row is not None:
                call_row.status = state.end_status
                call_row.hangup_cause = state.hangup_cause
                call_row.ended_at = datetime.now(timezone.utc)
                call_row.duration_seconds = int(self._elapsed_seconds(state))
                await db.commit()

    @staticmethod
    def _elapsed_seconds(state: CallState) -> float:
        return time.monotonic() - state.started_monotonic
