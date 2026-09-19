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

from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.knowledge import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from app.services.lexical_retrieval import (
    ChunkRecord,
    LexicalIndex,
    content_terms,
    load_synonym_groups,
    names_a_topic,
    topic_words,
)

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


# A caller's follow-up ("iska ilaj kya hai?") names no topic at all, so on its
# own it retrieves nothing. A question with this many meaningful words or fewer
# (function words like क्या/है/के do not count) that does not name the subject
# itself is treated as a follow-up. The caller's own words are always kept.
FOLLOW_UP_MAX_CONTENT_TERMS = 2


def build_retrieval_query(user_text: str, previous_user_texts: list[str] | None = None) -> str:
    """The text used for embedding and lexical retrieval for this turn.

    Long or topic-naming utterances are used as they are. A short follow-up is
    prefixed with the most recent earlier caller utterance so that pronouns
    ("iska", "uska") still resolve to a searchable topic.
    """
    text = (user_text or "").strip()
    if not text:
        return text
    if names_a_topic(text) or len(content_terms(text)) > FOLLOW_UP_MAX_CONTENT_TERMS:
        return text
    previous_texts = [p.strip() for p in (previous_user_texts or []) if p.strip() and p.strip() != text]
    # Carry the SUBJECT the caller last named, not their whole earlier question:
    # the earlier question's own angle ("लक्षण") would drag the answer to the
    # wrong passage when they now ask about "बचाव". This holds across
    # acknowledgements ("जी हाँ", "ठीक है") in between.
    for previous in reversed(previous_texts):
        subject = topic_words(previous)
        if subject:
            return f"{' '.join(dict.fromkeys(subject))} {text}"
    # No known subject: fall back to the last question with content of its own.
    for previous in reversed(previous_texts):
        if len(content_terms(previous)) > FOLLOW_UP_MAX_CONTENT_TERMS:
            return f"{previous} {text}"
    for previous in reversed(previous_texts):
        return f"{previous} {text}"
    return text


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


# One index per process and embedding space, rebuilt only when the matching
# corpus changes.  The row IDs are part of the signature: counts/timestamps can
# collide when fast tests (or an operator) replace one document with another.
_INDEX_CACHE: dict[tuple[str, str], tuple[tuple, LexicalIndex]] = {}


async def _lexical_index(
    db: AsyncSession, synonyms_path: str | None, embedding_space: str | None
) -> LexicalIndex:
    filters = [KnowledgeDocument.status == "ready"]
    if embedding_space is not None:
        filters.append(KnowledgeDocument.metadata_json["embedding_space"].astext == embedding_space)
    rows = (
        await db.execute(
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.document_id,
                KnowledgeDocument.filename,
                KnowledgeChunk.page_number,
                KnowledgeChunk.chunk_index,
                KnowledgeChunk.chunk_text,
                KnowledgeChunk.updated_at,
                KnowledgeDocument.updated_at,
            )
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(*filters)
            .order_by(KnowledgeDocument.created_at, KnowledgeChunk.chunk_index)
        )
    ).all()
    synonyms_stamp = None
    if synonyms_path:
        try:
            synonyms_stamp = Path(synonyms_path).stat().st_mtime_ns
        except OSError:
            pass
    signature = (synonyms_stamp, tuple((row[0], row[6], row[7]) for row in rows))
    key = (synonyms_path or "", embedding_space or "")
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    index = LexicalIndex(
        [ChunkRecord(*row[:6]) for row in rows], synonym_groups=load_synonym_groups(synonyms_path)
    )
    _INDEX_CACHE[key] = (signature, index)
    logger.info("Lexical index built: %d chunks", len(rows))
    return index


async def _lexical_hits(db, query_text, *, top_k, min_coverage, synonyms_path, embedding_space):
    index = await _lexical_index(db, synonyms_path, embedding_space)
    return index.search(query_text, top_k=top_k, min_coverage=min_coverage)


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
    use_lexical_index: bool = True,
    lexical_min_coverage: float = 0.34,
    synonyms_path: str | None = None,
) -> list[SearchResult]:
    """Return top-K hybrid-ranked chunks from ready documents.

    Two channels are merged. The vector channel (below) needs a meaningful
    embedding space. The lexical channel (app.services.lexical_retrieval)
    searches every ready chunk in the current embedding space, so a weak or
    placeholder vector cannot make the knowledge base look empty without
    admitting stale or unknown-provenance documents.

    With an explicit query space, unknown and incompatible provenance is
    excluded until re-indexed. Equal dimensions do not make models compatible.
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
        # Unknown provenance cannot safely participate in semantic ranking.
        stored_space = KnowledgeDocument.metadata_json["embedding_space"].astext
        filters.append(stored_space == embedding_space)

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

    if use_lexical_index and (query_text or "").strip():
        lexical_started = time.monotonic()
        by_chunk = {result.chunk_id: position for position, (_, result) in enumerate(ranked)}
        for hit in await _lexical_hits(db, query_text or "", top_k=top_k, min_coverage=lexical_min_coverage,
                                        synonyms_path=synonyms_path, embedding_space=embedding_space):
            # In vector-similarity units: a chunk covering the whole question ~1.0.
            score = 0.35 + 0.65 * hit.coverage
            position = by_chunk.get(hit.record.chunk_id)
            if position is not None:
                # Both channels found it: the stronger score, plus a little for agreeing.
                ranked[position] = (max(ranked[position][0], score) + 0.05, ranked[position][1])
                continue
            record = hit.record
            ranked.append((score, SearchResult(
                chunk_id=record.chunk_id, document_id=record.document_id, document_filename=record.filename,
                page_number=record.page_number, chunk_index=record.chunk_index, chunk_text=record.text,
                similarity=score,
            )))
        if timings is not None:
            timings["lexical"] = int((time.monotonic() - lexical_started) * 1000)

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
