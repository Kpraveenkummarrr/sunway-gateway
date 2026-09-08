import tempfile
from pathlib import Path

import pytest

from app.models.knowledge import KnowledgeDocument
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.services.knowledge_ingestion import ingest_pdf
from app.services.knowledge_search import SearchError, search_chunks
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_pdf, make_simple_pdf


@pytest.mark.asyncio
async def test_search_returns_top_k_results_ordered_by_similarity(db_session) -> None:
    provider = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf("The quick brown fox jumps over the lazy dog."),
            original_filename="search_test.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=800,
            chunk_overlap=100,
        )
        try:
            query_embedding = await provider.embed_one("The quick brown fox jumps over the lazy dog.")
            results = await search_chunks(db_session, query_embedding=query_embedding, top_k=5)

            assert len(results) >= 1
            assert results[0].document_id == document.id
            assert results[0].document_filename == "search_test.pdf"
            # Same text embedded the same way -> should be (near-)identical.
            assert results[0].similarity > 0.99
        finally:
            await db_session.delete(document)
            await db_session.commit()


@pytest.mark.asyncio
async def test_search_respects_top_k_limit(db_session) -> None:
    provider = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_pdf_with_many_paragraphs(),
            original_filename="many_chunks.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=50,
            chunk_overlap=10,
        )
        try:
            query_embedding = await provider.embed_one("arbitrary query text")
            results = await search_chunks(db_session, query_embedding=query_embedding, top_k=2)
            assert len(results) <= 2
        finally:
            await db_session.delete(document)
            await db_session.commit()


@pytest.mark.asyncio
async def test_search_excludes_non_ready_documents(db_session) -> None:
    document = KnowledgeDocument(
        filename="not_ready.pdf",
        storage_path="/tmp/does-not-matter.pdf",
        status="processing",
        metadata_json={},
    )
    db_session.add(document)
    await db_session.commit()
    try:
        provider = MockEmbeddingProvider(dimensions=1536)
        query_embedding = await provider.embed_one("anything")
        results = await search_chunks(db_session, query_embedding=query_embedding, top_k=10)
        assert all(r.document_id != document.id for r in results)
    finally:
        await db_session.delete(document)
        await db_session.commit()


@pytest.mark.asyncio
async def test_search_rejects_wrong_dimension_query_embedding(db_session) -> None:
    with pytest.raises(SearchError):
        await search_chunks(db_session, query_embedding=[0.1, 0.2, 0.3], top_k=5)


@pytest.mark.asyncio
async def test_search_applies_similarity_threshold(db_session) -> None:
    provider = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf("Completely unrelated filler content about gardening."),
            original_filename="threshold_test.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=800,
            chunk_overlap=100,
        )
        try:
            # An unrelated random-ish query should not pass an unreasonably
            # high similarity threshold against mock (hash-based) vectors.
            query_embedding = await provider.embed_one("zzz unrelated query zzz")
            results = await search_chunks(
                db_session, query_embedding=query_embedding, top_k=10, similarity_threshold=0.999
            )
            assert all(r.similarity >= 0.999 for r in results)
        finally:
            await db_session.delete(document)
            await db_session.commit()


def make_pdf_with_many_paragraphs() -> bytes:
    text = "\n\n".join(f"This is paragraph number {i} with some filler content." for i in range(20))
    return make_pdf([text])
