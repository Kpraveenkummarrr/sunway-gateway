"""pgvector cosine-similarity search over knowledge_chunks."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument


class SearchError(Exception):
    pass


@dataclass(frozen=True)
class SearchResult:
    chunk_id: UUID
    document_id: UUID
    document_filename: str
    page_number: int | None
    chunk_index: int
    chunk_text: str
    similarity: float


async def search_chunks(
    db: AsyncSession,
    *,
    query_embedding: list[float],
    top_k: int,
    similarity_threshold: float | None = None,
) -> list[SearchResult]:
    """Returns the top-K most similar chunks (only from "ready" documents),
    optionally filtered to a minimum cosine similarity."""
    if len(query_embedding) != EMBEDDING_DIM:
        raise SearchError(
            f"Query embedding has {len(query_embedding)} dimensions, expected {EMBEDDING_DIM}"
        )
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    # pgvector's `<=>` is cosine *distance* (0 = identical); similarity = 1 - distance.
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
    similarity = (1 - distance).label("similarity")

    stmt = (
        select(KnowledgeChunk, KnowledgeDocument.filename, similarity)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.status == "ready")
        .order_by(distance)
        .limit(top_k)
    )

    rows = (await db.execute(stmt)).all()

    results = [
        SearchResult(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_filename=filename,
            page_number=chunk.page_number,
            chunk_index=chunk.chunk_index,
            chunk_text=chunk.chunk_text,
            similarity=float(sim),
        )
        for chunk, filename, sim in rows
    ]

    if similarity_threshold is not None:
        results = [r for r in results if r.similarity >= similarity_threshold]

    return results
