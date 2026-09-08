"""Builds a bounded, LLM-ready context string from RAG search results."""

from app.services.knowledge_search import SearchResult


def build_context(results: list[SearchResult], *, max_chars: int) -> str | None:
    """Formats retrieved chunks (with document/page metadata) into a single
    context string, cut off at max_chars. Returns None if there are no
    results — callers must treat that as "no supporting knowledge found",
    not as an empty-but-valid context.

    Deliberately does not send whole documents to the LLM — only the
    already-ranked top-K chunks, further capped by size here.
    """
    if not results:
        return None

    parts: list[str] = []
    used = 0
    for result in results:
        page_info = f", page {result.page_number}" if result.page_number is not None else ""
        entry = f"[Source: {result.document_filename}{page_info}]\n{result.chunk_text}"

        if used + len(entry) > max_chars:
            remaining = max_chars - used
            if remaining > 50:  # only include a truncated fragment if it's still meaningful
                parts.append(entry[:remaining].rstrip() + "...")
            break

        parts.append(entry)
        used += len(entry)

    return "\n\n".join(parts) if parts else None
