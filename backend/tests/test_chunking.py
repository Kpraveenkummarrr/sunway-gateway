import pytest

from app.services.chunking import chunk_pages
from app.services.pdf_extraction import ExtractedPage


def test_short_document_produces_single_chunk() -> None:
    pages = [ExtractedPage(page_number=1, text="A short document.")]
    chunks = chunk_pages(pages, chunk_size=800, chunk_overlap=100)
    assert len(chunks) == 1
    assert chunks[0].text == "A short document."
    assert chunks[0].page_number == 1
    assert chunks[0].index == 0


def test_long_document_produces_multiple_chunks() -> None:
    paragraph = "Sentence number {n} in a long paragraph of test content. "
    long_text = "\n\n".join(paragraph.format(n=i) * 3 for i in range(50))
    pages = [ExtractedPage(page_number=1, text=long_text)]
    chunks = chunk_pages(pages, chunk_size=200, chunk_overlap=40)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.strip() != ""


def test_no_empty_chunks_ever_produced() -> None:
    pages = [ExtractedPage(page_number=1, text="\n\n\n   \n\n")]
    chunks = chunk_pages(pages, chunk_size=100, chunk_overlap=10)
    assert chunks == []


def test_chunk_overlap_carries_trailing_context_forward() -> None:
    p1 = "AAAAAAAAAA BBBBBBBBBB"  # 21 chars
    p2 = "CCCCCCCCCC DDDDDDDDDD"  # 21 chars
    pages = [ExtractedPage(page_number=1, text=f"{p1}\n\n{p2}")]
    chunks = chunk_pages(pages, chunk_size=25, chunk_overlap=10)
    assert len(chunks) == 2
    # The tail of chunk 0 should reappear at the start of chunk 1.
    overlap_text = chunks[0].text[-10:].strip()
    assert overlap_text in chunks[1].text


def test_chunk_metadata_preserves_page_number() -> None:
    pages = [
        ExtractedPage(page_number=1, text="Page one content here."),
        ExtractedPage(page_number=2, text="Page two content here."),
    ]
    chunks = chunk_pages(pages, chunk_size=15, chunk_overlap=2)
    page_numbers = {c.page_number for c in chunks}
    assert page_numbers == {1, 2}


def test_oversized_paragraph_is_hard_split() -> None:
    long_word_paragraph = " ".join(f"word{i}" for i in range(200))
    pages = [ExtractedPage(page_number=1, text=long_word_paragraph)]
    chunks = chunk_pages(pages, chunk_size=50, chunk_overlap=10)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.strip() != ""


def test_chunk_indices_are_sequential() -> None:
    pages = [ExtractedPage(page_number=1, text="para one\n\npara two\n\npara three")]
    chunks = chunk_pages(pages, chunk_size=10, chunk_overlap=2)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_invalid_chunk_size_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_pages([ExtractedPage(page_number=1, text="x")], chunk_size=0, chunk_overlap=0)


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError):
        chunk_pages([ExtractedPage(page_number=1, text="x")], chunk_size=50, chunk_overlap=50)
