import base64
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.security import require_internal_api_key
from app.models.ai import AIMessage, AISession
from app.providers.embeddings.base import EmbeddingProviderError
from app.providers.embeddings.factory import get_embedding_provider
from app.providers.llm.base import LLMProviderError
from app.providers.llm.factory import get_llm_provider
from app.providers.stt.base import STTProviderError
from app.providers.stt.factory import get_stt_provider
from app.providers.tts.base import TTSProviderError
from app.providers.tts.factory import get_tts_provider
from app.services.conversation import (
    ConversationError,
    ConversationTimeoutError,
    TurnResult,
    create_session,
    end_session,
    get_history,
    get_session,
    handle_audio_turn,
    handle_text_turn,
)
from app.services.knowledge_search import SearchError

router = APIRouter(
    prefix="/api/conversation", tags=["conversation"], dependencies=[Depends(require_internal_api_key)]
)
logger = get_logger(__name__)


class CreateSessionRequest(BaseModel):
    language: str | None = None


class MessageOut(BaseModel):
    id: UUID
    role: str
    text: str | None

    @classmethod
    def from_model(cls, m: AIMessage) -> "MessageOut":
        return cls(id=m.id, role=m.role, text=m.text)


class SessionOut(BaseModel):
    id: UUID
    language: str
    status: str
    messages: list[MessageOut] = []

    @classmethod
    def from_model(cls, session: AISession, messages: list[AIMessage] | None = None) -> "SessionOut":
        return cls(
            id=session.id,
            language=session.language,
            status=session.status,
            messages=[MessageOut.from_model(m) for m in (messages or [])],
        )


class SendMessageRequest(BaseModel):
    text: str


class RetrievedChunkOut(BaseModel):
    document_filename: str
    page_number: int | None
    similarity: float


class TurnOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    retrieved_chunks: list[RetrievedChunkOut]

    @classmethod
    def from_turn(cls, turn: TurnResult) -> "TurnOut":
        return cls(
            user_message=MessageOut.from_model(turn.user_message),
            assistant_message=MessageOut.from_model(turn.assistant_message),
            retrieved_chunks=[
                RetrievedChunkOut(
                    document_filename=r.document_filename, page_number=r.page_number, similarity=r.similarity
                )
                for r in turn.retrieved_chunks
            ],
        )


class AudioTurnOut(BaseModel):
    transcribed_text: str
    turn: TurnOut
    assistant_audio_base64: str
    assistant_audio_format: str


async def _get_session_or_404(db: AsyncSession, session_id: UUID) -> AISession:
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_conversation_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SessionOut:
    session = await create_session(db, language=body.language or settings.ai_language)
    return SessionOut.from_model(session)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_conversation_session(session_id: UUID, db: AsyncSession = Depends(get_db)) -> SessionOut:
    session = await _get_session_or_404(db, session_id)
    messages = await get_history(db, session_id)
    return SessionOut.from_model(session, messages)


@router.post("/sessions/{session_id}/end", response_model=SessionOut)
async def end_conversation_session(session_id: UUID, db: AsyncSession = Depends(get_db)) -> SessionOut:
    session = await _get_session_or_404(db, session_id)
    session = await end_session(db, session)
    return SessionOut.from_model(session)


@router.post("/sessions/{session_id}/messages", response_model=TurnOut)
async def send_text_message(
    session_id: UUID,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TurnOut:
    session = await _get_session_or_404(db, session_id)

    try:
        embedding_provider = get_embedding_provider(settings)
        llm_provider = get_llm_provider(settings)
    except (EmbeddingProviderError, LLMProviderError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        turn = await handle_text_turn(
            db, session, body.text, settings=settings, embedding_provider=embedding_provider, llm_provider=llm_provider
        )
    except ConversationTimeoutError as exc:
        logger.warning("Text turn timed out for session %s: %s", session_id, exc)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail="AI response took too long") from exc
    except ConversationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SearchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (EmbeddingProviderError, LLMProviderError) as exc:
        logger.error("Provider failure during text turn for session %s: %s", session_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="AI provider request failed") from exc

    return TurnOut.from_turn(turn)


@router.post("/sessions/{session_id}/audio", response_model=AudioTurnOut)
async def send_audio_message(
    session_id: UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AudioTurnOut:
    session = await _get_session_or_404(db, session_id)

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Uploaded audio is empty")

    try:
        stt_provider = get_stt_provider(settings)
        embedding_provider = get_embedding_provider(settings)
        llm_provider = get_llm_provider(settings)
        tts_provider = get_tts_provider(settings)
    except (STTProviderError, EmbeddingProviderError, LLMProviderError, TTSProviderError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        result = await handle_audio_turn(
            db,
            session,
            audio_bytes,
            settings=settings,
            stt_provider=stt_provider,
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            tts_provider=tts_provider,
        )
    except ConversationTimeoutError as exc:
        logger.warning("Audio turn timed out for session %s: %s", session_id, exc)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail="AI response took too long") from exc
    except ConversationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SearchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except STTProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Could not transcribe audio: {exc}") from exc
    except (EmbeddingProviderError, LLMProviderError, TTSProviderError) as exc:
        logger.error("Provider failure during audio turn for session %s: %s", session_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="AI provider request failed") from exc

    return AudioTurnOut(
        transcribed_text=result.transcribed_text,
        turn=TurnOut.from_turn(result.turn),
        assistant_audio_base64=base64.b64encode(result.assistant_audio.audio_bytes).decode("ascii"),
        assistant_audio_format=result.assistant_audio.audio_format,
    )
