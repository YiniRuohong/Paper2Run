"""Docparse HTTP surface: page counting at upload, mode/install routes, parse + text."""
from __future__ import annotations

import io
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_docparse.install import OfflineInstaller
from p2r_docparse.testing import minimal_pdf
from p2r_orchestration.docparse_service import DocParseService
from p2r_persistence.models import (
    Job, MembershipRecord, PaperRecord, PaperTextRecord, ProjectRecord, UserRecord,
)

OLD_PAGE_RE = re.compile(rb"/Type\s*/Page(?:\s|/|>)")


def _service(tmp_path, platform="darwin-x64"):
    root = tmp_path / "docparse"
    return DocParseService(root, installer=OfflineInstaller(root, platform=lambda: platform))


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'dp.sqlite3'}", supervisor=object(), secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "store"), development_auth=True,
        docparse_service=_service(tmp_path),
    )


@pytest.fixture
def ids(app):
    with app.state.runtime["session_factory"].begin() as db:
        user = UserRecord(id="owner", display_name="Owner", can_create_project=True)
        db.add(user); db.flush()
        project = ProjectRecord(name="P", created_by=user.id)
        db.add(project); db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        return {"project": project.id}


def _upload(client, project, pdf, key="up-1", name="p.pdf"):
    boundary = "XBOUNDARYX"
    body = io.BytesIO()
    body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="source_processing_consent"\r\n\r\ntrue\r\n'.encode())
    body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
               f'Content-Type: application/pdf\r\n\r\n'.encode())
    body.write(pdf); body.write(f"\r\n--{boundary}--\r\n".encode())
    return client.post(f"/api/v1/projects/{project}/papers", content=body.getvalue(),
                       headers={"X-Actor-ID": "owner", "Idempotency-Key": key,
                                "Content-Type": f"multipart/form-data; boundary={boundary}"})


H = {"X-Actor-ID": "owner"}


def _jobs(app):
    with app.state.runtime["session_factory"]() as db:
        return db.scalars(select(Job).where(Job.kind == "paper_parse")).all()


def test_compressed_pdf_upload_counts_pages(app, ids):
    pdf = minimal_pdf(["a", "b", "c"], object_streams=True)
    assert OLD_PAGE_RE.findall(pdf) == []
    with TestClient(app) as client:
        resp = _upload(client, ids["project"], pdf)
        assert resp.status_code == 202, resp.text
        paper_id = resp.json()["data"]["paper_id"]
        got = client.get(f"/api/v1/papers/{paper_id}", headers=H).json()["data"]
    assert got["page_count"] == 3


def test_invalid_pdf_upload(app, ids):
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    writer.encrypt("secret")
    buf = io.BytesIO(); writer.write(buf)
    with TestClient(app) as client:
        resp = _upload(client, ids["project"], buf.getvalue())
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PDF_INVALID"
        resp = _upload(client, ids["project"], b"%PDF-1.4\ngarbage", key="up-2")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PDF_INVALID"


def test_upload_autoenqueues_when_mode_set(app, ids):
    with TestClient(app) as client:
        r = client.put("/api/v1/docparse/mode", json={"mode": "online"}, headers=H)
        assert r.status_code == 200 and r.json()["data"]["mode"] == "online"
        resp = _upload(client, ids["project"], minimal_pdf(["x"]))
        assert resp.status_code == 202
        assert resp.json()["data"]["status"] == "parsing"
        paper_id = resp.json()["data"]["paper_id"]
    with app.state.runtime["session_factory"]() as db:
        assert db.get(PaperRecord, paper_id).status == "parsing"
    jobs = _jobs(app)
    assert len(jobs) == 1
    assert jobs[0].payload_ref == f"{paper_id}:online"
    assert jobs[0].idempotency_key == f"paper_parse:{paper_id}:upload"


def test_upload_waits_when_mode_unset(app, ids):
    with TestClient(app) as client:
        resp = _upload(client, ids["project"], minimal_pdf(["x"]))
        assert resp.json()["data"]["status"] == "uploaded"
        paper_id = resp.json()["data"]["paper_id"]
        assert _jobs(app) == []
        r = client.post(f"/api/v1/papers/{paper_id}/parse", headers={**H, "Idempotency-Key": "k1"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "DOCPARSE_MODE_UNSET"
        client.put("/api/v1/docparse/mode", json={"mode": "offline"}, headers=H)
        r = client.post(f"/api/v1/papers/{paper_id}/parse", headers=H)
        assert r.status_code == 422
        r = client.post(f"/api/v1/papers/{paper_id}/parse", headers={**H, "Idempotency-Key": "k1"})
        assert r.status_code == 202, r.text
        data = r.json()["data"]
        assert data["status"] == "parsing" and data["paper_id"] == paper_id and data["job_id"]
    assert _jobs(app)[0].payload_ref == f"{paper_id}:offline"


def test_parse_rejects_non_pdf(app, ids):
    with TestClient(app) as client:
        client.put("/api/v1/docparse/mode", json={"mode": "online"}, headers=H)
        r = client.post(f"/api/v1/projects/{ids['project']}/papers/text", content=b"hello world",
                        headers={**H, "Idempotency-Key": "t1", "Content-Type": "text/plain",
                                 "X-Source-Processing-Consent": "true"})
        assert r.status_code == 202, r.text
        paper_id = r.json()["data"]["paper_id"]
        r = client.post(f"/api/v1/papers/{paper_id}/parse", headers={**H, "Idempotency-Key": "k"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "PAPER_NOT_PDF"
    assert _jobs(app) == []


def test_status_and_mode_roundtrip(app):
    with TestClient(app) as client:
        s = client.get("/api/v1/docparse/status", headers=H).json()["data"]
        assert s["mode"] is None
        assert s["offline"]["state"] == "unsupported"
        assert isinstance(s["platform"], str)
        r = client.put("/api/v1/docparse/mode", json={"mode": "offline"}, headers=H)
        assert r.json()["data"]["mode"] == "offline"
        assert client.get("/api/v1/docparse/status", headers=H).json()["data"]["mode"] == "offline"
        bad = client.put("/api/v1/docparse/mode", json={"mode": "cloud"}, headers=H)
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_install_unsupported_platform_409(app):
    with TestClient(app) as client:
        r = client.post("/api/v1/docparse/offline/install", headers=H)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "DOCPARSE_OFFLINE_UNSUPPORTED"
        assert client.post("/api/v1/docparse/offline/cancel", headers=H).status_code == 200
        assert client.delete("/api/v1/docparse/offline", headers=H).status_code == 200


def test_text_endpoint_states(app, ids):
    store = app.state.runtime["artifact_store"]
    with TestClient(app) as client:
        resp = _upload(client, ids["project"], minimal_pdf(["x"]))
        paper_id = resp.json()["data"]["paper_id"]
        url = f"/api/v1/papers/{paper_id}/text"
        d = client.get(url, headers=H).json()["data"]
        assert d["status"] == "none" and d["preview"] is None and d["error_code"] is None

        with app.state.runtime["session_factory"].begin() as db:
            paper = db.get(PaperRecord, paper_id)
            rec = PaperTextRecord(paper_id=paper_id, source_sha256=paper.sha256, page_count=1, status="parsing")
            db.add(rec); db.flush(); rec_id = rec.id
        d = client.get(url, headers=H).json()["data"]
        assert d["status"] == "parsing" and d["preview"] is None

        markdown = "# Title\n" + "word " * 2000
        addr = store.put_bytes(markdown.encode())
        with app.state.runtime["session_factory"].begin() as db:
            rec = db.get(PaperTextRecord, rec_id)
            rec.status, rec.text_sha256, rec.backend = "ready", addr.sha256, "online"
            rec.pages_parsed, rec.chunk_map_json, rec.warnings_json = 1, [{"status": "done"}], ["w"]
        d = client.get(url, headers=H).json()["data"]
        assert d["status"] == "ready" and d["backend"] == "online"
        assert d["preview"] == markdown[:4000] and len(d["preview"]) == 4000
        assert d["page_count"] == 1 and d["pages_parsed"] == 1
        assert d["chunks"] == [{"status": "done"}] and d["warnings"] == ["w"]

        with app.state.runtime["session_factory"].begin() as db:
            rec = db.get(PaperTextRecord, rec_id)
            rec.status, rec.error_json = "failed", {"code": "DOCPARSE_REMOTE_FAILED"}
        d = client.get(url, headers=H).json()["data"]
        assert d["status"] == "failed" and d["error_code"] == "DOCPARSE_REMOTE_FAILED" and d["preview"] is None


def test_text_ignores_records_for_other_content(app, ids):
    with TestClient(app) as client:
        paper_id = _upload(client, ids["project"], minimal_pdf(["x"])).json()["data"]["paper_id"]
        with app.state.runtime["session_factory"].begin() as db:
            db.add(PaperTextRecord(paper_id=paper_id, source_sha256="f" * 64, page_count=1, status="parsing"))
        assert client.get(f"/api/v1/papers/{paper_id}/text", headers=H).json()["data"]["status"] == "none"


def test_docparse_requires_session_without_dev_auth(tmp_path):
    prod = create_app(db_url=f"sqlite:///{tmp_path / 'prod.sqlite3'}", secure_cookies=False,
                      allowed_origin="http://127.0.0.1:8000", artifact_store=LocalArtifactStore(tmp_path / "s"),
                      docparse_service=_service(tmp_path))
    with TestClient(prod) as client:
        assert client.get("/api/v1/docparse/status").status_code == 401
        assert client.put("/api/v1/docparse/mode", json={"mode": "online"}).status_code == 401


def _finish_parse(app, paper_id, outcome):
    with app.state.runtime["session_factory"].begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == "paper_parse")):
            job.status = "succeeded" if outcome == "ready" else "failed"
        for rec in db.scalars(select(PaperTextRecord).where(PaperTextRecord.paper_id == paper_id)):
            rec.status = outcome
        db.get(PaperRecord, paper_id).status = outcome


def _start_parse(client, ids, key):
    paper_id = _upload(client, ids["project"], minimal_pdf(["x"]), key="up").json()["data"]["paper_id"]
    return paper_id


def test_parse_replay_same_key_returns_same_response(app, ids):
    with TestClient(app) as client:
        client.put("/api/v1/docparse/mode", json={"mode": "online"}, headers=H)
        paper_id = _start_parse(client, ids, "up")
        _finish_parse(app, paper_id, "ready")
        url = f"/api/v1/papers/{paper_id}/parse"
        first = client.post(url, headers={**H, "Idempotency-Key": "k1"})
        assert first.status_code == 202
        # first call after the upload job finished started a new job; finish it too
        _finish_parse(app, paper_id, "ready")
        replay = client.post(url, headers={**H, "Idempotency-Key": "k1"})
        assert replay.status_code == 202
        assert replay.json()["data"] == first.json()["data"]
        diff = client.post(url, content=b'{"x":1}', headers={**H, "Idempotency-Key": "k1"})
        assert diff.status_code == 409 and diff.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with app.state.runtime["session_factory"]() as db:
        assert db.get(PaperRecord, paper_id).status == "ready"
        texts = db.scalars(select(PaperTextRecord).where(PaperTextRecord.paper_id == paper_id)).all()
        assert [t.status for t in texts] == ["ready", "ready"]
    assert len(_jobs(app)) == 2


def test_parse_new_key_after_failure_starts_new_parse(app, ids):
    with TestClient(app) as client:
        client.put("/api/v1/docparse/mode", json={"mode": "online"}, headers=H)
        paper_id = _start_parse(client, ids, "up")
        _finish_parse(app, paper_id, "failed")
        url = f"/api/v1/papers/{paper_id}/parse"
        r = client.post(url, headers={**H, "Idempotency-Key": "retry"})
        assert r.status_code == 202
        job_id = r.json()["data"]["job_id"]
    jobs = _jobs(app)
    assert len(jobs) == 2 and job_id in {j.id for j in jobs}
    assert next(j for j in jobs if j.id == job_id).status == "queued"
    with app.state.runtime["session_factory"]() as db:
        assert db.get(PaperRecord, paper_id).status == "parsing"
        assert sorted(t.status for t in db.scalars(select(PaperTextRecord).where(PaperTextRecord.paper_id == paper_id))) == ["failed", "parsing"]
