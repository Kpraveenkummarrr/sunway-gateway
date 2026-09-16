"""Hybrid pgvector + lexical search over knowledge_chunks.

Vector similarity is the primary ranking signal. A small lexical candidate
pass makes exact domain terms and ASR spelling variants recoverable when a
cross-lingual embedding is imperfect, while the embedding-space check keeps
known-incompatible document vectors out of a query.
"""

import re
import time
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument

logger = get_logger(__name__)

_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "about", "can", "do", "does", "for", "give", "how", "is", "ke", "kya",
    "me", "the", "tell", "what", "with", "you", "hai", "hain", "batao", "mujhe", "kya", "mein", "par",
}

# These are retrieval aliases only. They do not contain answers and are kept
# intentionally small so the knowledge source remains the source of truth.
_LUMPY_ALIASES = (
    "lumpy skin disease",
    "lumpy disease",
    "lumpy skin",
    "lampi skin disease",
    "lampy skin disease",
    "\u0932\u092e\u094d\u092a\u0940 \u0938\u094d\u0915\u093f\u0928 \u0921\u093f\u091c\u0940\u091c",
    "\u0932\u092e\u094d\u092a\u0940 \u0924\u094d\u0935\u091a\u093e \u0930\u094b\u0917",
)
_LUMPY_MARKERS = ("lumpy", "lampi", "lampy", "\u0932\u092e\u094d\u092a\u0940")
_TOKEN_RE = re.compile(r"\S+")


def normalize_search_text(text: str) -> str:
    """Normalize case, Unicode compatibility forms and punctuation while
    preserving Hindi letters/marks and word boundaries."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if char.isspace() or category[0] in ("L", "M", "N"):
            chars.append(char)
        else:
            chars.append(" ")
    return " ".join("".join(chars).split())


def lexical_terms(query: str) -> list[str]:
    """Return meaningful lexical terms plus safe domain aliases."""
    normalized = normalize_search_text(query)
    tokens = [token for token in _TOKEN_RE.findall(normalized) if token not in _SEARCH_STOPWORDS and len(token) > 1]
    terms: list[str] = []

    def add(term: str) -> None:
        term = normalize_search_text(term)
        if term and term not in terms:
            terms.append(term)

    for token in tokens:
        add(token)

    if any(marker in normalized for marker in _LUMPY_MARKERS):
        for alias in _LUMPY_ALIASES:
            add(alias)
            for token in normalize_search_text(alias).split():
                if len(token) > 1:
                    add(token)
    return terms


def lexical_match_score(text: str, terms: list[str]) -> float:
    """Return a bounded lexical overlap score for ranking candidates."""
    normalized = normalize_search_text(text)
    if not normalized or not terms:
        return 0.0
    if any(" " in term and term in normalized for term in terms):
        return 1.0
    source_tokens = set(normalized.split())
    token_terms = {term for term in terms if " " not in term}
    if not token_terms:
        return 0.0
    return sum(term in source_tokens for term in token_terms) / len(token_terms)


def has_strong_lexical_match(text: str, terms: list[str]) -> bool:
    normalized = normalize_search_text(text)
    return any(" " in term and term in normalized for term in terms)


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
    query_text: str | None = None,
    top_k: int,
    similarity_threshold: float | None = None,
    embedding_space: str | None = None,
    timings: dict[str, int] | None = None,
) -> list[SearchResult]:
    """Return top-K hybrid-ranked chunks from ready documents.

    Documents created before embedding-space metadata existed are retained
    for backwards compatibility. Documents with an explicit different space
    are excluded, preventing known model mixing until they are re-indexed.
    """
    if len(query_embedding) != EMBEDDING_DIM:
        raise SearchError(
            f"Query embedding has {len(query_embedding)} dimensions, expected {EMBEDDING_DIM}"
        )
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    if embedding_space is not None and not embedding_space.strip():
        raise SearchError("embedding_space must not be blank")

    normalization_started = time.monotonic()
    terms = lexical_terms(query_text or "")
    if timings is not None:
        timings["query_normalization"] = int((time.monotonic() - normalization_started) * 1000)
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
    similarity = (1 - distance).label("similarity")

    filters = [
        KnowledgeDocument.status == "ready",
        KnowledgeChunk.embedding.is_not(None),
    ]
    if embedding_space is not None:
        # Old documents without metadata remain searchable; a document that
        # explicitly names another model/dimension is not mixed into this
        # query and must be re-indexed with the current provider.
        stored_space = KnowledgeDocument.metadata_json["embedding_space"].astext
        filters.append(or_(stored_space.is_(None), stored_space == embedding_space))

    def _base_stmt():
        return (
            select(KnowledgeChunk, KnowledgeDocument.filename, similarity)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(*filters)
        )

    # The vector shortlist stays bounded. For small business knowledge bases,
    # the additional lexical pass is also bounded but wide enough to recover
    # an exact domain-term hit that a weak cross-lingual vector misses.
    retrieval_started = time.monotonic()
    vector_rows = (
        await db.execute(_base_stmt().order_by(distance).limit(max(top_k * 4, top_k)))
    ).all()

    candidates: dict[UUID, tuple[KnowledgeChunk, str, float]] = {}
    for chunk, filename, sim in vector_rows:
        if sim is not None:
            candidates[chunk.id] = (chunk, filename, float(sim))

    if terms:
        lexical_filters = [func.lower(KnowledgeChunk.chunk_text).contains(term) for term in terms]
        lexical_rows = (
            await db.execute(
                _base_stmt()
                .where(or_(*lexical_filters))
                .order_by(distance)
                .limit(max(50, top_k * 10))
            )
        ).all()
        for chunk, filename, sim in lexical_rows:
            if sim is not None:
                candidates[chunk.id] = (chunk, filename, float(sim))
    if timings is not None:
        timings["rag"] = int((time.monotonic() - retrieval_started) * 1000)
        timings["retrieval"] = timings["rag"]

    ranked: list[tuple[float, SearchResult]] = []
    for chunk, filename, sim in candidates.values():
        lexical_score = lexical_match_score(chunk.chunk_text, terms)
        strong_lexical = has_strong_lexical_match(chunk.chunk_text, terms)
        if similarity_threshold is not None and sim < similarity_threshold and not strong_lexical:
            continue
        result = SearchResult(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_filename=filename,
            page_number=chunk.page_number,
            chunk_index=chunk.chunk_index,
            chunk_text=chunk.chunk_text,
            similarity=sim,
        )
        # Lexical evidence is a tie-breaker/boost, never a replacement for
        # the vector score exposed to callers.
        ranked.append((sim + (0.15 * lexical_score), result))

    ranked.sort(key=lambda item: (-item[0], -item[1].similarity, item[1].chunk_index))
    results = [result for _, result in ranked[:top_k]]

    logger.info(
        "RAG retrieval candidates=%d lexical_terms=%d returned=%d embedding_space=%s",
        len(candidates),
        len(terms),
        len(results),
        embedding_space or "legacy-or-unspecified",
    )
    for result in results:
        logger.info(
            "RAG result document=%s chunk=%s page=%s similarity=%.4f",
            result.document_id,
            result.chunk_id,
            result.page_number if result.page_number is not None else "-",
            result.similarity,
        )

    return results
