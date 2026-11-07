"""Article entry (TXT/paste text, PDF upload, source content) HTTP integration tests."""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_docparse.testing import minimal_pdf
from p2r_persistence.models import (
    PaperRecord, PaperVersionRecord, ProjectRecord,
    UserRecord, MembershipRecord,
)


TEXT_BODY = "Paper2Run pasted article text.\nSecond line with details.\n"


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'entry.sqlite3'}",
        development_auth=True,
        secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "store"),
    )


@pytest.fixture
def seeded(app):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="Entry owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="Entry project", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        ids = {"user": user.id, "project": project.id}
    return app, ids


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def post_text(client, project_id, *, text=TEXT_BODY, key="text-1", consent=True):
    body = {"text": text, "source_processing_consent": consent}
    if not consent:
        body.pop("source_processing_consent")
    return client.post(
        f"/api/v1/projects/{project_id}/papers/text",
        headers=headers(key),
        json=body,
    )


def test_paste_text_creates_paper_version_hash_and_is_idempotent(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        first = post_text(client, ids["project"])
        assert first.status_code == 202, first.text
        data = first.json()["data"]
        assert set(data) == {"paper_id", "operation_id", "status"}
        assert data["status"] == "uploaded"

        paper = client.get(f"/api/v1/papers/{data['paper_id']}",
                           headers={"X-Actor-ID": "owner"}).json()["data"]
        expected = hashlib.sha256(TEXT_BODY.encode()).hexdigest()
        assert paper["sha256"] == expected
        with app.state.runtime["session_factory"]() as db:
            version = db.scalar(select(PaperVersionRecord).where(PaperVersionRecord.paper_id == data["paper_id"]))
            assert paper["paper_version_id"] == version.id
        listed = client.get(f"/api/v1/projects/{ids['project']}/papers", headers={"X-Actor-ID": "owner"}).json()["data"]
        assert listed[0]["paper_version_id"] == paper["paper_version_id"]

        # content round-trip is byte-exact and text/plain
        content = client.get(f"/api/v1/papers/{data['paper_id']}/content",
                             headers={"X-Actor-ID": "owner"})
        assert content.status_code == 200
        assert content.content == TEXT_BODY.encode()
        assert content.headers["content-type"].startswith("text/plain")

        # replay with same key + body returns the same paper
        replay = post_text(client, ids["project"], key="text-1")
        assert replay.json()["data"] == data

        # different body with same key is a conflict
        conflict = post_text(client, ids["project"], text=TEXT_BODY + "x", key="text-1")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        with app.state.runtime["session_factory"]() as db:
            assert db.scalar(select(func.count()).select_from(PaperRecord)) == 1
            version = db.scalar(select(PaperVersionRecord))
            assert version.sha256 == expected
            assert version.bytes == len(TEXT_BODY.encode())
            assert version.media_type == "text/plain"
            assert version.version == 1
            # no provider secret column exists on papers at all
            columns = {c["name"] for c in __import__("sqlalchemy").inspect(
                db.get_bind()).get_columns("papers")}
            assert not any("secret" in c or "api_key" in c for c in columns)


def test_text_requires_idempotency_key_and_consent(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        no_key = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers={"X-Actor-ID": "owner"},
            json={"text": TEXT_BODY, "source_processing_consent": True},
        )
        assert no_key.status_code == 422
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"

        no_consent = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers=headers("no-consent"),
            json={"text": TEXT_BODY},
        )
        assert no_consent.status_code == 422
        assert no_consent.json()["error"]["code"] == "SOURCE_PROCESSING_CONSENT_REQUIRED"


def test_text_plain_body_requires_explicit_consent_header(seeded):
    """Regression: raw text/plain body must NOT implicitly grant consent (F1)."""
    app, ids = seeded
    with TestClient(app) as client:
        # raw text/plain with no consent header -> rejected
        no_consent = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers={**headers("plain-no-consent"),
                     "Content-Type": "text/plain; charset=utf-8"},
            content=TEXT_BODY.encode(),
        )
        assert no_consent.status_code == 422, no_consent.text
        assert no_consent.json()["error"]["code"] == "SOURCE_PROCESSING_CONSENT_REQUIRED"

        # raw text/plain with an explicit consent header -> accepted
        with_consent = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers={**headers("plain-consent"),
                     "Content-Type": "text/plain; charset=utf-8",
                     "X-Source-Processing-Consent": "true"},
            content=TEXT_BODY.encode(),
        )
        assert with_consent.status_code == 202, with_consent.text

        # wrong consent value is still rejected
        bad_consent = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers={**headers("plain-bad-consent"),
                     "Content-Type": "text/plain; charset=utf-8",
                     "X-Source-Processing-Consent": "yes"},
            content=TEXT_BODY.encode(),
        )
        assert bad_consent.status_code == 422
        assert bad_consent.json()["error"]["code"] == "SOURCE_PROCESSING_CONSENT_REQUIRED"


@pytest.mark.parametrize("body,code", [
    ({"text": ""}, "VALIDATION_ERROR"),
    ({"text": "   \n  "}, "VALIDATION_ERROR"),
    ({"text": "x" * (2 * 1024 * 1024 + 1)}, "TEXT_TOO_LARGE"),
])
def test_text_validation_rejects_empty_oversize(seeded, body, code):
    app, ids = seeded
    with TestClient(app) as client:
        body = {**body, "source_processing_consent": True}
        resp = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers=headers("bad-" + code),
            json=body,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == code


def test_invalid_utf8_text_rejected(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        # multipart path with binary body that is not valid UTF-8
        resp = client.post(
            f"/api/v1/projects/{ids['project']}/papers/text",
            headers={**headers("bad-utf8"), "Content-Type": "text/plain; charset=utf-8",
                     "X-Source-Processing-Consent": "true"},
            content=b"\xff\xfe not utf8 \x80\x81",
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "TEXT_INVALID_UTF8"


def test_paper_json_reports_source_kind_and_media_type(seeded):
    """The frontend decides whether parsing applies from these fields, not from text status."""
    app, ids = seeded
    with TestClient(app) as client:
        text_id = post_text(client, ids["project"], key="kind-text").json()["data"]["paper_id"]
        pdf = client.post(
            f"/api/v1/projects/{ids['project']}/papers",
            headers=headers("kind-pdf"),
            files={"file": ("paper.pdf", minimal_pdf(["Kind"]), "application/pdf")},
            data={"source_processing_consent": "true"},
        )
        pdf_id = pdf.json()["data"]["paper_id"]
        text = client.get(f"/api/v1/papers/{text_id}", headers={"X-Actor-ID": "owner"}).json()["data"]
        assert (text["source_kind"], text["media_type"]) == ("text", "text/plain")
        paper = client.get(f"/api/v1/papers/{pdf_id}", headers={"X-Actor-ID": "owner"}).json()["data"]
        assert (paper["source_kind"], paper["media_type"]) == ("pdf", "application/pdf")
        listed = client.get(f"/api/v1/projects/{ids['project']}/papers", headers={"X-Actor-ID": "owner"}).json()["data"]
        assert {row["paper_id"]: row["source_kind"] for row in listed} == {text_id: "text", pdf_id: "pdf"}


def test_pdf_upload_still_works(seeded):
    app, ids = seeded
    pdf = minimal_pdf(["Real PDF text"]) + b"\r\n\r\n"
    with TestClient(app) as client:
        result = client.post(
            f"/api/v1/projects/{ids['project']}/papers",
            headers=headers("pdf"),
            files={"file": ("paper.pdf", pdf, "application/pdf")},
            data={"source_processing_consent": "true"},
        )
        assert result.status_code == 202, result.text
        paper_id = result.json()["data"]["paper_id"]
        content = client.get(f"/api/v1/papers/{paper_id}/content",
                             headers={"X-Actor-ID": "owner"})
        assert content.content == pdf
        assert content.headers["content-type"] == "application/pdf"
