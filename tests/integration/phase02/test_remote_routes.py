"""Tablet-remote routes: thin proxy to the p2r-tunnel control API."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore

TOKEN = "t" * 64


class FakeControl:
    """Records requests and answers like the Rust control API."""

    def __init__(self):
        self.calls: list[tuple[str, str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                outer.calls.append((self.command, self.path, self.headers.get("authorization")))
                if self.headers.get("authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"error": "unauthorized"})
                if (self.command, self.path) == ("GET", "/status"):
                    return self._reply(200, {"desktop_id": "d" * 64, "relay": "https://relay/", "devices": [
                        {"id": "a" * 64, "name": "Tab", "paired_at": 1, "online": True, "path": "relay", "last_seen": 2}]})
                if (self.command, self.path) == ("POST", "/pairing"):
                    return self._reply(200, {"code": "123-45678", "expires_in_ms": 300000})
                if (self.command, self.path) == ("GET", "/pairing"):
                    return self._reply(200, {"state": "waiting", "code": "123-45678", "expires_in_ms": 299000})
                if self.command == "DELETE" and self.path == "/devices/" + "a" * 64:
                    return self._reply(204)
                return self._reply(404, {"error": "not found"})

            do_GET = do_POST = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def control(tmp_path, monkeypatch):
    fake = FakeControl()
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"port": fake.port, "token": TOKEN, "pid": 1}))
    monkeypatch.setenv("P2R_REMOTE_CONTROL_FILE", str(path))
    yield fake
    fake.close()


@pytest.fixture
def client(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'r.sqlite3'}", supervisor=object(), secure_cookies=False,
                     artifact_store=LocalArtifactStore(tmp_path / "store"))
    return TestClient(app)


REMOTE = {"X-P2R-Remote": "1"}


def test_status_when_sidecar_absent(client, tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_REMOTE_CONTROL_FILE", str(tmp_path / "missing.json"))
    r = client.get("/api/v1/remote/status")
    assert r.status_code == 200
    assert r.json()["data"] == {"available": False, "remote": False}


def test_status_proxies_and_marks_remote(client, control):
    local = client.get("/api/v1/remote/status").json()["data"]
    assert local["available"] is True and local["remote"] is False
    assert local["devices"][0]["name"] == "Tab"
    assert client.get("/api/v1/remote/status", headers=REMOTE).json()["data"]["remote"] is True
    assert all(auth == f"Bearer {TOKEN}" for _, _, auth in control.calls)


def test_remote_header_blocks_writes(client, control):
    for method, path in [("POST", "/api/v1/remote/pairing"), ("DELETE", "/api/v1/remote/devices/" + "a" * 64)]:
        r = client.request(method, path, headers=REMOTE)
        assert r.status_code == 403, (method, r.text)
        assert r.json()["error"]["code"] == "REMOTE_FORBIDDEN"
    assert control.calls == []
    r = client.post("/api/v1/remote/pairing")
    assert r.status_code == 200 and r.json()["data"]["code"] == "123-45678"
    assert client.delete("/api/v1/remote/devices/" + "a" * 64).status_code == 204
    assert [(m, p) for m, p, _ in control.calls] == [("POST", "/pairing"), ("DELETE", "/devices/" + "a" * 64)]


def test_cross_site_writes_rejected(client, control):
    r = client.post("/api/v1/remote/pairing", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert control.calls == []


def test_remote_can_read_pairing_state(client, control):
    r = client.get("/api/v1/remote/pairing", headers=REMOTE)
    assert r.status_code == 200 and r.json()["data"]["state"] == "waiting"


def test_pairing_unavailable_returns_503(client, tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_REMOTE_CONTROL_FILE", str(tmp_path / "missing.json"))
    r = client.post("/api/v1/remote/pairing")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "REMOTE_UNAVAILABLE"


def test_stale_control_file_reads_as_unavailable(client, tmp_path, monkeypatch):
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"port": 1, "token": TOKEN, "pid": 1}))
    monkeypatch.setenv("P2R_REMOTE_CONTROL_FILE", str(path))
    assert client.get("/api/v1/remote/status").json()["data"]["available"] is False
    assert client.post("/api/v1/remote/pairing").status_code == 503
