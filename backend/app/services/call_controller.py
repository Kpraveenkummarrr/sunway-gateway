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
    prepare_tts_clip,
    read_wav_info,
)
from app.services.endpointing import EndOfSpeechTracker, EndpointConfig, monitor_turn
from app.services.hindi_tts_text import hindi_tts_text, unspoken_latin
from app.services.system_config import effective_settings
from app.services.spoken_text import spoken_text
from app.services.knowledge_search import SearchError
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
_CALLER_MESSAGE_KINDS = ("welcome", "error", "goodbye", "closing")

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
        "consumed_recording_seq",
        "settings",
        "last_talk_finished",
        "reply_in_progress",
        "recording_name",
        "recording_task",
        "endpoint_stopped_seq",
        "endpoint_last_speech",
        "endpoint_reason",
        "tracker",
        "talking",
        "deadline_task",
        "turns_completed",
        "barge_in_count",
        "playback_started_monotonic",
        "answered_monotonic",
        "welcome_playing",
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
        self.consumed_recording_seq = 0
        self.settings: Settings | None = None
        self.last_talk_finished: float | None = None
        # True from the first reply chunk being requested until the reply is
        # finished or discarded - including the pauses between chunks while the
        # next one is still being synthesized, when nothing is playing.
        self.reply_in_progress = False
        self.recording_name: str | None = None
        self.recording_task: asyncio.Task | None = None
        # Which recording the worker ended itself (vs Asterisk ending it).
        self.endpoint_stopped_seq = 0
        self.endpoint_last_speech: float | None = None
        self.endpoint_reason: str | None = None
        # End-of-speech tracker of the recording that is open, fed by Asterisk's
        # talk-detect events; and whether the caller is talking right now.
        self.tracker: EndOfSpeechTracker | None = None
        self.talking = False
        self.deadline_task: asyncio.Task | None = None
        self.turns_completed = 0
        self.barge_in_count = 0
        self.playback_started_monotonic: float | None = None
        self.answered_monotonic: float | None = None
        # True while the welcome plays: line noise or a caller's "hello" in the first second
        # must not cut it (seen on the client: every call's welcome stopped at ~1.0 s).
        self.welcome_playing = False

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
        self._phrase_clips: dict[tuple, asyncio.Future] = {}

    @property
    def active_call_count(self) -> int:
        return len(self._calls)

    def _settings_for(self, state: CallState) -> Settings:
        return state.settings or self._settings

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
        no rendered phrase just renders it itself.

        Panel overrides are loaded first: rendering the .env phrases and then
        discovering different panel phrases on the first call threw the cache
        away, so the first call after every restart waited for TTS (seen on the
        client: welcome 17 s + 40-60 s render, callers hung up)."""
        await self._refresh_settings()
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
                self._track_talking(event, started=True)
                await self._on_talking_started(event)
            elif event_type == "ChannelTalkingFinished":
                self._track_talking(event, started=False)
            elif event_type in ("StasisEnd", "ChannelHangupRequest", "ChannelDestroyed"):
                await self._on_hangup(event)
        except Exception:  # noqa: BLE001 - one bad event must not kill the loop
            logger.exception("Unhandled error processing ARI event %s", event_type)

    def _track_talking(self, event: dict, *, started: bool) -> None:
        """Feeds Asterisk's talk-detect events to the end-of-speech tracker of
        the turn being recorded. Synchronous and first, so the tracker never
        lags an event that barge-in handling is still awaiting."""
        state = self._calls.get((event.get("channel") or {}).get("id"))
        if state is None:
            return
        now = time.monotonic()
        state.talking = started
        if not started:
            state.last_talk_finished = now
        tracker = state.tracker
        if tracker is not None:
            if started:
                tracker.talking_started(now)
            else:
                tracker.talking_finished(now, event.get("duration"))

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
            await self._refresh_settings()
            state.settings = self._settings.model_copy(deep=True)
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

                session = await create_session(db, language=state.settings.ai_language, call_id=call_row.id)
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

        state.answered_monotonic = time.monotonic()
        self._start_background_diagnostic(self._log_media_formats(channel_id))
        # While the welcome plays, open the connections the first question will
        # need, so it does not pay for DNS + TCP + TLS on top of the AI work.
        self._start_background_diagnostic(self._warm_providers())
        await self._apply_talk_detect(state)
        # Enforced by a timer, not only at turn boundaries (see _call_deadline).
        state.deadline_task = asyncio.create_task(self._call_deadline(state))
        state.deadline_task.add_done_callback(self._log_task_failure("call deadline timer"))

        async with self._session_factory() as db:
            call_row = await db.get(Call, state.call_row_id)
            if call_row is not None:
                call_row.answered_at = datetime.now(timezone.utc)
                await db.commit()

        await self._play_welcome(state)
        if state.is_active:  # the caller may hang up during the welcome
            await self._start_next_recording(state)

    async def _warm_providers(self) -> None:
        started = time.monotonic()
        await asyncio.gather(
            self._stt_provider.warm_up(),
            self._llm_provider.warm_up(),
            self._tts_provider.warm_up(),
            return_exceptions=True,
        )
        logger.info("Provider connections warmed in %dms", _ms_since(started))

    @staticmethod
    def _log_task_failure(what: str):
        def done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                logger.warning("%s failed: %r", what, task.exception())

        return done

    async def _apply_talk_detect(self, state: CallState, *, reset: bool = False) -> None:
        """Sets barge-in sensitivity on this call's channel. The dialplan
        hardcodes it; doing it here makes it a setting, and it works for every
        way a call can reach the AI (verified on Asterisk 18.10: the override
        takes effect immediately).

        `reset` removes the detector first, so it starts from "nobody is
        talking". Measured on Asterisk 18.10: a gateway that goes from speech
        straight to comfort noise / silence leaves the detector believing the
        caller is still talking (its silence clock only runs on voice frames),
        and it then never reports the *next* burst of speech - barge-in would be
        dead for the whole reply. A fresh detector always reports it."""
        settings = self._settings_for(state)
        if not settings.ai_talk_detect_override:
            return  # the dialplan owns TALK_DETECT; do not remove what it set up
        value = f"{settings.ai_talk_detect_silence_ms},{settings.ai_talk_detect_threshold}"
        try:
            if reset:
                await self._ari.set_channel_variable(state.channel_id, "TALK_DETECT(remove)", "1")
                state.talking = False
            await self._ari.set_channel_variable(state.channel_id, "TALK_DETECT(set)", value)
        except AriError as exc:
            if _channel_gone(exc):
                return
            logger.warning(
                "Could not apply the barge-in sensitivity on channel %s (%s); the dialplan's values stay in effect",
                state.channel_id, exc,
            )
            return
        if not reset:
            logger.info(
                "Barge-in sensitivity applied: channel=%s talk_detect=%s (silence_ms,magnitude)",
                state.channel_id, value,
            )

    async def _end_if_over_duration(self, state: CallState) -> bool:
        """Closes the call politely if it has run past AI_CALL_TIMEOUT_SECONDS.
        Returns True if it did. 0 disables the limit."""
        limit = self._settings_for(state).ai_call_timeout_seconds
        if limit <= 0 or not state.is_active or self._elapsed_seconds(state) <= limit:
            return False
        logger.info(
            "Call reached its maximum duration: channel=%s elapsed=%.0fs limit=%ds - closing with a goodbye",
            state.channel_id, self._elapsed_seconds(state), limit,
        )
        await self._end_call_with_goodbye(state, hangup_cause="max_duration", kind="closing")
        return True

    async def _call_deadline(self, state: CallState) -> None:
        """Timer behind AI_CALL_TIMEOUT_SECONDS. The limit used to be checked
        only when a caller turn finished, so a call was cut at whichever turn
        boundary came first after it - ~165 s with ~55 s exchanges - and with no
        goodbye. Now, when the time is up: if the AI is listening, the turn is
        ended so the call closes at once; if it is thinking or speaking, the
        answer finishes and the call closes when it would listen next."""
        limit = self._settings_for(state).ai_call_timeout_seconds
        if limit <= 0:
            return
        try:
            await asyncio.wait_for(state.ended.wait(), timeout=max(0.0, limit - self._elapsed_seconds(state)))
            return  # the call ended first
        except asyncio.TimeoutError:
            pass
        name = state.recording_name
        if state.is_active and name and state.consumed_recording_seq < state.recording_seq:
            try:
                await self._ari.stop_recording(name)
            except AriError:
                pass  # the recording just ended by itself; its handler closes the call

    def _start_endpoint_monitor(self, state: CallState, name: str, seq: int, settings: Settings) -> None:
        """Ends this turn's recording once the caller has stopped talking,
        independently of the gateway's RTP behaviour (see
        app.services.endpointing for the two signals used and why the recording
        file is not one of them). Asterisk's own limits remain as a backstop."""
        if not settings.ai_endpoint_monitor:
            return
        config = EndpointConfig(
            silence_ms=settings.ai_endpoint_silence_ms,
            min_speech_ms=settings.ai_endpoint_min_speech_ms,
            # With the override off the dialplan's own TALK_DETECT applies: 200 ms in this project.
            talk_detect_silence_ms=settings.ai_talk_detect_silence_ms if settings.ai_talk_detect_override else 200,
            use_rtp_statistics=settings.ai_endpoint_use_rtp_statistics,
        )
        # A caller who is already mid-sentence (they interrupted the prompt) has had
        # their "started" event already.
        tracker = EndOfSpeechTracker(config, time.monotonic(), already_talking=state.talking)
        state.tracker = tracker

        def is_current() -> bool:
            return state.is_active and state.recording_seq == seq and state.consumed_recording_seq < seq

        async def sample_rtp() -> tuple[int, int] | None:
            try:
                stats = await self._ari.get_rtp_statistics(state.channel_id)
                return int(stats["rxcount"]), int(stats["rxoctetcount"])
            except (AriError, KeyError, TypeError, ValueError):
                return None

        async def on_end(decision) -> None:
            state.endpoint_stopped_seq = seq
            state.endpoint_last_speech = decision.last_speech_at
            state.endpoint_reason = decision.reason
            logger.info(
                "Caller finished speaking: channel=%s turn=%d signal=%s speech=%dms silence>=%dms - ending the recording",
                state.channel_id, seq, decision.reason, decision.speech_ms, settings.ai_endpoint_silence_ms,
            )
            try:
                await self._ari.stop_recording(name)
            except AriError as exc:
                if exc.status_code not in (404, 409):
                    logger.warning("Could not end recording %s: %s", name, exc)

        task = asyncio.ensure_future(monitor_turn(tracker, is_current=is_current, on_end=on_end, sample_rtp=sample_rtp))
        task.add_done_callback(self._log_task_failure("end-of-speech monitor"))
        state.recording_task = task

    @staticmethod
    def _cancel_task(task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()

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
        state.welcome_playing = not self._settings_for(state).ai_welcome_interruptible
        try:
            await self._play_caller_message(state, "welcome")
        except (TTSProviderError, AriError):
            logger.exception("Welcome message playback failed for channel %s", state.channel_id)
            # Not fatal — continue into the conversation loop even if the
            # welcome prompt couldn't be played.
        finally:
            state.welcome_playing = False

    async def _start_next_recording(self, state: CallState) -> None:
        if not state.is_active:
            return
        if await self._end_if_over_duration(state):
            return
        after_barge_in = state.barge_in_generation > state.beeped_generation
        state.beeped_generation = state.barge_in_generation
        state.recording_seq += 1
        state.recording_started_monotonic = time.monotonic()
        state.last_talk_finished = None
        settings = self._settings_for(state)
        # ARI's record `name` maps directly to a filename under Asterisk's
        # recording spool dir — it does NOT create subdirectories for a
        # name containing "/" (confirmed live: that fails with "recording
        # error: No such file or directory"). Keep it flat; "__" as the
        # separator since it can't appear in a UUID.
        name = f"{_RECORDING_NAME_PREFIX}{state.session_id}__{state.recording_seq}"
        state.recording_name = name
        self._cancel_task(state.recording_task)
        try:
            await self._ari.record(
                state.channel_id,
                name=name,
                max_duration_seconds=settings.ai_max_turn_seconds,
                max_silence_seconds=settings.ai_end_of_speech_silence_seconds,
                # After a barge-in the caller is already mid-sentence, so a
                # beep would land on top of their speech.
                beep=settings.ai_record_beep and not after_barge_in,
            )
            self._start_endpoint_monitor(state, name, state.recording_seq, settings)
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
        # Claim the expected sequence before the first await: duplicate or
        # delayed ARI events must not create parallel turns on one call.
        if not _seq.isdigit() or int(_seq) != state.recording_seq or int(_seq) <= state.consumed_recording_seq:
            return
        state.consumed_recording_seq = int(_seq)
        self._cancel_task(state.recording_task)
        state.tracker = None
        settings = self._settings_for(state)
        # Off the reply's critical path: ASR/LLM/TTS take far longer than this.
        self._start_background_diagnostic(self._apply_talk_detect(state, reset=True))
        if state.last_talk_finished is not None:
            logger.info("Stage call_id=%s turn_id=%d stage=talk_finished_event_to_recording duration_ms=%d",
                        state.channel_id, state.recording_seq, _ms_since(state.last_talk_finished))
        capture_ms = (
            _ms_since(state.recording_started_monotonic)
            if state.recording_started_monotonic is not None
            else None
        )

        # Why this turn ended - the single most useful fact when responses are
        # slow: a turn that ends at the maximum duration means silence was never
        # detected (comfort-noise gateway, or line noise above Asterisk's threshold).
        if state.endpoint_stopped_seq == int(_seq):
            ended_by = f"worker end-of-speech detector [{state.endpoint_reason}]"
        elif capture_ms is not None and capture_ms >= settings.ai_max_turn_seconds * 1000 - 800:
            ended_by = "MAX DURATION (silence was never detected)"
        else:
            ended_by = "Asterisk silence detector"
        end_wait_ms = (
            _ms_since(state.endpoint_last_speech)
            if state.endpoint_stopped_seq == int(_seq) and state.endpoint_last_speech is not None
            else None
        )
        logger.info(
            "Recording finished: channel=%s turn=%s ended_by=%s capture_ms=%s end_of_speech_wait_ms=%s "
            "audio_duration_s=%s talking_s=%s silence_s=%s",
            state.channel_id, _seq, ended_by, capture_ms if capture_ms is not None else "-",
            end_wait_ms if end_wait_ms is not None else "-", recording.get("duration", "-"),
            recording.get("talking_duration", "-"), recording.get("silence_duration", "-"),
        )

        if await self._end_if_over_duration(state):
            return

        audio_format = recording.get("format", "wav")
        if audio_format != "wav":
            await self._finish_call(state.channel_id, status="failed", hangup_cause="recording_format")
            return
        recording_path = Path(settings.asterisk_recording_spool_path) / f"{name}.{audio_format}"

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
            # Wall-clock, not audio length: a gateway that stops sending during
            # silence writes almost no audio for a long wait, and counting only
            # the audio would let a silent caller hold the line indefinitely.
            waited = max(wav_info.duration_seconds, (capture_ms or 0) / 1000.0)
            state.silent_seconds += waited
            logger.info(
                "Turn %d on channel %s had no speech (%.2fs) — %.1fs of %ds without input",
                state.recording_seq,
                state.channel_id,
                waited,
                state.silent_seconds,
                settings.ai_no_input_timeout_seconds,
            )
            if state.silent_seconds >= settings.ai_no_input_timeout_seconds:
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
            end_wait_ms=end_wait_ms,
        )

    async def _run_turn(
        self,
        state: CallState,
        audio_bytes: bytes,
        *,
        turn_started: float | None = None,
        audio_seconds: float = 0.0,
        capture_ms: int | None = None,
        end_wait_ms: int | None = None,
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
                logger.info("Stage call_id=%s turn_id=%d stage=asr duration_ms=%d",
                            state.channel_id, state.recording_seq, stage_timings["asr"])
                logger.info(
                    "STT ok for session %s: %d chars in %dms",
                    session.id,
                    len(transcription.text),
                    stage_timings["asr"],
                )
                try:
                    completed, turn = await self._unless_call_ends(state, handle_text_turn(
                        db,
                        session,
                        transcription.text,
                        settings=self._settings_for(state),
                        embedding_provider=self._embedding_provider,
                        llm_provider=self._llm_provider,
                        trace_call_id=state.channel_id,
                        trace_turn_id=str(state.recording_seq),
                    ))
                    if not completed:
                        return
                    reply_text = turn.assistant_message.text
                    finish_reason = turn.finish_reason
                    stage_timings.update(turn.timings)
                    succeeded = True
                except ConversationTimeoutError as exc:
                    logger.error("AI turn timed out for session %s: %s", session.id, exc)
                    needs_safe_error = True
                except ConversationError:
                    pass  # e.g. empty transcription — just prompt again, no error tone
                except (EmbeddingProviderError, LLMProviderError, SearchError) as exc:
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
                    "Turn timing channel=%s turn=%d audio=%.1fs capture_ms=%sms asr_ms=%sms "
                    "embedding_ms=%sms retrieval_ms=%sms context_ms=%sms llm_ms=%sms "
                    "tts_ms=%sms audio_ms=%sms reply_chars=%d chunks=%d first_audio_ready_ms=%sms "
                    "tts_first_ms=%sms prep_first_ms=%sms speaking_ms=%d total_ms=%d "
                    "end_wait_ms=%sms heard_delay_ms=%sms",
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
                    end_wait_ms if end_wait_ms is not None else "-",
                    # last caller speech -> first reply audio ready locally
                    (end_wait_ms + pre_reply_ms + first_audio_ms)
                    if end_wait_ms is not None and first_audio_ms is not None else "-",
                )

        if succeeded:
            state.turns_completed += 1
        await self._start_next_recording(state)

    async def _register_failure(self, state: CallState) -> bool:
        """Tracks a failed turn (STT or provider failure). Returns True if
        the call was ended because the consecutive-failure cap was hit —
        callers should stop their own turn processing when this returns
        True."""
        if not state.is_active:
            return True
        state.consecutive_failures += 1
        if state.consecutive_failures < self._settings_for(state).ai_max_consecutive_failures:
            return False

        logger.info(
            "Channel %s hit %d consecutive failed turns — ending call",
            state.channel_id,
            state.consecutive_failures,
        )
        await self._end_call_with_goodbye(state, hangup_cause="too_many_failed_turns")
        return True

    async def _end_call_with_goodbye(self, state: CallState, *, hangup_cause: str, kind: str = "goodbye") -> None:
        try:
            await self._play_caller_message(state, kind)
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

    async def _render_speech(self, text: str, *, settings: Settings | None = None) -> SpeechClip:
        """TTS + telephone preparation for one piece of text, independent of
        any call. Raises TTSProviderError."""
        settings = settings or self._settings
        if settings.ai_language == "hi":
            # Digits, units and Latin acronyms are what an Indic voice mishandles;
            # the welcome (pure Devanagari) passes through unchanged.
            latin = unspoken_latin(text)
            if latin:
                logger.info("TTS text still has Latin words the voice may mishandle: %s", latin[:8])
            text = hindi_tts_text(text)
        tts_started = time.monotonic()
        result = await self._tts_provider.synthesize(text, language=settings.ai_language)
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
            # One call, off the event loop: prepare AND measure. The cheap
            # level measurements used to run on the loop, per chunk, per call.
            prepared = await asyncio.to_thread(
                prepare_tts_clip,
                result.audio_bytes,
                speed=settings.ai_tts_speed,
                profile=settings.ai_audio_profile,
                target_rms_dbfs=settings.ai_tts_target_rms_dbfs,
                peak_ceiling_dbfs=settings.ai_tts_peak_ceiling_dbfs,
            )
        except AudioFormatError as exc:
            raise TTSProviderError(f"Could not prepare TTS audio for playback: {exc}") from exc
        audio, clip = prepared.audio, prepared.diagnostics
        seconds = clip.final_duration_s
        prep_ms = _ms_since(prep_started)
        words = len(text.split())
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
        # One line per clip with everything needed to tell WHICH stage made a
        # clip sound wrong: what came in, what was done to it, what went out.
        logger.info(
            "TTS clip: provider=%s model=%s voice=%s chars=%d words=%d tts=%dms | %s | speech_rate=%.1f words/min",
            self._settings.tts_provider,
            model,
            voice,
            len(text),
            words,
            tts_ms,
            clip.summary(),
            (words / seconds * 60.0) if seconds > 0 else 0.0,
        )
        return SpeechClip(audio=audio, tts_ms=tts_ms, prep_ms=prep_ms, seconds=seconds)

    def _phrase_clip(self, text: str, *, settings: Settings | None = None) -> asyncio.Future:
        """The shared rendering of a fixed caller phrase, started on first use.
        A failed or cancelled rendering is replaced on the next request."""
        settings = settings or self._settings
        key = (text, settings.ai_language, settings.ai_tts_speed, settings.ai_audio_profile,
               settings.ai_tts_target_rms_dbfs, settings.ai_tts_peak_ceiling_dbfs)
        future = self._phrase_clips.get(key)
        if future is None or (future.done() and (future.cancelled() or future.exception() is not None)):
            future = asyncio.ensure_future(self._render_speech(text, settings=settings))
            future.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark exception retrieved
            self._phrase_clips[key] = future
        return future

    async def _play_caller_message(self, state: CallState, kind: str) -> None:
        settings = self._settings_for(state)
        text = settings.caller_message(kind)
        if not text.strip() or not state.is_active:
            return
        # Shielded: a caller hanging up stops *this call's* wait, but the
        # shared rendering continues for the next call.
        completed, clip = await self._unless_call_ends(state, asyncio.shield(self._phrase_clip(text, settings=settings)))
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
        completed, clip = await self._unless_call_ends(state, self._render_speech(text, settings=self._settings_for(state)))
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
        settings = self._settings_for(state)
        pending = asyncio.ensure_future(self._render_speech(chunks[0], settings=settings))
        barge_in_generation = state.barge_in_generation
        # Marks the whole reply - including the pauses while the next chunk is
        # still being synthesized, when nothing is playing - so an interruption
        # in a pause is recognised (previously only an active playback counted,
        # and the AI then talked over the caller for the rest of the reply).
        state.reply_in_progress = True
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
                if state.barge_in_generation != barge_in_generation:
                    logger.info(
                        "Caller interrupted while reply chunk %d of %d was being prepared - discarding the rest of the reply",
                        index + 1,
                        len(chunks),
                    )
                    return stats
                pending = (
                    asyncio.ensure_future(self._render_speech(chunks[index + 1], settings=settings)) if index + 1 < len(chunks) else None
                )
                stats["tts_ms"] += clip.tts_ms
                stats["audio_ms"] += clip.prep_ms
                logger.info("Stage call_id=%s turn_id=%d stage=tts_chunk_%d duration_ms=%d",
                            state.channel_id, state.recording_seq, index + 1, clip.tts_ms)
                logger.info("Stage call_id=%s turn_id=%d stage=audio_prepare_chunk_%d duration_ms=%d",
                            state.channel_id, state.recording_seq, index + 1, clip.prep_ms)
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
            state.reply_in_progress = False
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
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
        sound_dir = Path(self._settings_for(state).asterisk_recording_spool_path).parent / "sounds" / "ai-agent"
        sound_dir.mkdir(parents=True, exist_ok=True)
        file_stem = f"{state.session_id}-{time.time_ns()}"
        output_path = sound_dir / f"{file_stem}.{audio_format}"
        output_path.write_bytes(audio_bytes)
        try:
            duration = read_wav_info(audio_bytes).duration_seconds if audio_format == "wav" else None
            await self._play_and_wait(
                state,
                media=f"sound:{(sound_dir / file_stem).as_posix()}",
                expected_duration_seconds=duration,
            )
        finally:
            # Ephemeral assistant audio, not caller recordings. Never let
            # generated clips accumulate for the lifetime of the server.
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove generated clip for channel %s", state.channel_id)

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
        state.playback_started_monotonic = playback_started
        request_started = time.monotonic()
        playback_id = self._ari.new_playback_id()
        finished = asyncio.Event()
        self._playback_finished[playback_id] = finished
        state.active_playback_id = playback_id
        barge_in_generation = state.barge_in_generation
        request_ms = 0
        outcome = "failed"
        try:
            try:
                playback = await self._ari.play(state.channel_id, media=media, playback_id=playback_id)
            except AriError as exc:
                if not _channel_gone(exc):
                    raise
                outcome = "call-ended"
                await self._finish_call(
                    state.channel_id, status="completed",
                    hangup_cause="caller_hangup", channel_gone=True,
                )
                return
            request_ms = _ms_since(request_started)
            if not state.is_active or state.barge_in_generation != barge_in_generation:
                # A stop sent before playback creation can return 404.
                # Retry once REST confirms creation, before starting recording.
                try:
                    await self._ari.stop_playback(playback_id)
                except AriError as exc:
                    if state.is_active and not _channel_gone(exc):
                        await self._finish_call(state.channel_id, status="failed", hangup_cause="playback_stop_failed")
                finished.set()
            if not playback.get("id"):
                logger.warning("Playback response for channel %s had no id (request=%dms)", state.channel_id, request_ms)
            outcome = "finished"
            completed, _ = await self._unless_call_ends(
                state, finished.wait(), timeout=max(
                    self._settings_for(state).provider_timeout_seconds,
                    (expected_duration_seconds or 0) + 5.0,
                )
            )
            if not completed:
                outcome = "call-ended"
                logger.info("Call on channel %s ended during playback %s", state.channel_id, playback_id)
        except asyncio.TimeoutError:
            outcome = "timeout"
            logger.warning("Timed out waiting for playback %s to finish on channel %s", playback_id, state.channel_id)
            try:
                await self._ari.stop_playback(playback_id)
            except AriError as exc:
                if not _channel_gone(exc):
                    await self._finish_call(state.channel_id, status="failed", hangup_cause="playback_stop_failed")
        except asyncio.CancelledError:
            outcome = "cancelled"
            # The request may have reached Asterisk even if its response did
            # not reach us. Stop the known ID before discarding local state.
            try:
                await self._ari.stop_playback(playback_id)
            except AriError:
                logger.info("Playback stop during cancellation was not confirmed: channel=%s", state.channel_id)
            raise
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
        if state is None or not state.is_active:
            return
        if state.welcome_playing:
            logger.info("Caller/noise heard during the welcome on channel %s - welcome not interrupted", channel_id)
            return
        playback_id = state.active_playback_id
        if not playback_id and not state.reply_in_progress:
            return  # ordinary speech while the AI is listening

        # Claim it synchronously; repeated talk events cannot stop it twice.
        state.active_playback_id = None
        state.barge_in_generation += 1
        state.barge_in_count += 1
        stop_started = time.monotonic()
        if not playback_id:
            # The caller spoke in the pause between two reply chunks. Nothing is
            # playing to stop, but bumping the generation discards the chunk that
            # is being prepared and every one after it.
            logger.info(
                "Barge-in detected on channel %s between reply chunks - discarding the rest of the reply",
                channel_id,
            )
            return
        age_ms = (
            _ms_since(state.playback_started_monotonic) if state.playback_started_monotonic is not None else None
        )
        logger.info(
            "Barge-in detected on channel %s; stopping playback %s (playback had been running %s ms)",
            channel_id,
            playback_id,
            age_ms if age_ms is not None else "-",
        )
        try:
            await self._ari.stop_playback(playback_id)
        except AriError as exc:
            if exc.status_code not in _CHANNEL_GONE_STATUSES:
                logger.warning("Could not stop playback %s after barge-in: %s", playback_id, exc)
                await self._finish_call(state.channel_id, status="failed", hangup_cause="barge_in_stop_failed")
        finally:
            # Asterisk normally emits PlaybackFinished after DELETE. Setting
            # the local event too prevents a lost event from waiting until
            # the provider timeout.
            finished = self._playback_finished.get(playback_id)
            if finished is not None:
                finished.set()
            logger.info("Stage call_id=%s turn_id=%d stage=barge_in_stop duration_ms=%d",
                        state.channel_id, state.recording_seq, _ms_since(stop_started))

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
            await asyncio.gather(work, ended, return_exceptions=True)
        if work in done:
            return True, work.result()
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
        self._cancel_task(state.recording_task)
        self._cancel_task(state.deadline_task)

        if state.session_id is not None:
            await self._close_records(state)
        # else: StasisStart is still creating this call's rows and closes them once they exist.

        if not channel_gone:
            try:
                await self._ari.hangup(channel_id)
            except AriError:
                pass  # hung up concurrently — fine

        logger.info(
            "Call finished: channel=%s status=%s cause=%s duration=%.1fs turns=%d barge_ins=%d ended_by=%s",
            channel_id, status, hangup_cause, self._elapsed_seconds(state), state.turns_completed,
            state.barge_in_count, "caller/gateway/Asterisk" if channel_gone else "this worker",
        )

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
