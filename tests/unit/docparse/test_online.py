import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from p2r_docparse.errors import DocParseError
from p2r_docparse.http import HttpRequest, HttpResponse, check_remote_url, urllib_transport
from p2r_docparse.online import OnlineAgentClient, parse_online
from p2r_docparse.testing import minimal_pdf

FILE_URL = "https://mineru.oss-cn-shanghai.aliyuncs.com/upload/abc"
MD_URL = "https://cdn-mineru.openxlab.org.cn/result/abc.md"
SUBMIT = "https://mineru.net/api/v1/agent/parse/file"


def _json(obj, status=200):
    return HttpResponse(status, json.dumps(obj).encode())


class Fake:
    """Scripted Transport: records every request, answers by URL."""

    def __init__(self, *, submits=None, states=("pending", "done"), markdown="# Title",
                 file_url=FILE_URL, md_url=MD_URL, fail_chunk_names=()):
        self.requests: list[HttpRequest] = []
        self.submits = list(submits or [])
        self.states = list(states)
        self.markdown = markdown
        self.file_url = file_url
        self.md_url = md_url
        self.fail_names = set(fail_chunk_names)
        self.failing_tasks: set[str] = set()
        self.n_tasks = 0
        self.poll_log: list[str] = []

    def __call__(self, req: HttpRequest) -> HttpResponse:
        self.requests.append(req)
        if req.url == SUBMIT:
            if self.submits:
                return self.submits.pop(0)
            name = json.loads(req.body)["file_name"]
            self.n_tasks += 1
            task = f"t{self.n_tasks}"
            if name in self.fail_names:
                self.failing_tasks.add(task)
            return _json({"code": 0, "msg": "ok", "data": {"task_id": task, "file_url": self.file_url}})
        if req.method == "PUT":
            return HttpResponse(200, b"")
        if req.url.startswith("https://mineru.net/api/v1/agent/parse/"):
            task = req.url.rsplit("/", 1)[1]
            if task in self.failing_tasks:
                return _json({"code": 0, "data": {"state": "failed", "err_code": -60001, "err_msg": "SECRET-RAW"}})
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            data = {"state": state}
            if state == "done":
                data["markdown_url"] = self.md_url
            return _json({"code": 0, "data": data})
        if req.url == self.md_url:
            return HttpResponse(200, self.markdown.encode("utf-8"))
        raise AssertionError(f"unexpected request {req.method} {req.url}")


def client(fake, **kw):
    sleeps = []
    t = [0.0]
    kw.setdefault("sleep", lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
    kw.setdefault("clock", lambda: t[0])
    c = OnlineAgentClient(fake, **kw)
    c.sleeps = sleeps
    return c


def test_happy_path_single_chunk():
    fake = Fake()
    md = client(fake).parse_chunk(b"%PDF-bytes", "paper-p1-1.pdf")
    assert md == "# Title"
    kinds = [(r.method, r.url) for r in fake.requests]
    assert kinds == [
        ("POST", SUBMIT), ("PUT", FILE_URL),
        ("GET", "https://mineru.net/api/v1/agent/parse/t1"),
        ("GET", "https://mineru.net/api/v1/agent/parse/t1"),
        ("GET", MD_URL),
    ]
    submit, put = fake.requests[0], fake.requests[1]
    assert json.loads(submit.body) == {"file_name": "paper-p1-1.pdf", "enable_table": True,
                                       "enable_formula": True, "is_ocr": False}
    assert submit.headers["Content-Type"] == "application/json"
    assert put.body == b"%PDF-bytes"
    for r in fake.requests:
        assert "Authorization" not in r.headers
        assert r.headers["User-Agent"] == "paper2run/0.1"
    assert "Content-Type" not in put.headers


def test_429_backoff_then_success():
    fake = Fake(submits=[HttpResponse(429, b""), HttpResponse(429, b"")])
    c = client(fake)
    assert c.parse_chunk(b"x", "paper-p1-1.pdf") == "# Title"
    assert c.sleeps[:2] == [1, 2]


def test_rate_limit_exhausted_maps_code():
    fake = Fake(submits=[HttpResponse(429, b"")] * 10)
    with pytest.raises(DocParseError) as e:
        client(fake, max_retries=2).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_RATE_LIMITED"


def test_business_error_code_maps():
    fake = Fake(submits=[_json({"code": -30003, "msg": "SECRET-RAW"})])
    with pytest.raises(DocParseError) as e:
        client(fake).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_REMOTE_FAILED"
    assert "SECRET-RAW" not in str(e.value)
    assert len(fake.requests) == 1


def test_failed_state_maps():
    fake = Fake(fail_chunk_names={"f.pdf"})
    with pytest.raises(DocParseError) as e:
        client(fake).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_REMOTE_FAILED"
    assert "SECRET-RAW" not in str(e.value)


@pytest.mark.parametrize("bad", [
    "http://mineru.oss-cn-shanghai.aliyuncs.com/x",
    "https://evil.example/x",
    "https://mineru.net.evil.com/x",
])
def test_unsafe_urls_rejected(bad):
    fake = Fake(file_url=bad)
    with pytest.raises(DocParseError) as e:
        client(fake).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_UNSAFE_URL"
    assert all(r.method != "PUT" for r in fake.requests)


def test_unsafe_markdown_url_rejected():
    fake = Fake(md_url="https://evil.example/r.md")
    with pytest.raises(DocParseError) as e:
        client(fake).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_UNSAFE_URL"


def test_check_remote_url_accepts_allowed_hosts():
    for u in ("https://mineru.net/a", "https://cdn.openxlab.org.cn/a", "https://x.y.aliyuncs.com/a"):
        check_remote_url(u)
    for u in ("https://mineru.net:8443/a", "https://user@evil.com/a", "ftp://mineru.net/a", "https://notmineru.net/a"):
        with pytest.raises(DocParseError):
            check_remote_url(u)


def test_chunk_timeout():
    fake = Fake(states=("running",))
    with pytest.raises(DocParseError) as e:
        client(fake, chunk_timeout=30.0, poll_interval=10.0).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_TIMEOUT"


def test_cancel_between_polls():
    fake = Fake(states=("running",))
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(DocParseError) as e:
        client(fake).parse_chunk(b"x", "f.pdf", cancelled=cancelled)
    assert e.value.code == "DOCPARSE_CANCELLED"


def test_parse_online_splits_and_merges():
    fake = Fake(states=("done",))
    progress = []
    res = parse_online(minimal_pdf([f"p{i}" for i in range(21)]), client=client(fake),
                       on_progress=lambda d, t: progress.append((d, t)))
    submits = [json.loads(r.body)["file_name"] for r in fake.requests if r.url == SUBMIT]
    assert submits == ["paper-p1-20.pdf", "paper-p21-21.pdf"]
    assert "<!-- p2r:pages 1-20 -->" in res.markdown
    assert "<!-- p2r:page 21 -->" in res.markdown
    assert (res.page_count, res.pages_parsed) == (21, 21)
    assert (res.backend, res.backend_version) == ("online_agent", "agent-v1")
    assert res.warnings == []
    assert res.chunks == [{"first_page": 1, "last_page": 20, "status": "done"},
                          {"first_page": 21, "last_page": 21, "status": "done"}]
    assert progress == [(1, 2), (2, 2)]


def test_partial_failure_and_retry_reuses():
    pdf = minimal_pdf([f"p{i}" for i in range(21)])
    fake = Fake(states=("done",), fail_chunk_names={"paper-p21-21.pdf"})
    res = parse_online(pdf, client=client(fake))
    assert res.warnings == ["第 21 页解析失败，未包含"]
    assert res.pages_parsed == 20
    assert res.chunks[1] == {"first_page": 21, "last_page": 21, "status": "missing"}
    assert "<!-- p2r:pages 21-21 missing -->" in res.markdown

    fake2 = Fake(states=("done",))
    res2 = parse_online(pdf, client=client(fake2), previous_markdown=res.markdown)
    assert sum(1 for r in fake2.requests if r.url == SUBMIT) == 1
    assert res2.warnings == [] and res2.pages_parsed == 21


def test_all_chunks_failed_raises():
    fake = Fake(fail_chunk_names={"paper-p1-1.pdf"})
    with pytest.raises(DocParseError) as e:
        parse_online(minimal_pdf(["a"]), client=client(fake))
    assert e.value.code == "DOCPARSE_REMOTE_FAILED"


def test_cancelled_between_chunks():
    fake = Fake(states=("done",))
    flag = {"stop": False}

    def on_progress(done, total):
        flag["stop"] = True

    with pytest.raises(DocParseError) as e:
        parse_online(minimal_pdf([f"p{i}" for i in range(21)]), client=client(fake),
                     on_progress=on_progress, cancelled=lambda: flag["stop"])
    assert e.value.code == "DOCPARSE_CANCELLED"
    assert sum(1 for r in fake.requests if r.url == SUBMIT) == 1


def test_connection_error_retries():
    attempts = []

    def boom(req):
        attempts.append(req)
        raise OSError("down")

    with pytest.raises(DocParseError) as e:
        client(boom, max_retries=3).parse_chunk(b"x", "f.pdf")
    assert e.value.code == "DOCPARSE_REMOTE_FAILED"
    assert len(attempts) == 4


def test_connection_error_in_one_chunk_is_a_missing_chunk():
    ok = Fake(states=("done",))

    def flaky(req):
        if req.url == SUBMIT and b"paper-p21-21" in req.body:
            raise OSError("down")
        return ok(req)

    res = parse_online(minimal_pdf([f"p{i}" for i in range(21)]), client=client(flaky, max_retries=1))
    assert res.pages_parsed == 20
    assert res.warnings == ["第 21 页解析失败，未包含"]


# --- urllib_transport against a loopback server ---

class _H(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _do(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        _H.seen.append((self.command, self.path, dict(self.headers), body))
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
        elif self.path == "/err":
            self.send_response(503)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"no")
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"x" * 100)
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    do_GET = do_PUT = do_POST = _do


@pytest.fixture
def server():
    _H.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_transport_put_has_no_content_type_and_json_keeps_it(server):
    r = urllib_transport(HttpRequest("PUT", server + "/up", {"User-Agent": "paper2run/0.1"}, b"abc", 5.0))
    assert (r.status, r.body) == (200, b"ok")
    _, _, headers, body = _H.seen[-1]
    assert body == b"abc"
    assert not any(k.lower() == "content-type" for k in headers)
    urllib_transport(HttpRequest("POST", server + "/j", {"Content-Type": "application/json"}, b"{}", 5.0))
    assert _H.seen[-1][2]["Content-Type"] == "application/json"


def test_transport_does_not_follow_redirects_and_returns_errors(server):
    r = urllib_transport(HttpRequest("GET", server + "/redir", {}, None, 5.0))
    assert r.status == 302
    assert len(_H.seen) == 1
    r = urllib_transport(HttpRequest("GET", server + "/err", {}, None, 5.0))
    assert (r.status, r.body) == (503, b"no")


def test_transport_body_cap(server):
    with pytest.raises(DocParseError) as e:
        urllib_transport(HttpRequest("GET", server + "/big", {}, None, 5.0), max_body=50)
    assert e.value.code == "DOCPARSE_RESPONSE_TOO_LARGE"
    with pytest.raises(DocParseError):
        urllib_transport(HttpRequest("GET", server + "/big", {}, None, 5.0, max_body=10))
