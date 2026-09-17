"""AI conversation session management and text/audio turn orchestration.

Pipeline for a text turn:
    user text -> (store) -> embed query -> RAG search -> bounded context
    -> LLM -> (store) -> assistant text

Pipeline for an audio turn additionally wraps this with STT before and TTS
after. Real-time telephone audio streaming is explicitly out of scope for
this phase — this is file/buffer based, for development/testing only.

Every session is isolated: all reads/writes are scoped by session_id, and
nothing is cached or shared across sessions in-process.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.ai import AIMessage, AISession
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.llm.base import LLMMessage, LLMProvider
from app.providers.stt.base import STTProvider
from app.providers.tts.base import SynthesisResult, TTSProvider
from app.services.knowledge_search import (
    SearchError,
    SearchResult,
    build_retrieval_query,
    search_chunks,
)
from app.services.rag_context import build_context

logger = get_logger(__name__)

_ROLE_TO_LLM_ROLE = {"caller": "user", "agent": "assistant"}


class ConversationError(Exception):
    """Raised for invalid conversation state (e.g. message on a closed
    session, empty input) — distinct from provider-specific errors, which
    propagate as-is so callers can distinguish "your request was invalid"
    from "a provider failed"."""


class ConversationTimeoutError(ConversationError):
    """The overall turn (embed + RAG + LLM, or the audio equivalent)
    exceeded AI_TURN_TIMEOUT_SECONDS — a belt-and-suspenders cap in
    addition to each provider's own PROVIDER_TIMEOUT_SECONDS. A subclass
    of ConversationError so existing `except ConversationError` handling
    still works; callers that want to react differently to a timeout
    specifically (e.g. a 504 instead of a 400) can catch it directly."""


@dataclass(frozen=True)
class TurnResult:
    user_message: AIMessage
    assistant_message: AIMessage
    retrieved_chunks: list[SearchResult] = field(default_factory=list)
    finish_reason: str | None = None
    # Stage latencies in ms: embed, rag, llm.
    timings: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioTurnResult:
    turn: TurnResult
    transcribed_text: str
    assistant_audio: SynthesisResult


async def create_session(
    db: AsyncSession, *, language: str = "en", call_id: uuid.UUID | None = None
) -> AISession:
    session = AISession(language=language, status="active", call_id=call_id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> AISession | None:
    return await db.get(AISession, session_id)


async def get_history(db: AsyncSession, session_id: uuid.UUID) -> list[AIMessage]:
    result = await db.execute(
        select(AIMessage).where(AIMessage.session_id == session_id).order_by(AIMessage.created_at.asc())
    )
    return list(result.scalars().all())


async def end_session(db: AsyncSession, session: AISession, *, status: str = "completed") -> AISession:
    session.status = status
    await db.commit()
    await db.refresh(session)
    return session


async def _record_message(
    db: AsyncSession,
    session: AISession,
    *,
    role: str,
    text: str | None,
    **kwargs,
) -> AIMessage:
    message = AIMessage(session_id=session.id, role=role, text=text, **kwargs)
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


def _history_to_llm_messages(history: list[AIMessage], *, max_messages: int) -> list[LLMMessage]:
    windowed = history[-max_messages:] if max_messages > 0 else history
    return [
        LLMMessage(role=_ROLE_TO_LLM_ROLE[m.role], content=m.text or "")
        for m in windowed
        if m.role in _ROLE_TO_LLM_ROLE and m.text
    ]


async def handle_text_turn(
    db: AsyncSession,
    session: AISession,
    user_text: str,
    *,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
) -> TurnResult:
    """Runs one text-in/text-out conversation turn: store the user
    message, retrieve relevant knowledge, generate a reply, store it.

    Each provider already enforces its own PROVIDER_TIMEOUT_SECONDS
    internally (see app.providers.*.openai_provider); this additionally
    times the RAG search (a plain DB query, not a "provider") and caps
    the whole embed+search+LLM sequence at AI_TURN_TIMEOUT_SECONDS, in
    case several individually-fast calls still add up to something a
    caller (or a live phone call) shouldn't be left waiting on.
    """
    if session.status != "active":
        raise ConversationError(f"Session {session.id} is not active (status={session.status})")
    if not user_text or not user_text.strip():
        raise ConversationError("Message text is empty")

    turn_started = time.monotonic()
    user_message = await _record_message(db, session, role="caller", text=user_text.strip())

    timings: dict[str, int] = {}

    async def _pipeline() -> tuple[list[SearchResult], object, int]:
        # Fetched before retrieval, not just for the LLM: a short follow-up
        # needs the previous caller utterance to be searchable at all.
        history = await get_history(db, session.id)
        search_text = build_retrieval_query(
            user_text,
            [m.text or "" for m in history[:-1] if m.role == "caller"],
        )

        embed_started = time.monotonic()
        query_embedding = await embedding_provider.embed_one(search_text)
        timings["embedding"] = int((time.monotonic() - embed_started) * 1000)
        # Keep the old key for callers that consumed the pre-existing timing
        # dictionary while exposing the clearer public name.
        timings["embed"] = timings["embedding"]
        logger.info("embedding stage: %dms", timings["embedding"])

        try:
            retrieved = await asyncio.wait_for(
                search_chunks(
                    db,
                    query_embedding=query_embedding,
                    query_text=search_text,
                    top_k=settings.rag_top_k,
                    similarity_threshold=settings.rag_similarity_threshold,
                    embedding_space=embedding_provider.embedding_space,
                    timings=timings,
                ),
                timeout=settings.provider_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise SearchError(f"RAG search timed out after {settings.provider_timeout_seconds}s") from exc
        logger.info("rag stage: %dms, %d chunks", timings["rag"], len(retrieved))

        context_started = time.monotonic()
        context = build_context(retrieved, max_chars=settings.ai_max_context_chars)
        timings["context"] = int((time.monotonic() - context_started) * 1000)
        llm_history = _history_to_llm_messages(history, max_messages=settings.ai_max_history_messages)

        llm_started = time.monotonic()
        llm_response = await llm_provider.generate_response(
            system_prompt=settings.system_prompt_for(
                session.language, caller_text=user_text
            ),
            history=llm_history,
            retrieved_context=context,
        )
        llm_latency_ms = int((time.monotonic() - llm_started) * 1000)
        timings["llm"] = llm_latency_ms
        timings["llm_ms"] = llm_latency_ms
        logger.info("llm stage: %dms", llm_latency_ms)

        return retrieved, llm_response, llm_latency_ms

    try:
        retrieved, llm_response, llm_latency_ms = await asyncio.wait_for(
            _pipeline(), timeout=settings.ai_turn_timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        raise ConversationTimeoutError(
            f"AI turn timed out after {settings.ai_turn_timeout_seconds}s"
        ) from exc

    assistant_message = await _record_message(
        db,
        session,
        role="agent",
        text=llm_response.text,
        retrieved_chunk_ids=[str(r.chunk_id) for r in retrieved],
        llm_latency_ms=llm_latency_ms,
    )

    logger.info("turn total: %dms", int((time.monotonic() - turn_started) * 1000))
    return TurnResult(
        user_message=user_message,
        assistant_message=assistant_message,
        retrieved_chunks=retrieved,
        finish_reason=llm_response.finish_reason,
        timings=timings,
    )


async def handle_audio_turn(
    db: AsyncSession,
    session: AISession,
    audio_bytes: bytes,
    *,
    settings: Settings,
    stt_provider: STTProvider,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    tts_provider: TTSProvider,
) -> AudioTurnResult:
    """Runs one audio-in/audio-out conversation turn: STT -> text turn -> TTS.

    File/buffer based only — not a real-time stream. Suitable for
    development/testing of the pipeline shape, not telephone call audio.
    """
    if session.status != "active":
        raise ConversationError(f"Session {session.id} is not active (status={session.status})")

    audio_turn_started = time.monotonic()

    stt_started = time.monotonic()
    transcription = await stt_provider.transcribe(audio_bytes, language=session.language)
    stt_latency_ms = int((time.monotonic() - stt_started) * 1000)
    logger.info(
        "stt stage: %dms, input=%d bytes, duration=%ss",
        stt_latency_ms,
        len(audio_bytes),
        transcription.duration_seconds,
    )

    turn = await handle_text_turn(
        db,
        session,
        transcription.text,
        settings=settings,
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
    )
    turn.user_message.stt_confidence = transcription.confidence
    turn.user_message.stt_latency_ms = stt_latency_ms
    await db.commit()

    tts_started = time.monotonic()
    audio_result = await tts_provider.synthesize(
        turn.assistant_message.text or "", language=session.language
    )
    tts_latency_ms = int((time.monotonic() - tts_started) * 1000)
    turn.assistant_message.tts_latency_ms = tts_latency_ms
    await db.commit()
    logger.info("tts stage: %dms, output=%d bytes", tts_latency_ms, len(audio_result.audio_bytes))

    logger.info("audio turn total: %dms", int((time.monotonic() - audio_turn_started) * 1000))
    return AudioTurnResult(turn=turn, transcribed_text=transcription.text, assistant_audio=audio_result)
