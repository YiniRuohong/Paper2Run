"""Provider extraction of a PDF paper reads its parsed Markdown, not raw PDF bytes.

The PDF path takes the latest ready PaperTextRecord for the paper's current
content, sends its Markdown (page markers included) to the provider, and records
parse provenance (source_text_sha256, parse_backend) plus parse warnings in the
persisted payload. Provenance (source_refs[*].pdf_sha256) stays the PDF digest.
Text papers and the deterministic no-provider PDF path are unchanged.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_docparse.testing import minimal_pdf
from p2r_orchestration.extraction_service import ExtractionService
from p2r_orchestration.providers import CompletionRequest, NormalizedEvent
from p2r_persistence.models import (
    ExtractionRecord, IdempotencyKey, MembershipRecord, ProjectRecord, UserRecord,
)
from tests.integration.phase02.paper_text_seed import seed_paper_text

PDF = minimal_pdf(["Parsed text source"]).rstrip(b"\r\n")  # multipart parser strips trailing CRLF
PDF_SHA = hashlib.sha256(PDF).hexdigest()
MARKDOWN = "<!-- p2r:pages 1-20 -->\n\n# Parsed paper\n\nWe classify images with a CNN baseline.\n"
TEXT_BODY = "Plain text article about a logistic-regression baseline."
PAGE_SENTENCE = (
    "Lines like <!-- p2r:page 7 --> or <!-- p2r:pages 21-40 --> mark the PDF page(s) "
    "of the text that follows; set source_ref.page to the first page of the marked span."
)
PARSE_WARNING = "第 21–40 页解析失败，未包含"


def evidence(value, status="supported", refs=("ref-1",)):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def document(paper_id, paper_sha, *, warnings=()):
    return {
        "schema_version": "article-extraction.v1",
        "title": evidence("Parsed paper"),
        "task": evidence("classification"),
        "methods": [evidence("CNN baseline", "inferred")],
        "formulas": [], "datasets": [], "candidate_experiments": [],
        "warnings": list(warnings), "limitations": [],
        "source_refs": [{
            "id": "ref-1", "paper_id": paper_id, "quote": "classify images",
            "page": 1, "pdf_sha256": paper_sha,
        }],
    }


class CapturingProvider:
    """Provider protocol fake: records each CompletionRequest, answers with `body`."""

    def __init__(self):
        self.body: dict | None = None
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest):
        self.requests.append(request)
        yield NormalizedEvent(type="completed", call_id=request.call_id, text=json.dumps(self.body))


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'parsed.sqlite3'}",
        supervisor=object(),
        secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "store"),
        development_auth=True,
    )


@pytest.fixture
def env(app):
    with app.state.runtime["session_factory"].begin() as db:
        user = UserRecord(id="owner", display_name="Owner", can_create_project=True)
        db.add(user); db.flush()
        project = ProjectRecord(name="Parsed project", created_by=user.id)
        db.add(project); db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        project_id = project.id
    provider = CapturingProvider()
    app.state.runtime["extraction_service"] = ExtractionService(provider)
    session = app.state.runtime["provider_secret_store"].put(
        base_url="https://provider.example/v1", endpoint_alias="parsed-endpoint",
        model_id="model-a", api_key="sk-parsed-secret-123456",
    )
    return {"project": project_id, "provider": provider, "session": session.id}


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def upload_pdf(client, project_id, key="pdf-up"):
    result = client.post(
        f"/api/v1/projects/{project_id}/papers", headers=headers(key),
        files={"file": ("paper.pdf", PDF, "application/pdf")},
        data={"source_processing_consent": "true"},
    )
    assert result.status_code == 202, result.text
    return result.json()["data"]["paper_id"]


def extract(client, paper_id, session_id, key):
    return client.post(
        f"/api/v1/papers/{paper_id}/extractions", headers=headers(key),
        json={"reason": "provider extraction", "provider_session_id": session_id},
    )


def stored_payload(client, paper_id):
    fetched = client.get(f"/api/v1/papers/{paper_id}/extraction", headers={"X-Actor-ID": "owner"})
    assert fetched.status_code == 200, fetched.text
    return fetched.json()["data"]


def test_pdf_without_text_409(app, env):
    with TestClient(app) as client:
        paper_id = upload_pdf(client, env["project"])
        # Neither in-progress, failed, nor ready-for-other-content records count.
        seed_paper_text(app, paper_id, "", status="parsing")
        seed_paper_text(app, paper_id, "", status="failed")
        seed_paper_text(app, paper_id, MARKDOWN, source_sha256="f" * 64)
        result = extract(client, paper_id, env["session"], "ext-notready")
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "PAPER_TEXT_NOT_READY"
        assert env["provider"].requests == []
        with app.state.runtime["session_factory"]() as db:
            assert db.query(ExtractionRecord).filter_by(paper_id=paper_id).count() == 0


def test_text_not_ready_leaves_no_idempotency_claim(app, env):
    with TestClient(app) as client:
        paper_id = upload_pdf(client, env["project"])
        first = extract(client, paper_id, env["session"], "ext-retry")
        assert first.status_code == 409, first.text
        assert first.json()["error"]["code"] == "PAPER_TEXT_NOT_READY"
        with app.state.runtime["session_factory"]() as db:
            assert db.query(IdempotencyKey).filter_by(key="ext-retry").count() == 0

        # Parsing finishes; the very same key now succeeds (no stuck claim).
        seed_paper_text(app, paper_id, MARKDOWN)
        env["provider"].body = document(paper_id, PDF_SHA)
        retry = extract(client, paper_id, env["session"], "ext-retry")
        assert retry.status_code == 202, retry.text
        assert retry.json()["data"]["extraction_id"]
        assert len(env["provider"].requests) == 1


def test_pdf_with_text_sends_markdown(app, env):
    with TestClient(app) as client:
        paper_id = upload_pdf(client, env["project"])
        text_sha = seed_paper_text(app, paper_id, MARKDOWN, backend="online_agent")
        env["provider"].body = document(paper_id, PDF_SHA)
        result = extract(client, paper_id, env["session"], "ext-md")
        assert result.status_code == 202, result.text

        request = env["provider"].requests[0]
        system, user = (m.content for m in request.messages)
        assert system.endswith(PAGE_SENTENCE)
        assert "<!-- p2r:pages 1-20 -->" in user
        assert "%PDF" not in user
        assert user.endswith(MARKDOWN)
        assert f"pdf_sha256: {PDF_SHA}" in user

        payload = stored_payload(client, paper_id)
        assert payload["provider"]["source_text_sha256"] == text_sha
        assert payload["provider"]["parse_backend"] == "online_agent"
        assert payload["source_refs"] and all(ref["pdf_sha256"] == PDF_SHA for ref in payload["source_refs"])
        assert payload["provider"]["content_sha256"] == hashlib.sha256(MARKDOWN.encode()).hexdigest()


def test_latest_ready_text_is_used(app, env):
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        paper_id = upload_pdf(client, env["project"])
        seed_paper_text(app, paper_id, "<!-- p2r:page 1 -->\n\nolder text\n",
                        backend="offline_mineru", created_at=now - timedelta(minutes=2))
        newest_ready = seed_paper_text(app, paper_id, MARKDOWN, backend="offline_mineru",
                                       created_at=now - timedelta(minutes=1))
        # A newer re-parse still running does not hide the ready text.
        seed_paper_text(app, paper_id, "", status="parsing", created_at=now)
        env["provider"].body = document(paper_id, PDF_SHA)
        assert extract(client, paper_id, env["session"], "ext-latest").status_code == 202

        user = env["provider"].requests[0].messages[1].content
        assert user.endswith(MARKDOWN) and "older text" not in user
        payload = stored_payload(client, paper_id)
        assert payload["provider"]["source_text_sha256"] == newest_ready
        assert payload["provider"]["parse_backend"] == "offline_mineru"


def test_parse_warnings_propagate(app, env):
    with TestClient(app) as client:
        paper_id = upload_pdf(client, env["project"])
        seed_paper_text(app, paper_id, MARKDOWN, warnings=[PARSE_WARNING, "公式识别降级"])
        # The model already repeated one parse warning: it must appear once.
        env["provider"].body = document(paper_id, PDF_SHA, warnings=["model warning", PARSE_WARNING])
        assert extract(client, paper_id, env["session"], "ext-warn").status_code == 202

        payload = stored_payload(client, paper_id)
        assert payload["warnings"] == ["model warning", PARSE_WARNING, "公式识别降级"]


def test_text_paper_path_unchanged(app, env):
    with TestClient(app) as client:
        created = client.post(
            f"/api/v1/projects/{env['project']}/papers/text", headers=headers("txt-up"),
            json={"text": TEXT_BODY, "source_processing_consent": True},
        )
        assert created.status_code == 202, created.text
        paper_id = created.json()["data"]["paper_id"]
        text_sha = hashlib.sha256(TEXT_BODY.encode()).hexdigest()
        env["provider"].body = document(paper_id, text_sha)
        result = extract(client, paper_id, env["session"], "ext-txt")
        assert result.status_code == 202, result.text

        user = env["provider"].requests[0].messages[1].content
        assert user.endswith(TEXT_BODY)
        assert f"pdf_sha256: {text_sha}" in user
        payload = stored_payload(client, paper_id)
        assert payload["provider"] == {
            "provider_config_id": "parsed-endpoint", "model_id": "model-a",
            "prompt_version": "prompt-1", "policy_version": "policy-1",
            "content_sha256": text_sha,
            "context_hash": hashlib.sha256(TEXT_BODY.encode()).hexdigest(),
        }
        assert payload["warnings"] == []
        assert payload["source_refs"][0]["pdf_sha256"] == text_sha
