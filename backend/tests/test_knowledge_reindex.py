"""Re-indexing: the operator's cure for "the AI is not reading the database".

Search excludes a document whose vectors came from a different embedding
model. That is correct, but invisible — so what is tested here is that the
condition is *reported*, that re-indexing clears it, and that the document is
genuinely searchable again afterwards.
"""

import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from httpx import ASGITransport

from app.core.config import Settings, get_settings
from app.main import app
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.providers.embeddings.base import EmbeddingProviderError
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.services.knowledge_ingestion import ingest_pdf
from app.services.knowledge_reindex import (
    ReindexError,
    index_status,
    reindex_all,
    reindex_document,
)
from app.services.knowledge_search import search_chunks
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_simple_pdf

SOURCE = "Lumpy Skin Disease spreads through biting flies, mosquitoes and ticks."


class _BrokenEmbeddings(MockEmbeddingProvider):
    async def embed(self, texts):
        raise EmbeddingProviderError("provider is down")


class _OtherModel(MockEmbeddingProvider):
    @property
    def embedding_space(self) -> str:
        return "other-model:1536"


@pytest.fixture
async def document(db_session):
    provider = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        doc = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf(SOURCE),
            original_filename="lsd-reindex.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=400,
            chunk_overlap=50,
        )
        try:
            yield doc, provider
        finally:
            await db_session.delete(doc)
            await db_session.commit()


async def _first_chunk(db_session, doc) -> KnowledgeChunk:
    """Loaded explicitly: `doc.chunks` would lazy-load inside the event loop."""
    return (
        await db_session.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == doc.id)
            .order_by(KnowledgeChunk.chunk_index)
        )
    ).scalars().first()


async def _find(db_session, provider, doc_id) -> bool:
    results = await search_chunks(
        db_session,
        query_embedding=await provider.embed_one("Lumpy Skin Disease"),
        query_text="Lumpy Skin Disease",
        top_k=4,
        similarity_threshold=0.75,
        embedding_space=provider.embedding_space,
    )
    return doc_id in {r.document_id for r in results}


# ---- status ----


@pytest.mark.asyncio
async def test_a_freshly_ingested_document_is_reported_as_up_to_date(document, db_session) -> None:
    doc, provider = document
    report = await index_status(db_session, embedding_provider=provider)
    row = next(d for d in report.documents if d.document_id == str(doc.id))

    assert not row.stale
    assert row.reason == "up to date"
    assert row.chunk_count == row.embedded_chunk_count > 0
    assert row.last_indexed_at is not None


@pytest.mark.asyncio
async def test_a_document_indexed_by_another_model_is_reported_stale(document, db_session) -> None:
    doc, _ = document
    other = _OtherModel(dimensions=1536)

    report = await index_status(db_session, embedding_provider=other)
    row = next(d for d in report.documents if d.document_id == str(doc.id))

    assert row.stale
    assert "queries use other-model:1536" in row.reason
    assert not report.is_healthy
    assert report.summary()["stale_documents"] >= 1
    # And it really is invisible to search, which is what the client saw.
    assert not await _find(db_session, other, doc.id)


@pytest.mark.asyncio
async def test_a_document_whose_chunks_lost_their_vectors_is_reported_stale(
    document, db_session
) -> None:
    doc, provider = document
    chunk = await _first_chunk(db_session, doc)
    chunk.embedding = None
    await db_session.commit()

    report = await index_status(db_session, embedding_provider=provider)
    row = next(d for d in report.documents if d.document_id == str(doc.id))
    assert row.stale
    assert "no embedding" in row.reason


@pytest.mark.asyncio
async def test_a_failed_document_is_reported_stale(document, db_session) -> None:
    doc, provider = document
    doc.status = "failed"
    await db_session.commit()

    report = await index_status(db_session, embedding_provider=provider)
    row = next(d for d in report.documents if d.document_id == str(doc.id))
    assert row.stale and row.reason == "status is failed"


# ---- re-indexing ----


@pytest.mark.asyncio
async def test_reindexing_makes_a_stale_document_searchable_again(document, db_session) -> None:
    doc, _ = document
    other = _OtherModel(dimensions=1536)
    assert not await _find(db_session, other, doc.id)

    chunks = await reindex_document(db_session, doc, embedding_provider=other)

    assert chunks > 0
    assert doc.metadata_json["embedding_space"] == "other-model:1536"
    assert doc.status == "ready"
    assert await _find(db_session, other, doc.id)

    report = await index_status(db_session, embedding_provider=other)
    assert next(d for d in report.documents if d.document_id == str(doc.id)).reason == "up to date"


@pytest.mark.asyncio
async def test_reindex_all_reports_what_it_touched(document, db_session) -> None:
    doc, _ = document
    other = _OtherModel(dimensions=1536)

    report = await reindex_all(db_session, embedding_provider=other)

    assert report.ok
    assert "lsd-reindex.pdf" in report.reindexed
    assert report.chunk_count > 0
    assert report.embedding_space == "other-model:1536"


@pytest.mark.asyncio
async def test_only_stale_skips_documents_that_are_already_current(document, db_session) -> None:
    doc, provider = document

    report = await reindex_all(db_session, embedding_provider=provider, only_stale=True)

    assert "lsd-reindex.pdf" not in report.reindexed


@pytest.mark.asyncio
async def test_a_provider_failure_leaves_the_old_vectors_in_place(document, db_session) -> None:
    doc, provider = document
    before = list((await _first_chunk(db_session, doc)).embedding)

    with pytest.raises(ReindexError, match="provider is down"):
        await reindex_document(db_session, doc, embedding_provider=_BrokenEmbeddings(dimensions=1536))

    await db_session.refresh(doc)
    after = list((await _first_chunk(db_session, doc)).embedding)
    assert after == before
    assert doc.status == "failed"
    assert doc.metadata_json["error"] == "provider is down"

    # Recoverable: a working provider puts it back.
    await reindex_document(db_session, doc, embedding_provider=provider)
    assert doc.status == "ready"
    assert await _find(db_session, provider, doc.id)


@pytest.mark.asyncio
async def test_a_wrong_dimension_provider_is_refused_before_touching_anything(
    document, db_session
) -> None:
    doc, _ = document
    with pytest.raises(ReindexError, match="fixed vector"):
        await reindex_document(db_session, doc, embedding_provider=MockEmbeddingProvider(dimensions=768))
    assert doc.status == "ready"


# ---- API ----


def _override_settings():
    overridden = get_settings().model_copy(
        update={"rag_embedding_provider": "mock", "internal_api_key": ""}
    )

    def _get() -> Settings:
        return overridden

    return _get


@pytest.mark.asyncio
async def test_index_status_and_reindex_are_exposed_to_the_admin_panel(document, db_session) -> None:
    doc, _ = document
    app.dependency_overrides[get_settings] = _override_settings()
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status_response = await client.get("/api/knowledge/index/status")
            assert status_response.status_code == 200
            body = status_response.json()
            assert body["current_embedding_space"]
            assert any(item["filename"] == "lsd-reindex.pdf" for item in body["items"])

            reindex_response = await client.post(
                "/api/knowledge/index/reindex", json={"document_id": str(doc.id)}
            )
            assert reindex_response.status_code == 200
            assert reindex_response.json()["ok"] is True
            assert reindex_response.json()["chunk_count"] > 0
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_reindexing_an_unknown_document_is_a_404() -> None:
    app.dependency_overrides[get_settings] = _override_settings()
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/knowledge/index/reindex",
                json={"document_id": "00000000-0000-0000-0000-000000000000"},
            )
            assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_settings, None)
