import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.services.knowledge_ingestion import IngestionError, ingest_pdf
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_empty_pdf, make_pdf, make_simple_pdf


async def _cleanup(db_session, document_id) -> None:
    doc = await db_session.get(KnowledgeDocument, document_id)
    if doc is not None:
        await db_session.delete(doc)
        await db_session.commit()


@pytest.mark.asyncio
async def test_ingest_creates_document_and_chunks(db_session) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_pdf(["First page of real content.", "Second page of real content."]),
            original_filename="test.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=MockEmbeddingProvider(dimensions=1536),
            chunk_size=800,
            chunk_overlap=100,
        )
        try:
            assert document.status == "ready"
            assert document.page_count == 2
            assert document.filename == "test.pdf"
            assert document.metadata_json["chunk_count"] >= 1
            assert "content_hash" in document.metadata_json
            assert document.metadata_json["embedding_space"] == "mock:1536"
            assert document.metadata_json["embedding_dimensions"] == 1536

            result = await db_session.execute(
                select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
            )
            chunks = result.scalars().all()
            assert len(chunks) == document.metadata_json["chunk_count"]
            for chunk in chunks:
                assert chunk.embedding is not None
                assert len(chunk.embedding) == 1536
                assert chunk.chunk_text.strip() != ""
        finally:
            await _cleanup(db_session, document.id)


@pytest.mark.asyncio
async def test_ingest_rejects_non_pdf(db_session) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(IngestionError):
            await ingest_pdf(
                db_session,
                file_bytes=b"not a pdf at all",
                original_filename="fake.pdf",
                storage_dir=Path(tmp),
                extractor=PyPDFTextExtractor(),
                embedding_provider=MockEmbeddingProvider(dimensions=1536),
                chunk_size=800,
                chunk_overlap=100,
            )


@pytest.mark.asyncio
async def test_ingest_rejects_empty_pdf(db_session) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(IngestionError):
            await ingest_pdf(
                db_session,
                file_bytes=make_empty_pdf(),
                original_filename="blank.pdf",
                storage_dir=Path(tmp),
                extractor=PyPDFTextExtractor(),
                embedding_provider=MockEmbeddingProvider(dimensions=1536),
                chunk_size=800,
                chunk_overlap=100,
            )


@pytest.mark.asyncio
async def test_ingest_rejects_mismatched_embedding_dimensions(db_session) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(IngestionError):
            await ingest_pdf(
                db_session,
                file_bytes=make_simple_pdf("Some content"),
                original_filename="test.pdf",
                storage_dir=Path(tmp),
                extractor=PyPDFTextExtractor(),
                embedding_provider=MockEmbeddingProvider(dimensions=384),  # wrong size
                chunk_size=800,
                chunk_overlap=100,
            )


@pytest.mark.asyncio
async def test_deleting_document_cascades_to_chunks(db_session) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db_session,
            file_bytes=make_simple_pdf("Cascade delete test content."),
            original_filename="cascade.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=MockEmbeddingProvider(dimensions=1536),
            chunk_size=800,
            chunk_overlap=100,
        )
        document_id = document.id

        await db_session.delete(document)
        await db_session.commit()

        result = await db_session.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
        )
        assert result.scalars().all() == []
