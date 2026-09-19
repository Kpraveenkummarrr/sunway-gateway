"""Hindi Lumpy Skin Disease retrieval, end to end through PostgreSQL.

The client's helpline answered "information unavailable" to questions its own
document answers. Retrieval used to depend on vector similarity, which is noise for
placeholder embeddings (these tests use the deterministic mock provider on purpose:
they prove retrieval does not need a meaningful vector) and its lexical fallback
looked only at the 50 rows that noise ranked first.

The knowledge base here is `tests/kb_fixtures.py`: 12 Hindi passages written for
testing - NOT the client's document. Recall on the real 123 chunks is measured on
the client's machine (`scripts/rag_uat_probe.py`).
"""

import uuid

import pytest
from sqlalchemy import delete

from app.models.ai import AIMessage
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.base import LLMProvider, LLMResponse
from app.services.conversation import create_session, handle_text_turn
from app.services.knowledge_search import build_retrieval_query, search_chunks
from tests.kb_fixtures import ANSWERABLE, LSD_KB, UNANSWERABLE
from tests.test_helpline_persona import _settings


class _RecordingLLM(LLMProvider):
    provider_name = "recording"

    def __init__(self) -> None:
        self.contexts: list[str | None] = []

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:  # noqa: ANN001
        self.contexts.append(retrieved_context)
        return LLMResponse(text="जी, बताती हूँ।")


@pytest.fixture
async def hindi_corpus(db_session):
    """The 12 passages, stored as a ready document. Yields (provider, key_by_chunk_id, document)."""
    provider = MockEmbeddingProvider(dimensions=1536)
    document = KnowledgeDocument(
        filename="lsd-hindi-test.pdf",
        storage_path="test-fixture",
        page_count=1,
        status="ready",
        metadata_json={"embedding_space": provider.embedding_space, "chunk_count": len(LSD_KB)},
    )
    db_session.add(document)
    await db_session.flush()
    key_by_chunk: dict[uuid.UUID, str] = {}
    for index, (key, text) in enumerate(LSD_KB):
        chunk = KnowledgeChunk(
            document_id=document.id,
            page_number=1,
            chunk_index=index,
            chunk_text=text,
            embedding=await provider.embed_one(text),
        )
        db_session.add(chunk)
        await db_session.flush()
        key_by_chunk[chunk.id] = key
    await db_session.commit()
    try:
        yield provider, key_by_chunk, document
    finally:
        await db_session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        await db_session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == document.id))
        await db_session.commit()


async def _retrieve(
    db_session,
    provider,
    question,
    previous=(),
    *,
    top_k=4,
    similarity_threshold=0.75,
    lexical_min_coverage=0.34,
):
    query = build_retrieval_query(question, list(previous))
    return await search_chunks(
        db_session,
        query_embedding=await provider.embed_one(query),
        query_text=query,
        top_k=top_k,
        similarity_threshold=similarity_threshold,
        embedding_space=provider.embedding_space,
        lexical_min_coverage=lexical_min_coverage,
    )


@pytest.mark.parametrize("question, previous, expected", ANSWERABLE, ids=[q for q, _, _ in ANSWERABLE])
async def test_a_question_reaches_the_passage_that_answers_it(hindi_corpus, db_session, question, previous, expected) -> None:
    provider, key_by_chunk, _ = hindi_corpus
    results = await _retrieve(db_session, provider, question, previous)
    keys = [key_by_chunk[r.chunk_id] for r in results if r.chunk_id in key_by_chunk]
    assert expected in keys, f"{question!r} retrieved {keys}"


@pytest.mark.parametrize("question", UNANSWERABLE)
async def test_a_question_the_knowledge_base_does_not_answer_retrieves_nothing(hindi_corpus, db_session, question) -> None:
    provider, key_by_chunk, _ = hindi_corpus
    results = await _retrieve(db_session, provider, question)
    assert [key_by_chunk.get(r.chunk_id) for r in results] == []


async def test_retrieval_works_even_though_the_embedding_carries_no_meaning(hindi_corpus, db_session) -> None:
    """The point of the lexical channel: with these hash-based vectors the vector
    channel alone finds nothing above the threshold, yet the question is answered."""
    provider, key_by_chunk, _ = hindi_corpus
    question = "लम्पी रोग के लक्षण क्या हैं"
    vector_only = await search_chunks(
        db_session,
        query_embedding=await provider.embed_one(question),
        query_text=question,
        top_k=4,
        similarity_threshold=0.75,
        embedding_space=provider.embedding_space,
        use_lexical_index=False,
    )
    with_lexical = await _retrieve(db_session, provider, question)
    assert "symptoms" in [key_by_chunk[r.chunk_id] for r in with_lexical]
    assert len(with_lexical) >= len(vector_only)


async def test_a_new_passage_is_findable_at_once_and_a_deleted_one_disappears(hindi_corpus, db_session) -> None:
    """The in-memory index is rebuilt when the knowledge base changes."""
    provider, key_by_chunk, document = hindi_corpus
    question = "पशु को अमरूद के पत्ते खिलाने के बारे में बताइए"
    # This is an index-invalidation test, not a mock-vector ranking test.  A
    # threshold above one removes deterministic hash-vector false positives;
    # the exact lexical hit added below still qualifies through that channel.
    assert await _retrieve(
        db_session, provider, question, similarity_threshold=1.1, lexical_min_coverage=0.8
    ) == []

    chunk = KnowledgeChunk(
        document_id=document.id,
        page_number=2,
        chunk_index=99,
        chunk_text="अमरूद के पत्ते पशु को दस्त होने पर पशु चिकित्सक की सलाह से दिए जाते हैं।",
        embedding=await provider.embed_one("अमरूद"),
    )
    db_session.add(chunk)
    await db_session.commit()
    found = await _retrieve(
        db_session, provider, question, similarity_threshold=1.1, lexical_min_coverage=0.8
    )
    assert chunk.id in {r.chunk_id for r in found}

    await db_session.delete(chunk)
    await db_session.commit()
    assert await _retrieve(
        db_session, provider, question, similarity_threshold=1.1, lexical_min_coverage=0.8
    ) == []


async def test_documents_that_are_not_ready_are_not_searched(hindi_corpus, db_session) -> None:
    provider, key_by_chunk, document = hindi_corpus
    document.status = "processing"
    await db_session.commit()
    try:
        assert await _retrieve(db_session, provider, "लम्पी रोग के लक्षण क्या हैं") == []
    finally:
        document.status = "ready"
        await db_session.commit()


async def test_a_conversation_keeps_its_subject_across_follow_ups_and_acknowledgements(hindi_corpus, db_session) -> None:
    """The regression behind "the AI does not read the database": the second,
    pronoun-only turn retrieved nothing, so the caller heard a refusal."""
    provider, key_by_chunk, _ = hindi_corpus
    settings = _settings(rag_similarity_threshold=0.75, rag_top_k=4)
    llm = _RecordingLLM()
    session = await create_session(db_session, language="hi")
    try:
        expected = [
            ("लम्पी रोग के लक्षण क्या हैं?", "symptoms"),
            ("इसका इलाज क्या है?", "what_to_do"),
            ("जी हाँ", None),  # an acknowledgement: no particular passage
            ("और बचाव?", "prevention"),
            ("यह कैसे फैलता है?", "spread"),
        ]
        for text, key in expected:
            turn = await handle_text_turn(
                db_session, session, text,
                settings=settings, embedding_provider=provider, llm_provider=llm,
            )
            keys = [key_by_chunk.get(c.chunk_id) for c in turn.retrieved_chunks]
            if key is not None:
                assert key in keys, f"{text!r} retrieved {keys}"
        assert all(llm.contexts[i] for i, (_, key) in enumerate(expected) if key is not None)
    finally:
        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()


async def test_a_question_with_no_supporting_passage_is_answered_honestly_without_the_model(hindi_corpus, db_session) -> None:
    """No context is not permission to answer a veterinary question from memory."""
    provider, key_by_chunk, _ = hindi_corpus
    settings = _settings(rag_similarity_threshold=0.75, rag_top_k=4, llm_provider="gemini")
    llm = _RecordingLLM()
    session = await create_session(db_session, language="hi")
    try:
        turn = await handle_text_turn(
            db_session, session, "आज मौसम कैसा है",
            settings=settings, embedding_provider=provider, llm_provider=llm,
        )
        assert turn.retrieved_chunks == []
        assert llm.contexts == [], "the model must not be asked to answer without evidence"
        assert "उपलब्ध नहीं" in turn.assistant_message.text
    finally:
        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()
