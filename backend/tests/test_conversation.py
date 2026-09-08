import tempfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.ai import AIMessage
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.base import LLMMessage
from app.providers.llm.mock import NO_CONTEXT_RESPONSE, MockLLMProvider
from app.services.conversation import (
    ConversationError,
    create_session,
    end_session,
    get_history,
    handle_text_turn,
)
from app.services.knowledge_ingestion import ingest_pdf
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_simple_pdf

TEST_SETTINGS = Settings(
    rag_top_k=4,
    rag_similarity_threshold=-1.0,  # accept anything for deterministic mock-embedding tests
    ai_max_context_chars=2000,
    ai_max_history_messages=20,
    ai_system_prompt="You are a test assistant.",
)


# ---- Session management ----


@pytest.mark.asyncio
async def test_create_session_defaults_to_active_status(db_session) -> None:
    session = await create_session(db_session, language="en")
    try:
        assert session.status == "active"
        assert session.language == "en"
        assert session.call_id is None
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_sessions_are_isolated_from_each_other(db_session) -> None:
    session_a = await create_session(db_session)
    session_b = await create_session(db_session)
    llm = MockLLMProvider()
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        await handle_text_turn(
            db_session, session_a, "message in session A", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
        )
        await handle_text_turn(
            db_session, session_b, "message in session B", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
        )

        history_a = await get_history(db_session, session_a.id)
        history_b = await get_history(db_session, session_b.id)

        assert all(m.session_id == session_a.id for m in history_a)
        assert all(m.session_id == session_b.id for m in history_b)
        assert "message in session A" in [m.text for m in history_a]
        assert "message in session A" not in [m.text for m in history_b]
        assert "message in session B" not in [m.text for m in history_a]
    finally:
        await db_session.delete(session_a)
        await db_session.delete(session_b)
        await db_session.commit()


@pytest.mark.asyncio
async def test_message_persistence(db_session) -> None:
    session = await create_session(db_session)
    llm = MockLLMProvider()
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        turn = await handle_text_turn(
            db_session, session, "hello", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
        )
        history = await get_history(db_session, session.id)
        assert len(history) == 2
        assert history[0].role == "caller"
        assert history[0].text == "hello"
        assert history[1].role == "agent"
        assert history[1].text == turn.assistant_message.text
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_end_session_marks_completed(db_session) -> None:
    session = await create_session(db_session)
    try:
        ended = await end_session(db_session, session)
        assert ended.status == "completed"
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_message_on_ended_session_raises(db_session) -> None:
    session = await create_session(db_session)
    llm = MockLLMProvider()
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        await end_session(db_session, session)
        with pytest.raises(ConversationError):
            await handle_text_turn(
                db_session, session, "hello", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
            )
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_empty_message_text_raises(db_session) -> None:
    session = await create_session(db_session)
    llm = MockLLMProvider()
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        with pytest.raises(ConversationError):
            await handle_text_turn(
                db_session, session, "   ", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
            )
    finally:
        await db_session.delete(session)
        await db_session.commit()


# ---- RAG integration ----


@pytest.mark.asyncio
async def test_no_knowledge_found_uses_no_context_response(db_session) -> None:
    """With no knowledge documents ingested at all, retrieval returns
    nothing, and the LLM should get retrieved_context=None."""
    session = await create_session(db_session)
    llm = MockLLMProvider()
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        turn = await handle_text_turn(
            db_session, session, "anything at all", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
        )
        assert turn.retrieved_chunks == []
        assert turn.assistant_message.text == NO_CONTEXT_RESPONSE
        assert llm.last_retrieved_context is None
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_relevant_knowledge_is_retrieved_and_passed_to_llm(db_session) -> None:
    embedder = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf("Our business hours are 9am to 5pm, Monday through Friday."),
            original_filename="hours.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=embedder,
            chunk_size=800,
            chunk_overlap=100,
        )
        session = await create_session(db_session)
        llm = MockLLMProvider()
        try:
            turn = await handle_text_turn(
                db_session,
                session,
                "Our business hours are 9am to 5pm, Monday through Friday.",
                settings=TEST_SETTINGS,
                embedding_provider=embedder,
                llm_provider=llm,
            )
            assert len(turn.retrieved_chunks) >= 1
            assert turn.retrieved_chunks[0].document_filename == "hours.pdf"
            assert llm.last_retrieved_context is not None
            assert "hours.pdf" in llm.last_retrieved_context
            assert turn.assistant_message.retrieved_chunk_ids
        finally:
            await db_session.delete(session)
            await db_session.delete(document)
            await db_session.commit()


@pytest.mark.asyncio
async def test_context_size_is_bounded(db_session) -> None:
    embedder = MockEmbeddingProvider(dimensions=1536)
    small_context_settings = Settings(
        rag_top_k=4,
        rag_similarity_threshold=-1.0,
        ai_max_context_chars=100,
        ai_max_history_messages=20,
        ai_system_prompt="You are a test assistant.",
    )
    with tempfile.TemporaryDirectory() as tmp:
        long_text = "This is a long sentence about business policy. " * 20
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf(long_text),
            original_filename="policy.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=embedder,
            chunk_size=800,
            chunk_overlap=100,
        )
        session = await create_session(db_session)
        llm = MockLLMProvider()
        try:
            await handle_text_turn(
                db_session,
                session,
                long_text,
                settings=small_context_settings,
                embedding_provider=embedder,
                llm_provider=llm,
            )
            assert llm.last_retrieved_context is not None
            assert len(llm.last_retrieved_context) <= 160  # max_chars + truncation marker/header allowance
        finally:
            await db_session.delete(session)
            await db_session.delete(document)
            await db_session.commit()


# ---- Conversation history windowing passed to LLM ----


@pytest.mark.asyncio
async def test_conversation_history_passed_to_llm_in_order(db_session) -> None:
    session = await create_session(db_session)
    llm = MockLLMProvider()
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        await handle_text_turn(
            db_session, session, "first message", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
        )
        await handle_text_turn(
            db_session, session, "second message", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=llm
        )
        assert llm.last_history is not None
        contents = [m.content for m in llm.last_history]
        assert contents.index("first message") < contents.index("second message")
        assert all(isinstance(m, LLMMessage) for m in llm.last_history)
    finally:
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_llm_provider_error_propagates(db_session) -> None:
    from app.providers.llm.base import LLMProviderError

    session = await create_session(db_session)
    failing_llm = MockLLMProvider(simulate_failure=True)
    embedder = MockEmbeddingProvider(dimensions=1536)
    try:
        with pytest.raises(LLMProviderError):
            await handle_text_turn(
                db_session, session, "hello", settings=TEST_SETTINGS, embedding_provider=embedder, llm_provider=failing_llm
            )
    finally:
        await db_session.delete(session)
        await db_session.commit()
