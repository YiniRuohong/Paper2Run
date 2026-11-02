from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter

from .errors import DocParseError


@dataclass(frozen=True)
class Chunk:
    first_page: int  # 1-based, inclusive
    last_page: int
    data: bytes


def _open(pdf: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocParseError("DOCPARSE_PDF_INVALID")
        if len(reader.pages) == 0:
            raise DocParseError("DOCPARSE_PDF_INVALID")
        return reader
    except DocParseError:
        raise
    except Exception:
        raise DocParseError("DOCPARSE_PDF_INVALID") from None


def count_pages(pdf: bytes) -> int:
    return len(_open(pdf).pages)


def _write(reader: PdfReader, first: int, last: int) -> bytes:
    try:
        writer = PdfWriter()
        for i in range(first - 1, last):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        raise DocParseError("DOCPARSE_PDF_INVALID") from None


def _fit(reader: PdfReader, first: int, last: int, max_bytes: int) -> list[Chunk]:
    data = _write(reader, first, last)
    if len(data) <= max_bytes:
        return [Chunk(first, last, data)]
    if first == last:
        raise DocParseError("DOCPARSE_PAGE_TOO_LARGE")
    mid = (first + last) // 2
    return _fit(reader, first, mid, max_bytes) + _fit(reader, mid + 1, last, max_bytes)


def plan_chunks(pdf: bytes, *, max_pages: int = 20, max_bytes: int = 9_500_000) -> list[Chunk]:
    reader = _open(pdf)
    total = len(reader.pages)
    chunks: list[Chunk] = []
    for first in range(1, total + 1, max_pages):
        chunks.extend(_fit(reader, first, min(first + max_pages - 1, total), max_bytes))
    return chunks
