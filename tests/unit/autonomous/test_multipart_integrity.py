"""Uploaded source bytes must survive multipart framing unchanged."""
import httpx
import pytest

from p2r_api.routes.papers import _parse_multipart


@pytest.mark.parametrize("payload", [
    b"%PDF-1.7\ncontent\n",
    b"%PDF-1.7\r\ncontent\r\n\r\n",
    b"%PDF-1.7\x00\xff\r",
    b"\r\ntext surrounded by newlines\r\n",
])
def test_multipart_preserves_all_source_bytes(payload):
    request = httpx.Request("POST", "http://localhost/upload",
        data={"source_processing_consent": "true"},
        files={"file": ("paper.pdf", payload, "application/pdf")})
    fields = _parse_multipart(request.headers["content-type"], request.read())
    assert fields["file"] == ("paper.pdf", payload)
    assert fields["source_processing_consent"] == (None, b"true")


def test_boundary_like_bytes_inside_pdf_are_not_delimiters():
    payload = b"%PDF-1.7\nstream--fixed-boundary\x00\r\n--fixed-boundary-suffix\r\nendstream\n"
    body = (b'--fixed-boundary\r\nContent-Disposition: form-data; name="file"; filename="paper.pdf"\r\n'
            b'Content-Type: application/pdf\r\n\r\n' + payload + b'\r\n--fixed-boundary--\r\n')
    fields = _parse_multipart('multipart/form-data; boundary="fixed-boundary"', body)
    assert fields["file"] == ("paper.pdf", payload)
