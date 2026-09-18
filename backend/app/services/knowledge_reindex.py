"""Re-embedding existing knowledge documents, and spotting stale ones.

A document is searchable only while its stored vectors come from the same
embedding model the query uses — `search_chunks()` deliberately excludes a
document whose `embedding_space` is not the current one rather than ranking
it wrongly. That is the safe behaviour, but on its own it looks exactly like
the complaint the client raised: the agent stops finding anything in the
knowledge base. Re-indexing is the cure, so it has to be something the
operator can run and verify, not a database chore.

Re-indexing re-embeds the chunk text already in the database. The stored PDF
is only re-read when a document has no chunks at all (a failed ingest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from app.providers.embeddings.base import EmbeddingProvider, EmbeddingProviderError
from app.services.chunking import chunk_pages
from app.services.pdf_extraction import PDFExtractionError, PDFTextExtractor

logger = get_logger(__name__)

# Chunks are embedded in batches so a large document does not become one
# enormous provider request that times out halfway through.
BATCH_SIZE = 64


class ReindexError(Exception):
    pass


@dataclass(frozen=True)
class DocumentIndexStatus:
    document_id: str
    filename: str
    status: str
    chunk_count: int
    embedded_chunk_count: int
    embedding_space: str | None
    stale: bool
    reason: str
    last_indexed_at: datetime | None


@dataclass
class IndexStatus:
    current_embedding_space: str
    documents: list[DocumentIndexStatus] = field(default_factory=list)

    @property
    def stale_documents(self) -> list[DocumentIndexStatus]:
        return [d for d in self.documents if d.stale]

    @property
    def is_healthy(self) -> bool:
        return not self.stale_documents

    def summary(self) -> dict:
        return {
            "current_embedding_space": self.current_embedding_space,
            "documents": len(self.documents),
            "stale_documents": len(self.stale_documents),
            "searchable_documents": len(self.documents) - len(self.stale_documents),
            "healthy": self.is_healthy,
        }


def _classify(
    doc: KnowledgeDocument,
    *,
    chunk_count: int,
    embedded_count: int,
    current_space: str,
) -> tuple[bool, str]:
    """Is this document effectively invisible to search, and why?"""
    meta = doc.metadata_json or {}
    space = meta.get("embedding_space")
    if doc.status != "ready":
        return True, f"status is {doc.status}"
    if chunk_count == 0:
        return True, "no chunks"
    if embedded_count < chunk_count:
        return True, f"{chunk_count - embedded_count} of {chunk_count} chunks have no embedding"
    if space is None:
        # Unknown provenance must not be treated as a compatible vector space.
        return True, "unknown embedding space; re-index required"
    if space != current_space:
        return True, f"indexed with {space}, queries use {current_space}"
    return False, "up to date"


async def index_status(db: AsyncSession, *, embedding_provider: EmbeddingProvider) -> IndexStatus:
    """Per-document index health against the provider queries actually use."""
    current_space = embedding_provider.embedding_space
    counts = dict(
        (
            await db.execute(
                select(KnowledgeChunk.document_id, func.count(KnowledgeChunk.id)).group_by(
                    KnowledgeChunk.document_id
                )
            )
        ).all()
    )
    embedded = dict(
        (
            await db.execute(
                select(KnowledgeChunk.document_id, func.count(KnowledgeChunk.id))
                .where(KnowledgeChunk.embedding.is_not(None))
                .group_by(KnowledgeChunk.document_id)
            )
        ).all()
    )

    documents = (
        (await db.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.created_at)))
        .scalars()
        .all()
    )

    rows: list[DocumentIndexStatus] = []
    for doc in documents:
        chunk_count = int(counts.get(doc.id, 0))
        embedded_count = int(embedded.get(doc.id, 0))
        stale, reason = _classify(
            doc, chunk_count=chunk_count, embedded_count=embedded_count, current_space=current_space
        )
        meta = doc.metadata_json or {}
        rows.append(
            DocumentIndexStatus(
                document_id=str(doc.id),
                filename=doc.filename,
                status=doc.status,
                chunk_count=chunk_count,
                embedded_chunk_count=embedded_count,
                embedding_space=meta.get("embedding_space"),
                stale=stale,
                reason=reason,
                last_indexed_at=doc.updated_at,
            )
        )
    return IndexStatus(current_embedding_space=current_space, documents=rows)


async def reindex_document(
    db: AsyncSession,
    document: KnowledgeDocument,
    *,
    embedding_provider: EmbeddingProvider,
    extractor: PDFTextExtractor | None = None,
    chunk_size: int = 800,
    chunk_overlap: int = 150,
) -> int:
    """Re-embed one document with the current provider. Returns chunk count.

    On failure the document keeps the vectors it had and records why, rather
    than being emptied into a state where it is neither searchable nor
    recoverable.
    """
    if embedding_provider.dimensions != EMBEDDING_DIM:
        raise ReindexError(
            f"Embedding provider produces {embedding_provider.dimensions}-dim vectors, "
            f"but knowledge_chunks.embedding is a fixed vector({EMBEDDING_DIM}) column."
        )

    chunks = (
        (
            await db.execute(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id)
                .order_by(KnowledgeChunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )

    if not chunks:
        chunks = await _rechunk_from_stored_pdf(
            db,
            document,
            extractor=extractor,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    texts = [c.chunk_text for c in chunks]
    vectors: list[list[float]] = []
    try:
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(await embedding_provider.embed(texts[start : start + BATCH_SIZE]))
    except EmbeddingProviderError as exc:
        await _mark_failed(db, document, str(exc))
        raise ReindexError(f"Embedding failed for {document.filename}: {exc}") from exc

    if len(vectors) != len(chunks):
        await _mark_failed(db, document, "Embedding provider returned the wrong number of vectors")
        raise ReindexError(
            f"Embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks"
        )

    # Validate the whole batch before mutating any stored vector. Otherwise
    # a later invalid vector can commit an earlier replacement on failure.
    for vector in vectors:
        if len(vector) != EMBEDDING_DIM or not all(math.isfinite(x) for x in vector):
            await _mark_failed(
                db, document, f"Embedding provider returned a {len(vector)}-dim vector"
            )
            raise ReindexError(f"Embedding provider returned a {len(vector)}-dim vector")

    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.embedding = vector

    document.status = "ready"
    document.metadata_json = {
        **(document.metadata_json or {}),
        "embedding_space": embedding_provider.embedding_space,
        "embedding_dimensions": EMBEDDING_DIM,
        "chunk_count": len(chunks),
        "error": None,
    }
    await db.commit()
    await db.refresh(document)
    logger.info(
        "reindexed document=%s chunks=%d embedding_space=%s",
        document.id,
        len(chunks),
        embedding_provider.embedding_space,
    )
    return len(chunks)


async def _rechunk_from_stored_pdf(
    db: AsyncSession,
    document: KnowledgeDocument,
    *,
    extractor: PDFTextExtractor | None,
    chunk_size: int,
    chunk_overlap: int,
) -> list[KnowledgeChunk]:
    if extractor is None:
        raise ReindexError(f"{document.filename} has no chunks and no extractor was supplied")
    path = Path(document.storage_path)
    if not path.is_file():
        raise ReindexError(f"{document.filename} has no chunks and its stored file is missing")
    try:
        extracted = extractor.extract(path.read_bytes())
    except (PDFExtractionError, OSError) as exc:
        raise ReindexError(f"{document.filename} could not be re-read: {exc}") from exc

    pieces = chunk_pages(extracted.pages, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if not pieces:
        raise ReindexError(f"{document.filename} produced no chunks")

    rows = [
        KnowledgeChunk(
            document_id=document.id,
            page_number=piece.page_number,
            chunk_index=piece.index,
            chunk_text=piece.text,
        )
        for piece in pieces
    ]
    for row in rows:
        db.add(row)
    await db.flush()
    return rows


async def _mark_failed(db: AsyncSession, document: KnowledgeDocument, error: str) -> None:
    document.status = "failed"
    document.metadata_json = {**(document.metadata_json or {}), "error": error}
    await db.commit()


@dataclass
class ReindexReport:
    embedding_space: str
    reindexed: list[str] = field(default_factory=list)
    chunk_count: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures


async def reindex_all(
    db: AsyncSession,
    *,
    embedding_provider: EmbeddingProvider,
    extractor: PDFTextExtractor | None = None,
    chunk_size: int = 800,
    chunk_overlap: int = 150,
    only_stale: bool = False,
) -> ReindexReport:
    """Re-embed every document (or only the ones search is ignoring).

    One document's failure does not stop the rest: a single unreadable PDF
    should not leave the whole knowledge base un-searchable.
    """
    report = ReindexReport(embedding_space=embedding_provider.embedding_space)
    status = await index_status(db, embedding_provider=embedding_provider)
    wanted = status.stale_documents if only_stale else status.documents

    for row in wanted:
        document = await db.get(KnowledgeDocument, row.document_id)
        if document is None:
            continue
        try:
            report.chunk_count += await reindex_document(
                db,
                document,
                embedding_provider=embedding_provider,
                extractor=extractor,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            report.reindexed.append(document.filename)
        except ReindexError as exc:
            logger.warning("reindex failed document=%s: %s", document.id, exc)
            report.failures[document.filename] = str(exc)
    return report
