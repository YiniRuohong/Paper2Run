"""Article entry (TXT/paste text) and Provider session HTTP integration tests."""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.models import (
    IdempotencyKey, PaperRecord, PaperVersionRecord, ProjectRecord,
    UserRecord, MembershipRecord,
)


TEXT_BODY = "Paper2Run pasted article text.\nSecond line with details.\n"


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'entry.sqlite3'}",
        supervisor=object(),
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


def test_pdf_upload_still_works(seeded):
    app, ids = seeded
    pdf = b"%PDF-1.4\n/Type /Page\n(Real PDF text)\n%%EOF"
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


# ---------------------------------------------------------------- Provider sessions

SAFE_PROVIDER_BODY = {
    "base_url": "https://provider.example/v1",
    "endpoint_alias": "my-provider",
    "model_id": "model-a",
    "display_name": "My Provider",
    "revision": "1",
    "api_key": "sk-live-secret-abcdef123456",
}


def post_provider(client, body, key):
    return client.post(
        "/api/v1/provider-sessions",
        headers={"X-Actor-ID": "owner", "Idempotency-Key": key},
        json=body,
    )


def test_provider_session_returns_only_safe_projection(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        resp = post_provider(client, SAFE_PROVIDER_BODY, "prov-1")
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert "provider_session_id" in data
        assert SAFE_PROVIDER_BODY["api_key"] not in resp.text
        assert "api_key" not in data
        assert data["secret_configured"] is True
        assert data["endpoint_alias"] == "my-provider"
        assert data["model_id"] == "model-a"

        # GET is idempotent-safe read-back of projection only
        readback = client.get(
            f"/api/v1/provider-sessions/{data['provider_session_id']}",
            headers={"X-Actor-ID": "owner"},
        )
        assert readback.status_code == 200
        assert SAFE_PROVIDER_BODY["api_key"] not in readback.text

        # replay same key+body returns same id
        replay = post_provider(client, SAFE_PROVIDER_BODY, "prov-1")
        assert replay.json()["data"]["provider_session_id"] == data["provider_session_id"]

        # same key different body -> conflict
        other = {**SAFE_PROVIDER_BODY, "model_id": "model-b"}
        conflict = post_provider(client, other, "prov-1")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        # missing key
        no_key = client.post("/api/v1/provider-sessions",
                             headers={"X-Actor-ID": "owner"},
                             json=SAFE_PROVIDER_BODY)
        assert no_key.status_code == 422
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad_url", [
    "http://provider.example/v1",
    "https://user:pass@provider.example/v1",
    "https://provider.example/v1?x=1",
    "https://provider.example/v1#frag",
    "ftp://provider.example/v1",
])
def test_provider_session_rejects_unsafe_endpoint(seeded, bad_url):
    app, ids = seeded
    with TestClient(app) as client:
        resp = post_provider(client, {**SAFE_PROVIDER_BODY, "base_url": bad_url}, "prov-bad")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "PROVIDER_ENDPOINT_REJECTED"


def test_provider_session_capabilities_are_passed_through_and_validated(seeded):
    """Regression: capabilities field must reach store.put, not be dropped (F2)."""
    app, ids = seeded
    with TestClient(app) as client:
        # valid capabilities are accepted (and stored on the ProviderConfig)
        ok = post_provider(client, {**SAFE_PROVIDER_BODY,
                                    "capabilities": {"text": "supported",
                                                     "structured_output": "supported"}},
                           "prov-caps-ok")
        assert ok.status_code == 201, ok.text

        # invalid capability value -> validation error, not silently dropped
        bad = post_provider(client, {**SAFE_PROVIDER_BODY,
                                     "capabilities": {"text": "sometimes"}},
                            "prov-caps-bad")
        assert bad.status_code == 422, bad.text
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

        # unknown capability key -> rejected by extra=forbid
        unknown = post_provider(client, {**SAFE_PROVIDER_BODY,
                                         "capabilities": {"text": "supported", "telepathy": "on"}},
                                "prov-caps-unknown")
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "VALIDATION_ERROR"

        # unknown top-level field -> rejected by extra=forbid
        extra = post_provider(client, {**SAFE_PROVIDER_BODY, "surprise": 1}, "prov-extra")
        assert extra.status_code == 422
        assert extra.json()["error"]["code"] == "VALIDATION_ERROR"

    # verify the stored config actually carries the declared capabilities
    store = app.state.runtime["provider_secret_store"]
    summary = store.public_summary(ok.json()["data"]["provider_session_id"])
    config = store.config(summary["provider_session_id"])
    assert config.capabilities.text == "supported"
    assert config.capabilities.structured_output == "supported"


def test_provider_secret_never_persisted_to_database_or_artifacts(seeded, tmp_path):
    app, ids = seeded
    with TestClient(app) as client:
        resp = post_provider(client, SAFE_PROVIDER_BODY, "prov-persist")
        assert resp.status_code == 201
        session_id = resp.json()["data"]["provider_session_id"]

    # scan every table row for the secret
    factory = app.state.runtime["session_factory"]
    from sqlalchemy import inspect as sa_inspect
    with factory() as db:
        engine = db.get_bind()
        for table in sa_inspect(engine).get_table_names():
            rows = db.execute(
                __import__("sqlalchemy").text(f'SELECT * FROM "{table}"')
            ).mappings().all()
            for row in rows:
                blob = json.dumps(dict(row), default=str, ensure_ascii=False)
                assert SAFE_PROVIDER_BODY["api_key"] not in blob, f"leak in table {table}"

    # scan artifact store files
    store_root = tmp_path / "store"
    if store_root.exists():
        for path in store_root.rglob("*"):
            if path.is_file():
                assert SAFE_PROVIDER_BODY["api_key"] not in path.read_bytes()

    # clearing the session removes the runtime secret
    with TestClient(app) as client:
        cleared = client.delete(
            f"/api/v1/provider-sessions/{session_id}",
            headers={"X-Actor-ID": "owner", "Idempotency-Key": "prov-clear"},
        )
        assert cleared.status_code in (200, 204), cleared.text
        after = client.get(
            f"/api/v1/provider-sessions/{session_id}",
            headers={"X-Actor-ID": "owner"},
        )
        assert after.status_code in (404, 410)
        assert SAFE_PROVIDER_BODY["api_key"] not in after.text
