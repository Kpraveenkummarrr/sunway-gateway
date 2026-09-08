"""Hand-built minimal PDF generator for tests — avoids adding a PDF-writing
dependency (e.g. reportlab) just to produce test fixtures."""


def _pdf_object(obj_id: int, body: bytes) -> bytes:
    return f"{obj_id} 0 obj\n".encode() + body + b"\nendobj\n"


def make_pdf(page_texts: list[str]) -> bytes:
    """Builds a minimal, valid, multi-page PDF where each page shows one
    line of text (or is blank if the string is empty), with correct xref
    offsets computed programmatically."""
    if not page_texts:
        raise ValueError("page_texts must be non-empty")

    n = len(page_texts)
    # Object numbering: 1=Catalog, 2=Pages, 3=Font,
    # then for each page i (0-indexed): (4+2i)=Page, (5+2i)=Contents
    catalog_id = 1
    pages_id = 2
    font_id = 3
    page_ids = [4 + 2 * i for i in range(n)]
    contents_ids = [5 + 2 * i for i in range(n)]

    objects: dict[int, bytes] = {}

    objects[catalog_id] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i, text in enumerate(page_texts):
        page_id = page_ids[i]
        content_id = contents_ids[i]
        objects[page_id] = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/MediaBox [0 0 300 144] /Contents {content_id} 0 R >>"
        ).encode()

        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream_body = f"BT /F1 14 Tf 10 100 Td ({escaped}) Tj ET".encode() if text else b""
        stream = f"<< /Length {len(stream_body)} >>\nstream\n".encode() + stream_body + b"\nendstream"
        objects[content_id] = stream

    max_id = max(objects) + 1
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for obj_id in range(1, max_id):
        offsets[obj_id] = len(out)
        out += _pdf_object(obj_id, objects[obj_id])

    xref_offset = len(out)
    out += f"xref\n0 {max_id}\n".encode()
    out += b"0000000000 65535 f \n"
    for obj_id in range(1, max_id):
        out += f"{offsets[obj_id]:010d} 00000 n \n".encode()

    out += (
        f"trailer\n<< /Size {max_id} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()

    return bytes(out)


def make_simple_pdf(text: str = "Hello World") -> bytes:
    return make_pdf([text])


def make_empty_pdf() -> bytes:
    """A structurally valid PDF with a single page and no text content."""
    return make_pdf([""])
