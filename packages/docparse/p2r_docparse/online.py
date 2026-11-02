from __future__ import annotations

import http.client
import json
import time
from typing import Callable

from .errors import DocParseError
from .http import HttpRequest, HttpResponse, Transport, check_remote_url, urllib_transport
from .markers import ChunkOutcome, ParseResult, merge_outcomes, missing_warnings, split_merged
from .pdf import plan_chunks

_JSON_CAP = 1024 * 1024
_MD_CAP = 8 * 1024 * 1024
_UA = "paper2run/0.1"


def _never() -> bool:
    return False


class OnlineAgentClient:
    def __init__(self, transport: Transport = urllib_transport, *, base_url: str = "https://mineru.net",
                 poll_interval: float = 2.5, chunk_timeout: float = 300.0, max_retries: int = 5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._transport = transport
        self._base = base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._chunk_timeout = chunk_timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock

    def _send(self, method: str, url: str, *, body: bytes | None = None,
              content_type: str | None = None, max_body: int = _JSON_CAP,
              timeout: float = 60.0) -> HttpResponse:
        """One logical request with retry on 429 / 5xx / connection errors."""
        headers = {"User-Agent": _UA}
        if content_type:
            headers["Content-Type"] = content_type
        last = "DOCPARSE_REMOTE_FAILED"
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._sleep(min(2 ** (attempt - 1), 30))
            try:
                resp = self._transport(HttpRequest(method, url, dict(headers), body, timeout, max_body))
            except (OSError, http.client.HTTPException, ValueError):
                last = "DOCPARSE_REMOTE_FAILED"
                continue
            if resp.status == 429:
                last = "DOCPARSE_RATE_LIMITED"
                continue
            if resp.status >= 500:
                last = "DOCPARSE_REMOTE_FAILED"
                continue
            if not 200 <= resp.status < 300:
                raise DocParseError("DOCPARSE_REMOTE_FAILED")
            return resp
        raise DocParseError(last)

    def _api(self, method: str, url: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        resp = self._send(method, url, body=body, content_type="application/json" if body else None)
        try:
            doc = json.loads(resp.body)
            data = doc["data"]
            if doc.get("code") != 0 or not isinstance(data, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise DocParseError("DOCPARSE_REMOTE_FAILED") from None
        return data

    def parse_chunk(self, data: bytes, file_name: str, *,
                    cancelled: Callable[[], bool] = _never) -> str:
        deadline = self._clock() + self._chunk_timeout
        sub = self._api("POST", f"{self._base}/api/v1/agent/parse/file", {
            "file_name": file_name, "enable_table": True, "enable_formula": True, "is_ocr": False})
        task_id, file_url = sub.get("task_id"), sub.get("file_url")
        if not isinstance(task_id, str) or not isinstance(file_url, str) or not task_id:
            raise DocParseError("DOCPARSE_REMOTE_FAILED")
        check_remote_url(file_url)
        self._send("PUT", file_url, body=data, timeout=120.0)

        while True:
            if cancelled():
                raise DocParseError("DOCPARSE_CANCELLED")
            st = self._api("GET", f"{self._base}/api/v1/agent/parse/{task_id}")
            state = st.get("state")
            if state == "done":
                md_url = st.get("markdown_url")
                if not isinstance(md_url, str):
                    raise DocParseError("DOCPARSE_REMOTE_FAILED")
                check_remote_url(md_url)
                resp = self._send("GET", md_url, max_body=_MD_CAP, timeout=120.0)
                try:
                    text = resp.body.decode("utf-8")
                except UnicodeDecodeError:
                    raise DocParseError("DOCPARSE_REMOTE_FAILED") from None
                if not text.strip():
                    raise DocParseError("DOCPARSE_REMOTE_FAILED")  # an empty result is not a parsed page
                return text
            if state == "failed":
                raise DocParseError("DOCPARSE_REMOTE_FAILED")
            if self._clock() + self._poll_interval > deadline:
                raise DocParseError("DOCPARSE_TIMEOUT")
            self._sleep(self._poll_interval)


def parse_online(pdf: bytes, *, client: OnlineAgentClient, previous_markdown: str | None = None,
                 on_progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] = _never) -> ParseResult:
    chunks = plan_chunks(pdf)
    reusable = {}
    if previous_markdown:
        reusable = {(o.first_page, o.last_page): o.markdown
                    for o in split_merged(previous_markdown) if o.markdown is not None and o.markdown.strip()}
    outcomes: list[ChunkOutcome] = []
    last_code = "DOCPARSE_REMOTE_FAILED"
    for i, ch in enumerate(chunks, 1):
        if cancelled():
            raise DocParseError("DOCPARSE_CANCELLED")
        md = reusable.get((ch.first_page, ch.last_page))
        if md is None:
            try:
                md = client.parse_chunk(ch.data, f"paper-p{ch.first_page}-{ch.last_page}.pdf",
                                        cancelled=cancelled)
            except DocParseError as e:
                if e.code == "DOCPARSE_CANCELLED":
                    raise
                last_code = e.code
                md = None
        outcomes.append(ChunkOutcome(ch.first_page, ch.last_page, md))
        if on_progress:
            on_progress(i, len(chunks))
    if all(o.markdown is None for o in outcomes):
        raise DocParseError(last_code)
    return ParseResult(
        markdown=merge_outcomes(outcomes),
        backend="online_agent",
        backend_version="agent-v1",
        page_count=chunks[-1].last_page,
        pages_parsed=sum(o.last_page - o.first_page + 1 for o in outcomes if o.markdown is not None),
        chunks=[{"first_page": o.first_page, "last_page": o.last_page,
                 "status": "done" if o.markdown is not None else "missing"} for o in outcomes],
        warnings=missing_warnings(outcomes),
    )
