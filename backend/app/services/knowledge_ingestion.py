"""PDF -> extract -> chunk -> embed -> pgvector ingestion pipeline."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from app.providers.embeddings.base import EmbeddingProvider, EmbeddingProviderError
from app.services.chunking import chunk_pages
from app.services.pdf_extraction import (
    EmptyPDFError,
    PDFExtractionError,
    PDFTextExtractor,
)


class IngestionError(Exception):
    """Wraps a failure that occurred before any document row was
    committed — the caller should turn this into an HTTP 4xx."""


async def ingest_pdf(
    db: AsyncSession,
    *,
    file_bytes: bytes,
    original_filename: str,
    storage_dir: Path,
    extractor: PDFTextExtractor,
    embedding_provider: EmbeddingProvider,
    chunk_size: int,
    chunk_overlap: int,
) -> KnowledgeDocument:
    """Runs the full ingestion pipeline and returns the persisted
    KnowledgeDocument (status "ready" on success).

    Raises IngestionError for validation failures (bad/empty PDF) before
    anything is written. Embedding/storage failures after that point are
    recorded on the document itself (status "failed") rather than raised,
    since by then we have something worth showing the caller.
    """
    if embedding_provider.dimensions != EMBEDDING_DIM:
        raise IngestionError(
            f"Embedding provider produces {embedding_provider.dimensions}-dim vectors, "
            f"but knowledge_chunks.embedding is a fixed vector({EMBEDDING_DIM}) column."
        )

    try:
        extracted = extractor.extract(file_bytes)
    except PDFExtractionError as exc:
        raise IngestionError(f"Invalid PDF: {exc}") from exc
    except EmptyPDFError as exc:
        raise IngestionError(str(exc)) from exc

    content_hash = hashlib.sha256(file_bytes).hexdigest()

    storage_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4()}.pdf"
    storage_path = storage_dir / stored_name
    storage_path.write_bytes(file_bytes)

    document = KnowledgeDocument(
        filename=original_filename,
        content_type="application/pdf",
        storage_path=str(storage_path),
        page_count=extracted.page_count,
        status="processing",
        metadata_json={
            "content_hash": content_hash,
            "embedding_space": embedding_provider.embedding_space,
            "embedding_dimensions": EMBEDDING_DIM,
        },
    )
    db.add(document)
    await db.flush()

    chunks = chunk_pages(extracted.pages, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if not chunks:
        document.status = "failed"
        document.metadata_json = {**(document.metadata_json or {}), "error": "No chunks produced"}
        await db.commit()
        await db.refresh(document)
        return document

    try:
        vectors = await embedding_provider.embed([c.text for c in chunks])
    except EmbeddingProviderError as exc:
        document.status = "failed"
        document.metadata_json = {**(document.metadata_json or {}), "error": str(exc)}
        await db.commit()
        await db.refresh(document)
        return document

    for chunk, vector in zip(chunks, vectors, strict=True):
        if len(vector) != EMBEDDING_DIM:
            document.status = "failed"
            document.metadata_json = {
                **(document.metadata_json or {}),
                "error": f"Embedding provider returned {len(vector)}-dim vector, expected {EMBEDDING_DIM}",
            }
            await db.commit()
            await db.refresh(document)
            return document

        db.add(
            KnowledgeChunk(
                document_id=document.id,
                page_number=chunk.page_number,
                chunk_index=chunk.index,
                chunk_text=chunk.text,
                embedding=vector,
            )
        )

    document.status = "ready"
    document.metadata_json = {**(document.metadata_json or {}), "chunk_count": len(chunks)}
    await db.commit()
    await db.refresh(document)
    return document
