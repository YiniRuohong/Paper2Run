import io

import pytest
from pypdf import PdfReader, PdfWriter

from p2r_docparse.errors import ERROR_CODES, DocParseError
from p2r_docparse.pdf import count_pages, plan_chunks
from p2r_docparse.testing import minimal_pdf


def test_count_pages():
    assert count_pages(minimal_pdf(["a", "b", "c"])) == 3


def test_invalid_pdf():
    with pytest.raises(DocParseError) as e:
        count_pages(b"not a pdf")
    assert e.value.code == "DOCPARSE_PDF_INVALID"


def test_encrypted_pdf_is_invalid():
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(minimal_pdf(["a"]))))
    writer.encrypt("pw")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(DocParseError) as e:
        count_pages(buf.getvalue())
    assert e.value.code == "DOCPARSE_PDF_INVALID"


def test_unknown_error_code_rejected():
    assert "DOCPARSE_PDF_INVALID" in ERROR_CODES
    with pytest.raises(ValueError):
        DocParseError("NOPE")


def test_exactly_20_pages_one_chunk():
    chunks = plan_chunks(minimal_pdf([str(i) for i in range(20)]))
    assert [(c.first_page, c.last_page) for c in chunks] == [(1, 20)]


def test_21_pages_two_chunks():
    chunks = plan_chunks(minimal_pdf([str(i) for i in range(21)]))
    assert [(c.first_page, c.last_page) for c in chunks] == [(1, 20), (21, 21)]


def test_oversize_chunk_halves():
    one = len(plan_chunks(minimal_pdf(["0"]))[0].data)
    max_bytes = int(one * 2.5)
    chunks = plan_chunks(minimal_pdf([str(i) for i in range(10)]), max_bytes=max_bytes)
    assert len(chunks) > 1
    assert all(len(c.data) <= max_bytes for c in chunks)
    assert chunks[0].first_page == 1 and chunks[-1].last_page == 10
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.first_page == prev.last_page + 1


def test_single_page_too_large():
    with pytest.raises(DocParseError) as e:
        plan_chunks(minimal_pdf(["x"]), max_bytes=10)
    assert e.value.code == "DOCPARSE_PAGE_TOO_LARGE"


def test_chunk_is_valid_pdf():
    for c in plan_chunks(minimal_pdf([str(i) for i in range(25)])):
        assert count_pages(c.data) == c.last_page - c.first_page + 1


def test_object_stream_pdf():
    data = minimal_pdf(["a", "b", "c"], object_streams=True)
    assert b"/Type /Page" not in data and b"/Type/Page" not in data
    assert count_pages(data) == 3


def _encrypted(pages: int, algorithm: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(minimal_pdf([str(i) for i in range(pages)]))))
    writer.encrypt(user_password="", owner_password="owner-pw", algorithm=algorithm)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _check_opens(data: bytes) -> None:
    assert count_pages(data) == 21
    chunks = plan_chunks(data)
    assert [(c.first_page, c.last_page) for c in chunks] == [(1, 20), (21, 21)]
    for c in chunks:
        assert count_pages(c.data) == c.last_page - c.first_page + 1
        assert not PdfReader(io.BytesIO(c.data)).is_encrypted


def test_empty_password_aes128_pdf_opens():
    _check_opens(_encrypted(21, "AES-128"))


def test_empty_password_aes256_pdf_opens():
    _check_opens(_encrypted(21, "AES-256"))
