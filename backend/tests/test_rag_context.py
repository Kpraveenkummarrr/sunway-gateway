from uuid import uuid4

from app.services.knowledge_search import SearchResult
from app.services.rag_context import build_context


def _make_result(text: str, *, similarity: float = 0.9, page: int | None = 1) -> SearchResult:
    return SearchResult(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_filename="doc.pdf",
        page_number=page,
        chunk_index=0,
        chunk_text=text,
        similarity=similarity,
    )


def test_build_context_returns_none_for_no_results() -> None:
    assert build_context([], max_chars=1000) is None


def test_build_context_includes_document_and_page_metadata() -> None:
    context = build_context([_make_result("Some fact.", page=3)], max_chars=1000)
    assert "doc.pdf" in context
    assert "page 3" in context
    assert "Some fact." in context


def test_build_context_omits_page_when_none() -> None:
    context = build_context([_make_result("Some fact.", page=None)], max_chars=1000)
    assert "[Source: doc.pdf]" in context
    assert "page" not in context


def test_build_context_respects_max_chars() -> None:
    results = [_make_result("X" * 500, page=i) for i in range(5)]
    context = build_context(results, max_chars=600)
    assert len(context) <= 700  # some allowance for source headers/truncation marker


def test_build_context_includes_multiple_chunks_when_under_limit() -> None:
    results = [_make_result("short fact one"), _make_result("short fact two")]
    context = build_context(results, max_chars=1000)
    assert "short fact one" in context
    assert "short fact two" in context
