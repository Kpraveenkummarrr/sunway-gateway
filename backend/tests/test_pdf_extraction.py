import pytest

from app.services.pdf_extraction import (
    EmptyPDFError,
    PDFExtractionError,
    PyPDFTextExtractor,
    normalize_text,
)
from tests.pdf_fixtures import make_empty_pdf, make_pdf, make_simple_pdf


def test_extracts_text_from_valid_pdf() -> None:
    extractor = PyPDFTextExtractor()
    result = extractor.extract(make_simple_pdf("Hello World"))
    assert result.page_count == 1
    assert "Hello World" in result.pages[0].text


def test_preserves_page_numbers_across_multiple_pages() -> None:
    extractor = PyPDFTextExtractor()
    result = extractor.extract(make_pdf(["First page", "Second page", "Third page"]))
    assert result.page_count == 3
    assert [p.page_number for p in result.pages] == [1, 2, 3]
    assert "First page" in result.pages[0].text
    assert "Second page" in result.pages[1].text
    assert "Third page" in result.pages[2].text


def test_rejects_non_pdf_file() -> None:
    extractor = PyPDFTextExtractor()
    with pytest.raises(PDFExtractionError):
        extractor.extract(b"this is definitely not a pdf")


def test_rejects_empty_bytes() -> None:
    extractor = PyPDFTextExtractor()
    with pytest.raises(PDFExtractionError):
        extractor.extract(b"")


def test_detects_pdf_with_no_extractable_text() -> None:
    extractor = PyPDFTextExtractor()
    with pytest.raises(EmptyPDFError):
        extractor.extract(make_empty_pdf())


def test_normalize_text_collapses_whitespace() -> None:
    assert normalize_text("hello    world\t\tfoo") == "hello world foo"


def test_normalize_text_preserves_paragraph_breaks() -> None:
    raw = "Paragraph one.\n\n\n\n\nParagraph two."
    assert normalize_text(raw) == "Paragraph one.\n\nParagraph two."


def test_normalize_text_strips_line_edges() -> None:
    assert normalize_text("  line one  \n   line two  ") == "line one\nline two"
