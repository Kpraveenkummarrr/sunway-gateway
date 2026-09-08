"""Deterministic text chunking for the RAG pipeline.

Packs paragraphs (from the page text) into chunks up to `chunk_size`
characters, splitting only at paragraph/sentence/word boundaries where
possible. A paragraph longer than `chunk_size` on its own is hard-split at
a word boundary as a fallback. Each chunk after the first carries
`chunk_overlap` characters of trailing context from the previous chunk, so
retrieval doesn't lose meaning at a chunk edge.
"""

from dataclasses import dataclass

from app.services.pdf_extraction import ExtractedPage


@dataclass(frozen=True)
class Chunk:
    index: int
    page_number: int | None
    text: str


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _hard_split(paragraph: str, chunk_size: int) -> list[str]:
    """Split an over-long paragraph at word boundaries into pieces no
    larger than chunk_size."""
    words = paragraph.split(" ")
    pieces: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > chunk_size:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def chunk_pages(pages: list[ExtractedPage], chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    """Chunk a document's pages into overlapping text chunks.

    Never returns empty chunks. `chunk_overlap` must be smaller than
    `chunk_size` (enforced here to avoid an infinite/degenerate overlap).
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be >= 0 and less than chunk_size")

    # Flatten to (page_number, paragraph) pairs, preserving page boundaries.
    units: list[tuple[int, str]] = []
    for page in pages:
        for paragraph in _split_paragraphs(page.text):
            if len(paragraph) > chunk_size:
                for piece in _hard_split(paragraph, chunk_size):
                    units.append((page.page_number, piece))
            else:
                units.append((page.page_number, paragraph))

    chunks: list[Chunk] = []
    current_text = ""
    current_page: int | None = None

    def flush() -> None:
        nonlocal current_text, current_page
        stripped = current_text.strip()
        if stripped:
            chunks.append(Chunk(index=len(chunks), page_number=current_page, text=stripped))
        current_text = ""
        current_page = None

    for page_number, paragraph in units:
        if current_page is None:
            current_page = page_number

        candidate = f"{current_text}\n\n{paragraph}".strip() if current_text else paragraph
        if len(candidate) <= chunk_size:
            current_text = candidate
            continue

        # Current chunk is full — flush it, then start the next one with
        # `chunk_overlap` characters of trailing context carried forward.
        overlap_text = current_text[-chunk_overlap:].strip() if chunk_overlap else ""
        flush()
        current_page = page_number
        current_text = f"{overlap_text}\n\n{paragraph}".strip() if overlap_text else paragraph

        # A single paragraph plus overlap could still exceed chunk_size in
        # rare cases (very small chunk_size) — that's acceptable; we never
        # drop content, we just don't guarantee a hard size cap in that edge case.

    flush()
    return chunks
