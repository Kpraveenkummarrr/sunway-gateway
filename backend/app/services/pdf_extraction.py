"""PDF text extraction.

Kept modular (an ABC + one implementation) so a different extraction
backend — or an OCR fallback for scanned/image-only PDFs — can be added
later without touching the ingestion pipeline that calls this.
"""

import io
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


class PDFExtractionError(Exception):
    """The input is not a readable PDF at all (bad magic bytes, corrupt)."""


class EmptyPDFError(Exception):
    """The PDF parsed fine but no extractable text was found.

    Most commonly a scanned/image-only PDF with no text layer. OCR support
    is a future phase — not implemented here to keep this phase's
    dependencies light.
    """


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int  # 1-indexed
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    pages: list[ExtractedPage]

    @property
    def page_count(self) -> int:
        return len(self.pages)


_WHITESPACE_RE = re.compile(r"[ \t\x0b\x0c\r]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Collapse runs of horizontal whitespace and excessive blank lines
    without destroying paragraph structure (single/double newlines kept)."""
    text = text.replace("\x00", "")  # PostgreSQL text cannot store NUL.
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


class PDFTextExtractor(ABC):
    @abstractmethod
    def extract(self, file_bytes: bytes) -> ExtractedDocument:
        """Raises PDFExtractionError if the file isn't a valid PDF, or
        EmptyPDFError if it is but contains no extractable text."""


class PyPDFTextExtractor(PDFTextExtractor):
    """Extracts embedded text via pypdf. Does not OCR — image-only pages
    yield empty text for that page, same as any other page pypdf can't
    read text from."""

    def extract(self, file_bytes: bytes) -> ExtractedDocument:
        if not file_bytes.lstrip(b"\x00").startswith(b"%PDF-"):
            raise PDFExtractionError("File does not start with a %PDF- header")

        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            reader = PdfReader(io.BytesIO(file_bytes))
        except ImportError as exc:  # pragma: no cover - dependency always installed
            raise PDFExtractionError(f"pypdf is not available: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - pypdf raises various error types
            raise PDFExtractionError(f"Could not parse PDF: {exc}") from exc

        pages: list[ExtractedPage] = []
        try:
            for index, page in enumerate(reader.pages, start=1):
                raw_text = page.extract_text() or ""
                pages.append(ExtractedPage(page_number=index, text=normalize_text(raw_text)))
        except PdfReadError as exc:
            raise PDFExtractionError(f"Could not read PDF pages: {exc}") from exc

        if not pages:
            raise PDFExtractionError("PDF has no pages")

        if not any(page.text for page in pages):
            raise EmptyPDFError(
                "No extractable text found in any page — this may be a "
                "scanned/image-only PDF, which requires OCR (not implemented "
                "in this phase) to ingest."
            )

        return ExtractedDocument(pages=pages)
