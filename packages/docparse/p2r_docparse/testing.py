"""Stdlib-only PDF fixtures shared by tests and the offline smoke check."""
from __future__ import annotations

import zlib


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(text: str) -> bytes:
    return f"BT /F1 12 Tf 72 720 Td ({_escape(text)}) Tj ET".encode("latin-1", "replace")


def minimal_pdf(pages: list[str], *, object_streams: bool = False) -> bytes:
    """One Helvetica text line per page. With object_streams=True the page tree
    lives in a compressed /ObjStm behind an xref stream (like pdfTeX output),
    so the raw bytes never contain "/Type /Page"."""
    n = len(pages)
    # object numbers: 1 catalog, 2 pages, 3 font, then per page: page, content
    kids = [4 + 2 * i for i in range(n)]
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Count {n} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    streams: dict[int, bytes] = {}
    for i, text in enumerate(pages):
        page, content = 4 + 2 * i, 5 + 2 * i
        bodies[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>").encode()
        streams[content] = _content(text)
    last = 3 + 2 * n

    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def emit(num: int, body: bytes, stream: bytes | None = None, extra: bytes = b"") -> None:
        offsets[num] = len(out)
        if stream is None:
            out.extend(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
        else:
            out.extend(f"{num} 0 obj\n<< {extra.decode()}/Length {len(stream)} >>\nstream\n".encode()
                       + stream + b"\nendstream\nendobj\n")

    if not object_streams:
        for num in sorted(bodies):
            emit(num, bodies[num])
        for num in sorted(streams):
            emit(num, b"", streams[num])
        xref = len(out)
        out.extend(f"xref\n0 {last + 1}\n0000000000 65535 f \n".encode())
        for num in range(1, last + 1):
            out.extend(f"{offsets[num]:010d} 00000 n \n".encode())
        out.extend(f"trailer\n<< /Size {last + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return bytes(out)

    # Compressed object stream holds every non-stream object.
    stream_id, xref_id = last + 1, last + 2
    packed = sorted(bodies)
    header, payload = [], bytearray()
    for num in packed:
        header.append(f"{num} {len(payload)}")
        payload.extend(bodies[num] + b"\n")
    head = (" ".join(header) + "\n").encode()
    objstm = zlib.compress(head + bytes(payload))
    for num in sorted(streams):
        emit(num, b"", streams[num])
    emit(stream_id, b"", objstm,
         extra=f"/Type /ObjStm /N {len(packed)} /First {len(head)} /Filter /FlateDecode ".encode())
    xref_pos = len(out)
    # xref stream rows: type(1) field2(4) field3(2)
    rows = bytearray(b"\x00" + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big"))
    for num in range(1, xref_id):
        if num in bodies:
            rows += b"\x02" + stream_id.to_bytes(4, "big") + packed.index(num).to_bytes(2, "big")
        else:
            rows += b"\x01" + offsets[num].to_bytes(4, "big") + (0).to_bytes(2, "big")
    rows += b"\x01" + xref_pos.to_bytes(4, "big") + (0).to_bytes(2, "big")
    emit(xref_id, b"", zlib.compress(bytes(rows)),
         extra=f"/Type /XRef /Size {xref_id + 1} /W [1 4 2] /Root 1 0 R /Filter /FlateDecode ".encode())
    out.extend(f"startxref\n{xref_pos}\n%%EOF\n".encode())
    return bytes(out)
