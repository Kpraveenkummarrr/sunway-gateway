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

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.ai import AIMessage, AISession
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.llm.base import LLMMessage, LLMProvider
from app.providers.stt.base import STTProvider
from app.providers.tts.base import SynthesisResult, TTSProvider
from app.services.knowledge_search import SearchResult, search_chunks
from app.services.rag_context import build_context

_ROLE_TO_LLM_ROLE = {"caller": "user", "agent": "assistant"}


class ConversationError(Exception):
    """Raised for invalid conversation state (e.g. message on a closed
    session, empty input) — distinct from provider-specific errors, which
    propagate as-is so callers can distinguish "your request was invalid"
    from "a provider failed"."""


@dataclass(frozen=True)
class TurnResult:
    user_message: AIMessage
    assistant_message: AIMessage
    retrieved_chunks: list[SearchResult] = field(default_factory=list)


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
    message, retrieve relevant knowledge, generate a reply, store it."""
    if session.status != "active":
        raise ConversationError(f"Session {session.id} is not active (status={session.status})")
    if not user_text or not user_text.strip():
        raise ConversationError("Message text is empty")

    user_message = await _record_message(db, session, role="caller", text=user_text.strip())

    query_embedding = await embedding_provider.embed_one(user_text)
    retrieved = await search_chunks(
        db,
        query_embedding=query_embedding,
        top_k=settings.rag_top_k,
        similarity_threshold=settings.rag_similarity_threshold,
    )
    context = build_context(retrieved, max_chars=settings.ai_max_context_chars)

    history = await get_history(db, session.id)
    llm_history = _history_to_llm_messages(history, max_messages=settings.ai_max_history_messages)

    started = time.monotonic()
    llm_response = await llm_provider.generate_response(
        system_prompt=settings.ai_system_prompt,
        history=llm_history,
        retrieved_context=context,
    )
    llm_latency_ms = int((time.monotonic() - started) * 1000)

    assistant_message = await _record_message(
        db,
        session,
        role="agent",
        text=llm_response.text,
        retrieved_chunk_ids=[str(r.chunk_id) for r in retrieved],
        llm_latency_ms=llm_latency_ms,
    )

    return TurnResult(user_message=user_message, assistant_message=assistant_message, retrieved_chunks=retrieved)


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

    stt_started = time.monotonic()
    transcription = await stt_provider.transcribe(audio_bytes, language=session.language)
    stt_latency_ms = int((time.monotonic() - stt_started) * 1000)

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

    return AudioTurnResult(turn=turn, transcribed_text=transcription.text, assistant_audio=audio_result)
