"""RAG parity across LLM providers — Part 3's "otherwise the comparison is
invalid" requirement, made concrete.

Swapping the LLM provider must never change what gets retrieved. These run
the exact same turn through two different provider stubs (one standing in
for Gemini, one for Sarvam-M) and prove they were handed byte-identical
history and retrieved context — and that the RAG evidence log line (Part 3:
provider, call_id, turn_id, retrieval_count, document_ids, chunk_ids) is
actually emitted, with the right provider name for whichever provider ran.
"""

import logging
import tempfile
from pathlib import Path

import pytest

from app.models.ai import AIMessage
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.services.conversation import create_session, handle_text_turn
from app.services.knowledge_ingestion import ingest_pdf
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_simple_pdf
from tests.test_helpline_persona import _settings

SOURCE = "Lumpy Skin Disease spreads through biting flies, mosquitoes and ticks."


class _RecordingProvider(LLMProvider):
    """A stub standing in for a real provider (Gemini or Sarvam-M): records
    exactly what it was handed and returns a fixed reply."""

    def __init__(self, name: str, reply: str = "जी हाँ, समझ गई।") -> None:
        self._name = name
        self.reply = reply
        self.calls: list[dict] = []

    @property
    def provider_name(self) -> str:
        return self._name

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:
        self.calls.append(
            {"system_prompt": system_prompt, "history": list(history), "retrieved_context": retrieved_context}
        )
        return LLMResponse(text=self.reply, finish_reason="stop")


@pytest.fixture
async def lsd_document(db_session):
    provider = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf(SOURCE),
            original_filename="lsd-parity.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=400,
            chunk_overlap=50,
        )
        try:
            yield document, provider
        finally:
            await db_session.delete(document)
            await db_session.commit()


async def _run_turn(db_session, session, provider, embedding_provider, text: str):
    settings = _settings(rag_similarity_threshold=0.75, rag_top_k=4)
    return await handle_text_turn(
        db_session,
        session,
        text,
        settings=settings,
        embedding_provider=embedding_provider,
        llm_provider=provider,
    )


@pytest.mark.asyncio
async def test_gemini_and_sarvam_m_stand_ins_receive_identical_context_for_the_same_turn(
    lsd_document, db_session
) -> None:
    document, embedding_provider = lsd_document
    gemini_stub = _RecordingProvider("Gemini")
    sarvam_stub = _RecordingProvider("Sarvam-M local")

    session_a = await create_session(db_session, language="hi")
    session_b = await create_session(db_session, language="hi")
    try:
        turn_a = await _run_turn(db_session, session_a, gemini_stub, embedding_provider, "लम्पी रोग कैसे फैलता है?")
        turn_b = await _run_turn(db_session, session_b, sarvam_stub, embedding_provider, "लम्पी रोग कैसे फैलता है?")

        assert turn_a.retrieved_chunks and turn_b.retrieved_chunks
        assert {c.document_id for c in turn_a.retrieved_chunks} == {document.id}
        assert {c.document_id for c in turn_b.retrieved_chunks} == {document.id}

        # The critical assertion: identical system prompt (base + context)
        # and identical history reached both providers.
        assert gemini_stub.calls[0]["system_prompt"] == sarvam_stub.calls[0]["system_prompt"]
        assert gemini_stub.calls[0]["retrieved_context"] == sarvam_stub.calls[0]["retrieved_context"]
        gemini_history = [(m.role, m.content) for m in gemini_stub.calls[0]["history"]]
        sarvam_history = [(m.role, m.content) for m in sarvam_stub.calls[0]["history"]]
        assert gemini_history == sarvam_history
    finally:
        from sqlalchemy import delete

        for session in (session_a, session_b):
            await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
            await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_the_rag_evidence_log_line_names_the_provider_that_actually_ran(
    lsd_document, db_session, caplog
) -> None:
    document, embedding_provider = lsd_document
    sarvam_stub = _RecordingProvider("Sarvam-M local")
    session = await create_session(db_session, language="hi")
    try:
        with caplog.at_level(logging.INFO, logger="app.services.conversation"):
            turn = await _run_turn(db_session, session, sarvam_stub, embedding_provider, "लम्पी रोग कैसे फैलता है?")

        evidence_lines = [r.message for r in caplog.records if r.message.startswith("RAG turn provider=")]
        assert len(evidence_lines) == 1
        line = evidence_lines[0]
        assert "provider=Sarvam-M local" in line
        assert f"session={session.id}" in line
        assert "turn=1" in line
        assert f"retrieval_count={len(turn.retrieved_chunks)}" in line
        assert str(document.id) in line
        for chunk in turn.retrieved_chunks:
            assert str(chunk.chunk_id) in line
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_the_turn_number_in_the_evidence_line_increments_across_a_conversation(
    lsd_document, db_session, caplog
) -> None:
    document, embedding_provider = lsd_document
    provider = _RecordingProvider("Gemini")
    session = await create_session(db_session, language="hi")
    try:
        with caplog.at_level(logging.INFO, logger="app.services.conversation"):
            await _run_turn(db_session, session, provider, embedding_provider, "लम्पी रोग क्या है?")
            await _run_turn(db_session, session, provider, embedding_provider, "यह कैसे फैलता है?")

        evidence_lines = [r.message for r in caplog.records if r.message.startswith("RAG turn provider=")]
        assert len(evidence_lines) == 2
        assert "turn=1" in evidence_lines[0]
        assert "turn=2" in evidence_lines[1]
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()
