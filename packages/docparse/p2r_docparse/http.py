from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from .errors import DocParseError

ALLOWED_HOST_SUFFIXES = ("mineru.net", "openxlab.org.cn", "aliyuncs.com")


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float
    max_body: int | None = None  # per-request response cap; None = transport default


@dataclass
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]


def check_remote_url(url: str) -> None:
    """Reject URLs taken from a remote response unless https on an allowed host."""
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise DocParseError("DOCPARSE_UNSAFE_URL")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise DocParseError("DOCPARSE_UNSAFE_URL") from None
    ok = (
        parts.scheme == "https"
        and parts.username is None and parts.password is None
        and port in (None, 443)
        and any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)
    )
    if not ok:
        raise DocParseError("DOCPARSE_UNSAFE_URL")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib then raises HTTPError carrying the 3xx response


class _NoDefaultContentType(urllib.request.BaseHandler):
    """urllib adds a form-urlencoded Content-Type to any request with a body;
    presigned PUT URLs must be sent without one unless the caller set it."""

    handler_order = 600  # after the HTTP(S) handlers' own request processors

    def _strip(self, req):
        if not any(k.lower() == "content-type" for k in req.headers):
            req.unredirected_hdrs.pop("Content-type", None)
        return req

    http_request = https_request = _strip


_OPENER = urllib.request.build_opener(_NoRedirect, _NoDefaultContentType)


def _read_capped(fp, max_body: int) -> bytes:
    data = fp.read(max_body + 1)
    if len(data) > max_body:
        raise DocParseError("DOCPARSE_RESPONSE_TOO_LARGE")
    return data


def urllib_transport(req: HttpRequest, *, max_body: int = 8 * 1024 * 1024) -> HttpResponse:
    """Single request; redirects are not followed, 4xx/5xx come back as responses.
    Connection-level problems (including malformed responses and URLs) always
    surface as a bare OSError so callers can treat them uniformly."""
    limit = req.max_body if req.max_body is not None else max_body
    r = urllib.request.Request(req.url, data=req.body, headers=req.headers, method=req.method)
    try:
        with _OPENER.open(r, timeout=req.timeout) as resp:
            data = _read_capped(resp, limit)
            declared = resp.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and len(data) < int(declared):
                raise OSError("transport failure")  # truncated body: retryable like any dropped connection
            return HttpResponse(resp.status, data)
    except urllib.error.HTTPError as e:
        try:
            return HttpResponse(e.code, _read_capped(e, limit))
        except (http.client.HTTPException, ValueError):
            raise OSError("transport failure") from None
        finally:
            e.close()
    except (http.client.HTTPException, ValueError):
        raise OSError("transport failure") from None
