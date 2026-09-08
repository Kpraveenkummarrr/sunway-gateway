import tempfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.stt.base import STTProviderError
from app.providers.stt.mock import MockSTTProvider
from app.providers.tts.base import TTSProviderError
from app.providers.tts.mock import MockTTSProvider
from app.services.conversation import ConversationError, create_session, handle_audio_turn
from app.services.knowledge_ingestion import ingest_pdf
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_simple_pdf

TEST_SETTINGS = Settings(
    rag_top_k=4,
    rag_similarity_threshold=-1.0,
    ai_max_context_chars=2000,
    ai_max_history_messages=20,
    ai_system_prompt="You are a test assistant.",
)


@pytest.mark.asyncio
async def test_full_audio_pipeline_mock_stt_rag_llm_tts(db_session) -> None:
    """mock audio -> mock STT -> RAG -> mock LLM -> mock TTS, end to end."""
    embedder = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        question = "Our business hours are 9am to 5pm, Monday through Friday."
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf(question),
            original_filename="hours.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=embedder,
            chunk_size=800,
            chunk_overlap=100,
        )
        session = await create_session(db_session)
        stt = MockSTTProvider()
        llm = MockLLMProvider()
        tts = MockTTSProvider()
        try:
            # Mock STT convention: audio bytes ARE the UTF-8 text.
            fake_audio = question.encode("utf-8")

            result = await handle_audio_turn(
                db_session,
                session,
                fake_audio,
                settings=TEST_SETTINGS,
                stt_provider=stt,
                embedding_provider=embedder,
                llm_provider=llm,
                tts_provider=tts,
            )

            assert result.transcribed_text == question
            assert result.turn.retrieved_chunks  # RAG found the matching chunk
            assert result.turn.assistant_message.text
            # Mock TTS convention: audio bytes ARE the UTF-8 text of the reply.
            assert result.assistant_audio.audio_bytes.decode("utf-8") == result.turn.assistant_message.text
            assert result.turn.user_message.stt_latency_ms is not None
            assert result.turn.assistant_message.tts_latency_ms is not None
        finally:
            await db_session.delete(session)
            await db_session.delete(document)
            await db_session.commit()


@pytest.mark.asyncio
async def test_audio_turn_rejects_inactive_session(db_session) -> None:
    from app.services.conversation import end_session

    session = await create_session(db_session)
    try:
        await end_session(db_session, session)
        with pytest.raises(ConversationError):
            await handle_audio_turn(
                db_session,
                session,
                b"hello",
                settings=TEST_SETTINGS,
                stt_provider=MockSTTProvider(),
                embedding_provider=MockEmbeddingProvider(dimensions=1536),
                llm_provider=MockLLMProvider(),
                tts_provider=MockTTSProvider(),
            )
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_audio_turn_propagates_stt_failure(db_session) -> None:
    session = await create_session(db_session)
    try:
        with pytest.raises(STTProviderError):
            await handle_audio_turn(
                db_session,
                session,
                b"",  # empty audio -> mock STT rejects
                settings=TEST_SETTINGS,
                stt_provider=MockSTTProvider(),
                embedding_provider=MockEmbeddingProvider(dimensions=1536),
                llm_provider=MockLLMProvider(),
                tts_provider=MockTTSProvider(),
            )
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_audio_turn_propagates_tts_failure(db_session) -> None:
    session = await create_session(db_session)
    try:
        with pytest.raises(TTSProviderError):
            await handle_audio_turn(
                db_session,
                session,
                b"hello there",
                settings=TEST_SETTINGS,
                stt_provider=MockSTTProvider(),
                embedding_provider=MockEmbeddingProvider(dimensions=1536),
                llm_provider=MockLLMProvider(),
                tts_provider=MockTTSProvider(simulate_failure=True),
            )
    finally:
        await db_session.delete(session)
        await db_session.commit()
