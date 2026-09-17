"""AI call controller: bridges Asterisk ARI call events to the Phase 5
conversation service.

Responsibility split (per docs/architecture.md):
    Asterisk owns  — SIP, RTP, channel lifecycle, answering, media playback/recording.
    This module owns — mapping a channel to an AI session, driving the
        turn loop (record -> conversation service -> synthesize -> play),
        and cleaning up on hangup/error/timeout.

No AI business logic (RAG, LLM, prompt policy) lives here — all of that
stays in app.services.conversation. This module only decides *when* to
call it and how to get audio in and out of Asterisk.

TEST MODE vs PRODUCTION MEDIA MODE (see docs/architecture.md):
When TTS_PROVIDER=mock, synthesized "audio" is placeholder bytes that
Asterisk cannot play as real speech — the controller plays a short tone
instead so the call-control mechanism (the Playback API call itself) is
still exercised. TTS_PROVIDER is never silently swapped; this only affects
how already-mock output is handled downstream.

LATENCY: fixed caller phrases (welcome/error/goodbye) are synthesized once
and reused by every call (pre-rendered at worker startup). Replies are
split into sentence chunks: the first chunk starts playing as soon as it is
synthesized while the next one is synthesized in parallel, so the caller
hears the answer after one short TTS request instead of after the whole
reply. Each turn logs one "Turn timing" line with every stage's latency.

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
as the channel being gone, not as an error. Shared phrase rendering is the
one exception to cancellation: it keeps going (shielded) for the next call.
"""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
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
    ASTERISK_SAMPLE_RATE,
    AudioFormatError,
    is_effectively_silent,
    is_too_short,
    make_short_beep_wav,
    measure_levels,
    prepare_tts_for_playback,
    read_wav_info,
)
from app.services.system_config import effective_settings
from app.services.spoken_text import spoken_text
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
_CALLER_MESSAGE_KINDS = ("welcome", "error", "goodbye")

_SENTENCE_BREAK = re.compile(r"(?<=[।॥?!])\s+|(?<=\.)\s+")
_SENTENCE_END = ("।", "॥", "?", "!", ".")
MIN_SPEECH_CHUNK_CHARS = 40
# The first chunk is allowed to be shorter than the rest. Synthesis time
# scales with length, and nothing is playing yet, so the opening sentence is
# the one place where a smaller chunk directly shortens the silence the
# caller hears. Later chunks keep the larger minimum: they are synthesized
# while earlier audio is still playing, where small chunks buy nothing and
# risk choppy prosody.
FIRST_SPEECH_CHUNK_CHARS = 24


def _channel_gone(exc: AriError) -> bool:
    return exc.status_code in _CHANNEL_GONE_STATUSES


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def speech_chunks(
    text: str,
    *,
    truncated: bool = False,
    min_chars: int = MIN_SPEECH_CHUNK_CHARS,
    first_min_chars: int = FIRST_SPEECH_CHUNK_CHARS,
) -> list[str]:
    """Splits a reply into sentence-based chunks for incremental TTS.
    The text is first reduced to what can actually be spoken (see
    app.services.spoken_text) so no markup reaches the TTS engine.
    Sentences shorter than `min_chars` are merged forward so TTS isn't
    called for fragments. When the LLM hit its token limit (`truncated`), a
    trailing unfinished sentence is dropped rather than spoken cut off."""
    text = spoken_text(text)
    sentences = [s.strip() for s in _SENTENCE_BREAK.split(text.strip()) if s.strip()]
    if truncated and len(sentences) > 1 and not sentences[-1].endswith(_SENTENCE_END):
        sentences.pop()

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        current = f"{current} {sentence}".strip()
        if len(current) >= (first_min_chars if not chunks else min_chars):
            chunks.append(current)
            current = ""
    if current:
        # A leftover tail is merged back only when it is too small to be worth
        # its own TTS request. Using the larger `min_chars` here would fold a
        # perfectly speakable closing sentence into the first chunk, which
        # delays the first audio the caller hears by exactly its length.
        if chunks and len(current) < first_min_chars:
            chunks[-1] = f"{chunks[-1]} {current}"
        else:
            chunks.append(current)
    return chunks


@dataclass(frozen=True)
class SpeechClip:
    """Telephone-ready audio (8kHz mono 16-bit WAV) plus how long it took."""

    audio: bytes
    tts_ms: int
    prep_ms: int
    seconds: float


class CallState:
    """In-memory bookkeeping for one active call, keyed by Asterisk channel
    id. The database (Call + AISession rows) remains the durable source of
    truth — this is just what the controller needs while the call is live
    (recording sequence number, consecutive-failure count for the retry
    cap, seconds without caller speech, whether the call has ended). Never
    shared across channels.

    call_row_id / session_id are None only while StasisStart is still
    creating the database rows."""

    __slots__ = (
        "channel_id",
        "call_row_id",
        "session_id",
        "started_monotonic",
        "recording_started_monotonic",
        "recording_seq",
        "consecutive_failures",
        "silent_seconds",
        "ended",
        "end_status",
        "hangup_cause",
        "records_closed",
        "active_playback_id",
        "barge_in_generation",
        "beeped_generation",
    )

    def __init__(
        self, channel_id: str, call_row_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None
    ) -> None:
        self.channel_id = channel_id
        self.call_row_id = call_row_id
        self.session_id = session_id
        self.started_monotonic = time.monotonic()
        self.recording_started_monotonic: float | None = None
        self.recording_seq = 0
        self.consecutive_failures = 0
        self.silent_seconds = 0.0
        self.ended = asyncio.Event()
        self.end_status: str | None = None
        self.hangup_cause: str | None = None
        self.records_closed = False
        self.active_playback_id: str | None = None
        # Each caller-speech event during playback advances this generation.
        # Reply playback uses it to discard prefetched chunks after an
        # interruption.
        self.barge_in_generation = 0
        # Tracks which barge-in generation the last recording start accounted
        # for, so the turn right after an interruption suppresses the beep.
        self.beeped_generation = 0

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
        # Environment defaults; admin-panel overrides are applied on top of
        # these at the start of each call (see _refresh_settings).
        self._base_settings = settings
        self._settings = settings
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._llm_provider = llm_provider
        self._stt_provider = stt_provider
        self._tts_provider = tts_provider
        self._calls: dict[str, CallState] = {}
        self._playback_finished: dict[str, asyncio.Event] = {}
        self._background_tasks: set[asyncio.Task] = set()
        # Rendered fixed caller phrases, keyed by text; shared by all calls.
        self._phrase_clips: dict[str, asyncio.Future] = {}

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

    async def prewarm_caller_messages(self) -> None:
        """Renders the welcome/error/goodbye phrases once so no call waits
        on TTS for them. Failures are logged, not raised — a call that finds
        no rendered phrase just renders it itself."""
        for kind in _CALLER_MESSAGE_KINDS:
            started = time.monotonic()
            try:
                clip = await self._phrase_clip(self._settings.caller_message(kind))
            except TTSProviderError as exc:
                logger.warning("Could not pre-render %s message: %s", kind, exc)
                continue
            logger.info(
                "Pre-rendered %s message: %.1fs audio in %dms (tts=%dms prep=%dms)",
                kind,
                clip.seconds,
                _ms_since(started),
                clip.tts_ms,
                clip.prep_ms,
            )

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
            elif event_type == "ChannelTalkingStarted":
                await self._on_talking_started(event)
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

        await self._refresh_settings()
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

        self._start_background_diagnostic(self._log_media_formats(channel_id))

        async with self._session_factory() as db:
            call_row = await db.get(Call, state.call_row_id)
            if call_row is not None:
                call_row.answered_at = datetime.now(timezone.utc)
                await db.commit()

        await self._play_welcome(state)
        if state.is_active:  # the caller may hang up during the welcome
            await self._start_next_recording(state)

    async def _refresh_settings(self) -> None:
        """Picks up settings changed in the admin panel, so a change takes
        effect on the next call without restarting the worker. A failure
        here must never fail the call — the previous settings stay in use."""
        try:
            async with self._session_factory() as db:
                updated = await effective_settings(db, self._base_settings)
        except Exception:  # noqa: BLE001 - config refresh is best effort
            logger.exception("Could not refresh runtime settings; keeping the current ones")
            return
        if updated != self._settings:
            logger.info("Runtime settings changed in the admin panel — applied to this call")
            # Re-rendered phrases: a changed welcome/speed must not serve stale audio.
            self._phrase_clips.clear()
        self._settings = updated

    async def _play_welcome(self, state: CallState) -> None:
        try:
            await self._play_caller_message(state, "welcome")
        except (TTSProviderError, AriError):
            logger.exception("Welcome message playback failed for channel %s", state.channel_id)
            # Not fatal — continue into the conversation loop even if the
            # welcome prompt couldn't be played.

    async def _start_next_recording(self, state: CallState) -> None:
        if not state.is_active:
            return
        after_barge_in = state.barge_in_generation > state.beeped_generation
        state.beeped_generation = state.barge_in_generation
        state.recording_seq += 1
        state.recording_started_monotonic = time.monotonic()
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
                max_duration_seconds=self._settings.ai_max_turn_seconds,
                max_silence_seconds=self._settings.ai_end_of_speech_silence_seconds,
                # After a barge-in the caller is already mid-sentence, so a
                # beep would land on top of their speech.
                beep=self._settings.ai_record_beep and not after_barge_in,
            )
        except AriError as exc:
            if _channel_gone(exc):
                logger.info("Recording not started on channel %s: channel already gone", state.channel_id)
                await self._finish_call(state.channel_id, status="completed", hangup_cause="caller_hangup", channel_gone=True)
                return
            logger.exception("Failed to start recording on channel %s", state.channel_id)
            await self._finish_call(state.channel_id, status="failed", hangup_cause="record_failed")

    async def _on_recording_finished(self, event: dict) -> None:
        turn_started = time.monotonic()
        recording = event.get("recording", {})
        name: str = recording.get("name", "")
        if not name.startswith(_RECORDING_NAME_PREFIX):
            return  # not one of ours

        session_id_str, _, _seq = name[len(_RECORDING_NAME_PREFIX) :].rpartition("__")
        state = next((s for s in self._calls.values() if str(s.session_id) == session_id_str), None)
        if state is None:
            return  # call already ended/cleaned up
        capture_ms = (
            _ms_since(state.recording_started_monotonic)
            if state.recording_started_monotonic is not None
            else None
        )

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
            state.silent_seconds += wav_info.duration_seconds
            logger.info(
                "Turn %d on channel %s had no speech (%.2fs) — %.1fs of %ds without input",
                state.recording_seq,
                state.channel_id,
                wav_info.duration_seconds,
                state.silent_seconds,
                self._settings.ai_no_input_timeout_seconds,
            )
            if state.silent_seconds >= self._settings.ai_no_input_timeout_seconds:
                await self._end_call_with_goodbye(state, hangup_cause="no_input")
            else:
                await self._start_next_recording(state)
            return

        if wav_info is not None:
            levels = measure_levels(audio_bytes)
            logger.info(
                "Caller audio channel=%s turn=%d rate=%dHz channels=%d duration=%.2fs "
                "peak=%.1fdBFS rms=%.1fdBFS noise_floor=%.1fdBFS clipped=%.4f",
                state.channel_id,
                state.recording_seq,
                wav_info.sample_rate,
                wav_info.channels,
                levels.duration_seconds,
                levels.peak_dbfs,
                levels.rms_dbfs,
                levels.noise_floor_dbfs,
                levels.clipped_ratio,
            )

        state.silent_seconds = 0.0
        await self._run_turn(
            state,
            audio_bytes,
            turn_started=turn_started,
            audio_seconds=wav_info.duration_seconds if wav_info is not None else 0.0,
            capture_ms=capture_ms,
        )

    async def _run_turn(
        self,
        state: CallState,
        audio_bytes: bytes,
        *,
        turn_started: float | None = None,
        audio_seconds: float = 0.0,
        capture_ms: int | None = None,
    ) -> None:
        turn_started = turn_started if turn_started is not None else time.monotonic()
        reply_text: str | None = None
        finish_reason: str | None = None
        stage_timings: dict[str, int] = {}
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
                stage_timings["asr"] = _ms_since(stt_started)
                logger.info(
                    "STT ok for session %s: %d chars in %dms",
                    session.id,
                    len(transcription.text),
                    stage_timings["asr"],
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
                    finish_reason = turn.finish_reason
                    stage_timings.update(turn.timings)
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
            reply_started = time.monotonic()
            pre_reply_ms = _ms_since(turn_started)
            try:
                speech = await self._speak_reply(state, reply_text, truncated=finish_reason == "length")
            except TTSProviderError as exc:
                logger.warning("TTS failed for session %s: %s", state.session_id, exc)
            else:
                first_audio_ms = speech.pop("first_audio_ms", None)
                stage_timings["tts"] = speech["tts_ms"]
                stage_timings["audio"] = speech["audio_ms"]
                logger.info(
                    "Turn timing channel=%s turn=%d audio=%.1fs vad_ms=%sms asr_ms=%sms "
                    "embedding_ms=%sms retrieval_ms=%sms context_ms=%sms llm_ms=%sms "
                    "tts_ms=%sms audio_ms=%sms reply_chars=%d chunks=%d first_audio_ms=%sms "
                    "tts_first_ms=%sms prep_first_ms=%sms speaking_ms=%d total_ms=%d",
                    state.channel_id,
                    state.recording_seq,
                    audio_seconds,
                    capture_ms if capture_ms is not None else "-",
                    stage_timings.get("asr", "-"),
                    stage_timings.get("embedding", "-"),
                    stage_timings.get("retrieval", "-"),
                    stage_timings.get("context", "-"),
                    stage_timings.get("llm", "-"),
                    stage_timings.get("tts", "-"),
                    stage_timings.get("audio", "-"),
                    len(reply_text),
                    speech["chunks"],
                    pre_reply_ms + first_audio_ms if first_audio_ms is not None else "-",
                    speech["tts_first_ms"] if speech["tts_first_ms"] is not None else "-",
                    speech["prep_first_ms"] if speech["prep_first_ms"] is not None else "-",
                    _ms_since(reply_started),
                    _ms_since(turn_started),
                )

        await self._start_next_recording(state)

    async def _register_failure(self, state: CallState) -> bool:
        """Tracks a failed turn (STT or provider failure). Returns True if
        the call was ended because the consecutive-failure cap was hit —
        callers should stop their own turn processing when this returns
        True."""
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
        await self._end_call_with_goodbye(state, hangup_cause="too_many_failed_turns")
        return True

    async def _end_call_with_goodbye(self, state: CallState, *, hangup_cause: str) -> None:
        try:
            await self._play_caller_message(state, "goodbye")
        except (TTSProviderError, AriError):
            logger.exception("Could not play goodbye message on channel %s", state.channel_id)
        await self._finish_call(state.channel_id, status="completed", hangup_cause=hangup_cause)

    async def _speak_safe_error(self, state: CallState) -> None:
        try:
            await self._play_caller_message(state, "error")
        except (TTSProviderError, AriError):
            logger.exception("Could not play safe-error message on channel %s", state.channel_id)

    # ---- speech ----

    def _start_background_diagnostic(self, awaitable) -> None:
        """Run read-only diagnostics without extending caller latency."""
        task = asyncio.create_task(awaitable)
        self._background_tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:  # noqa: BLE001 - diagnostics must never affect call control
                logger.exception("Background voice diagnostic failed")

        task.add_done_callback(completed)

    async def _log_media_formats(self, channel_id: str) -> None:
        """Log the actual channel formats selected by Asterisk.

        ``audionativeformat`` is the negotiated endpoint codec; read/write
        formats show any translation Asterisk is applying around playback.
        Diagnostics are best-effort and must never fail a call.
        """
        variables = {
            "native": "CHANNEL(audionativeformat)",
            "read": "CHANNEL(audioreadformat)",
            "write": "CHANNEL(audiowriteformat)",
        }

        async def read(variable: str) -> str:
            try:
                return await self._ari.get_channel_variable(channel_id, variable)
            except AriError as exc:
                if not _channel_gone(exc):
                    logger.warning("Could not read %s for channel %s: %s", variable, channel_id, exc)
                return "unavailable"

        values = await asyncio.gather(*(read(variable) for variable in variables.values()))
        formats = dict(zip(variables, values, strict=True))
        logger.info(
            "Telephony media formats: channel=%s native=%s read=%s write=%s playback_file=slin/%dHz",
            channel_id,
            formats["native"] or "unknown",
            formats["read"] or "unknown",
            formats["write"] or "unknown",
            ASTERISK_SAMPLE_RATE,
        )

    async def _log_rtp_statistics(self, channel_id: str) -> None:
        """Log Asterisk RTP/RTCP counters after playback, when available."""
        try:
            stats = await self._ari.get_rtp_statistics(channel_id)
        except AriError as exc:
            if not _channel_gone(exc):
                logger.warning("RTP statistics unavailable for channel %s: %s", channel_id, exc)
            return
        logger.info(
            "RTP statistics: channel=%s txcount=%s rxcount=%s txploss=%s rxploss=%s "
            "txjitter=%s rxjitter=%s rtt=%s",
            channel_id,
            stats.get("txcount", "-"),
            stats.get("rxcount", "-"),
            stats.get("txploss", "-"),
            stats.get("rxploss", "-"),
            stats.get("txjitter", "-"),
            stats.get("rxjitter", "-"),
            stats.get("rtt", "-"),
        )

    async def _render_speech(self, text: str) -> SpeechClip:
        """TTS + telephone preparation for one piece of text, independent of
        any call. Raises TTSProviderError."""
        tts_started = time.monotonic()
        result = await self._tts_provider.synthesize(text, language=self._settings.ai_language)
        tts_ms = _ms_since(tts_started)

        if result.audio_format == "text/mock":
            # Test mode: no real audio to play. Play a short, genuinely
            # bounded generated beep so the Playback API mechanism is
            # still exercised, and log what would have been spoken.
            logger.info("TTS (mock): %r", text)
            beep = make_short_beep_wav()
            return SpeechClip(audio=beep, tts_ms=tts_ms, prep_ms=0, seconds=read_wav_info(beep).duration_seconds)

        # Real synthesized audio arrives at the vendor's native rate (OpenAI
        # 24kHz, Bhashini 48kHz); Asterisk's format_wav only plays 8kHz/16kHz
        # (confirmed via `module show like format`) and the GSM leg is 8kHz.
        # Every real clip is prepared; one that can't be is a TTS failure
        # rather than something played as-is, which the caller would hear as
        # silence or noise.
        if result.audio_format != "wav":
            raise TTSProviderError(f"Unsupported TTS audio format for Asterisk playback: {result.audio_format!r}")
        prep_started = time.monotonic()
        try:
            source_info = read_wav_info(result.audio_bytes)
            source_levels = measure_levels(result.audio_bytes)
            audio = await asyncio.to_thread(
                prepare_tts_for_playback, result.audio_bytes, speed=self._settings.ai_tts_speed
            )
            seconds = read_wav_info(audio).duration_seconds
        except AudioFormatError as exc:
            raise TTSProviderError(f"Could not prepare TTS audio for playback: {exc}") from exc
        prep_ms = _ms_since(prep_started)
        levels = measure_levels(audio)
        words = len(text.split())
        words_per_minute = (words / seconds * 60.0) if seconds > 0 else 0.0
        source_words_per_minute = (
            words / source_info.duration_seconds * 60.0 if source_info.duration_seconds > 0 else 0.0
        )
        model = (
            self._settings.bhashini_tts_service_id
            if self._settings.tts_provider.strip().lower() == "bhashini"
            else self._settings.tts_model or self._settings.tts_provider
        )
        voice = (
            self._settings.bhashini_tts_gender
            if self._settings.tts_provider.strip().lower() == "bhashini"
            else self._settings.tts_voice or "default"
        )

        logger.info(
            "TTS source: provider=%s model=%s voice=%s chars=%d words=%d rate=%dHz channels=%d bits=%d "
            "duration=%.2fs speech_rate=%.1f words/min peak=%.1fdBFS rms=%.1fdBFS noise_floor=%.1fdBFS "
            "leading=%.3fs trailing=%.3fs clipped=%.4f tts=%dms",
            self._settings.tts_provider,
            model,
            voice,
            len(text),
            words,
            source_info.sample_rate,
            source_info.channels,
            source_info.sample_width * 8,
            source_info.duration_seconds,
            source_words_per_minute,
            source_levels.peak_dbfs,
            source_levels.rms_dbfs,
            source_levels.noise_floor_dbfs,
            source_levels.leading_silence_seconds,
            source_levels.trailing_silence_seconds,
            source_levels.clipped_ratio,
            tts_ms,
        )
        logger.info(
            "TTS prepared: target=%dHz duration=%.2fs tempo=%.2fx speech_rate=%.1f words/min "
            "peak=%.1fdBFS rms=%.1fdBFS peak_delta=%.1fdB leading=%.3fs trailing=%.3fs clipped=%.4f prep=%dms",
            ASTERISK_SAMPLE_RATE,
            seconds,
            self._settings.ai_tts_speed,
            words_per_minute,
            levels.peak_dbfs,
            levels.rms_dbfs,
            levels.peak_dbfs - source_levels.peak_dbfs,
            levels.leading_silence_seconds,
            levels.trailing_silence_seconds,
            levels.clipped_ratio,
            prep_ms,
        )
        return SpeechClip(audio=audio, tts_ms=tts_ms, prep_ms=prep_ms, seconds=seconds)

    def _phrase_clip(self, text: str) -> asyncio.Future:
        """The shared rendering of a fixed caller phrase, started on first use.
        A failed or cancelled rendering is replaced on the next request."""
        future = self._phrase_clips.get(text)
        if future is None or (future.done() and (future.cancelled() or future.exception() is not None)):
            future = asyncio.ensure_future(self._render_speech(text))
            future.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark exception retrieved
            self._phrase_clips[text] = future
        return future

    async def _play_caller_message(self, state: CallState, kind: str) -> None:
        text = self._settings.caller_message(kind)
        if not text.strip() or not state.is_active:
            return
        # Shielded: a caller hanging up stops *this call's* wait, but the
        # shared rendering continues for the next call.
        completed, clip = await self._unless_call_ends(state, asyncio.shield(self._phrase_clip(text)))
        if not completed:
            logger.info("Call on channel %s ended while the %s message was rendering", state.channel_id, kind)
            return
        await self._write_and_play_wav(state, clip.audio, audio_format="wav")

    async def _synthesize_clip(self, state: CallState, text: str) -> SpeechClip | None:
        """Renders `text` for this call; None if there is nothing to say or
        the call ended first (the synthesis is then cancelled)."""
        if not text.strip():
            return None
        if not state.is_active:
            logger.info("Skipping TTS for channel %s: call already ended", state.channel_id)
            return None
        completed, clip = await self._unless_call_ends(state, self._render_speech(text))
        if not completed:
            logger.info(
                "Call on channel %s ended during TTS synthesis — synthesis cancelled, nothing played",
                state.channel_id,
            )
            return None
        return clip

    async def _synthesize_and_play(self, state: CallState, text: str) -> None:
        clip = await self._synthesize_clip(state, text)
        if clip is not None:
            await self._write_and_play_wav(state, clip.audio, audio_format="wav")

    async def _speak_reply(self, state: CallState, text: str, *, truncated: bool = False) -> dict:
        """Plays a reply chunk by chunk: each chunk starts as soon as it is
        synthesized, while the next chunk is already being synthesized.
        Returns timing stats; `first_audio_ms` is None if nothing played."""
        chunks = speech_chunks(text, truncated=truncated)
        stats: dict = {
            "chunks": len(chunks),
            "first_audio_ms": None,
            "tts_first_ms": None,
            "prep_first_ms": None,
            "tts_ms": 0,
            "audio_ms": 0,
        }
        if not chunks or not state.is_active:
            return stats

        started = time.monotonic()
        pending = asyncio.ensure_future(self._render_speech(chunks[0]))
        barge_in_generation = state.barge_in_generation
        try:
            for index in range(len(chunks)):
                completed, clip = await self._unless_call_ends(state, pending)
                if not completed:
                    logger.info(
                        "Call on channel %s ended during reply synthesis — %d of %d chunks unplayed",
                        state.channel_id,
                        len(chunks) - index,
                        len(chunks),
                    )
                    return stats
                pending = (
                    asyncio.ensure_future(self._render_speech(chunks[index + 1])) if index + 1 < len(chunks) else None
                )
                stats["tts_ms"] += clip.tts_ms
                stats["audio_ms"] += clip.prep_ms
                if stats["first_audio_ms"] is None:
                    stats.update(first_audio_ms=_ms_since(started), tts_first_ms=clip.tts_ms, prep_first_ms=clip.prep_ms)
                await self._write_and_play_wav(state, clip.audio, audio_format="wav")
                if not state.is_active or state.barge_in_generation != barge_in_generation:
                    if state.is_active and state.barge_in_generation != barge_in_generation:
                        logger.info(
                            "Barge-in completed on channel %s after reply chunk %d; listening for caller",
                            state.channel_id,
                            index + 1,
                        )
                    return stats
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
        return stats

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
        file_stem = f"{state.session_id}-{time.time_ns()}"
        (sound_dir / f"{file_stem}.{audio_format}").write_bytes(audio_bytes)
        duration = read_wav_info(audio_bytes).duration_seconds if audio_format == "wav" else None
        await self._play_and_wait(
            state,
            media=f"sound:{(sound_dir / file_stem).as_posix()}",
            expected_duration_seconds=duration,
        )

    async def _play_and_wait(
        self,
        state: CallState,
        *,
        media: str,
        expected_duration_seconds: float | None = None,
    ) -> None:
        """Starts ARI playback and waits for it to actually finish before
        returning, so the next recording doesn't start (and potentially
        capture the AI's own voice) while audio is still playing out.
        Never issues /play for a call that has ended, and stops waiting as
        soon as the call ends."""
        if not state.is_active:
            logger.info("Skipping playback on channel %s: call already ended", state.channel_id)
            return
        playback_started = time.monotonic()
        request_started = time.monotonic()
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
            await self._finish_call(
                state.channel_id,
                status="completed",
                hangup_cause="caller_hangup",
                channel_gone=True,
            )
            return
        request_ms = _ms_since(request_started)
        playback_id = playback.get("id")
        if not playback_id:
            logger.warning("Playback response for channel %s had no id (request=%dms)", state.channel_id, request_ms)
            return  # can't track completion — proceed rather than hang

        finished = asyncio.Event()
        self._playback_finished[playback_id] = finished
        state.active_playback_id = playback_id
        barge_in_generation = state.barge_in_generation
        outcome = "finished"
        try:
            completed, _ = await self._unless_call_ends(
                state, finished.wait(), timeout=self._settings.provider_timeout_seconds
            )
            if not completed:
                outcome = "call-ended"
                logger.info("Call on channel %s ended during playback %s", state.channel_id, playback_id)
        except asyncio.TimeoutError:
            outcome = "timeout"
            logger.warning("Timed out waiting for playback %s to finish on channel %s", playback_id, state.channel_id)
        finally:
            self._playback_finished.pop(playback_id, None)
            if state.active_playback_id == playback_id:
                state.active_playback_id = None
            if state.barge_in_generation != barge_in_generation:
                outcome = "barge-in"
            wall_ms = _ms_since(playback_started)
            expected_ms = int(expected_duration_seconds * 1000) if expected_duration_seconds is not None else None
            drift_ms = wall_ms - expected_ms if expected_ms is not None and outcome == "finished" else None
            logger.info(
                "Playback timing: channel=%s playback=%s outcome=%s request_ms=%d wall_ms=%d expected_ms=%s drift_ms=%s",
                state.channel_id,
                playback_id,
                outcome,
                request_ms,
                wall_ms,
                expected_ms if expected_ms is not None else "-",
                drift_ms if drift_ms is not None else "-",
            )
            if state.is_active:
                self._start_background_diagnostic(self._log_rtp_statistics(state.channel_id))

    async def _on_talking_started(self, event: dict) -> None:
        """Stop AI speech when Asterisk detects caller speech.

        The AI dialplan enables TALK_DETECT, which emits this event on the
        caller channel. It is only considered barge-in while a playback is
        active; normal caller speech during the recording phase is handled by
        the regular recording lifecycle instead.
        """
        channel_id = (event.get("channel") or {}).get("id")
        state = self._calls.get(channel_id) if channel_id else None
        if state is None or not state.is_active or not state.active_playback_id:
            return

        playback_id = state.active_playback_id
        state.barge_in_generation += 1
        logger.info(
            "Barge-in detected on channel %s; stopping playback %s",
            channel_id,
            playback_id,
        )
        try:
            await self._ari.stop_playback(playback_id)
        except AriError as exc:
            if exc.status_code not in _CHANNEL_GONE_STATUSES:
                logger.warning("Could not stop playback %s after barge-in: %s", playback_id, exc)
        finally:
            # Asterisk normally emits PlaybackFinished after DELETE. Setting
            # the local event too prevents a lost event from waiting until
            # the provider timeout.
            finished = self._playback_finished.get(playback_id)
            if finished is not None:
                finished.set()

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
