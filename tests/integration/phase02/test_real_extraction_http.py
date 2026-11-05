"""End-to-end HTTP: runtime provider session + SessionExtractionService.

create_app's default runtime is rewired to a SessionExtractionService backed
by the app's own provider secret store and a scripted OpenAI-compatible
transport (no network I/O). The full product flow — pasted-text paper,
provider session via POST /api/v1/provider-sessions, provider extraction —
must produce a ready extraction with candidate experiments, while the api_key
never appears in any response or database row.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_orchestration.providers import TransportResponse
from p2r_orchestration.session_extraction import SessionExtractionService
from p2r_persistence.models import ExtractionRecord, MembershipRecord, ProjectRecord, UserRecord

TEXT_BODY = (
    "Small CNN baseline for image classification. We train on CIFAR-10 and "
    "report top-1 accuracy against the linear baseline."
)
PAPER_SHA256 = hashlib.sha256(TEXT_BODY.encode("utf-8")).hexdigest()
API_KEY = "sk-test-integration-abcdef123456"
BASE_URL = "https://api.example.com"
ENDPOINT_ALIAS = "openai-compat"
MODEL_ID = "gpt-test-1"


def evidence(value, status="supported", refs=("ref-1",)):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def document(paper_id: str, **changes):
    body = {
        "schema_version": "article-extraction.v1",
        "title": evidence("CNN baseline study"),
        "task": evidence("image classification"),
        "methods": [evidence("CNN baseline", "inferred")],
        "formulas": [],
        "datasets": [evidence("CIFAR-10")],
        "candidate_experiments": [{
            "candidate_id": "cand-1", "recipe_id": "fixture-recipe", "recipe_version": "1",
            "support_status": "supported", "parameters": {},
            "required_dependencies": [], "required_datasets": [],
            "resource_requirements": {}, "estimated_runtime": None,
            "source_refs": ["ref-1"],
        }],
        "warnings": [], "limitations": [],
        "source_refs": [{
            "id": "ref-1", "paper_id": paper_id, "quote": "classify images",
            "page": 1, "pdf_sha256": PAPER_SHA256,
        }],
    }
    body.update(changes)
    return body


def openai_body(text: str) -> bytes:
    return json.dumps({
        "model": MODEL_ID,
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    }).encode()


class ScriptedTransport:
    """AsyncTransport fake: one scripted OpenAI-compatible body, records calls."""

    def __init__(self, body: bytes = b"", status: int = 200):
        self.body, self.status, self.calls = body, status, []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return TransportResponse(status=self.status, body=self.body)


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'real-extraction.sqlite3'}",
        secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "store"),
        development_auth=True,
    )


@pytest.fixture
def seeded(app):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="Real extraction owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="Real extraction project", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        ids = {"user": user.id, "project": project.id}
    return app, ids


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def rewire_extraction_service(app, transport) -> None:
    """Replace the default runtime seam with a session service over a fake transport."""
    app.state.runtime["extraction_service"] = SessionExtractionService(
        app.state.runtime["provider_secret_store"], transport=transport,
    )


def upload_text_paper(client, project_id, *, key="text-paper"):
    result = client.post(
        f"/api/v1/projects/{project_id}/papers/text",
        headers=headers(key),
        json={"text": TEXT_BODY, "source_processing_consent": True},
    )
    assert result.status_code == 202, result.text
    return result.json()["data"]["paper_id"]


def create_provider_session(client, *, key="provider-session", api_key=API_KEY):
    result = client.post(
        "/api/v1/provider-sessions",
        headers=headers(key),
        json={"base_url": BASE_URL, "endpoint_alias": ENDPOINT_ALIAS,
              "model_id": MODEL_ID, "api_key": api_key},
    )
    assert result.status_code == 201, result.text
    assert api_key not in result.text
    return result.json()["data"]["provider_session_id"]


def request_extraction(client, paper_id, session_id, *, key="extract"):
    return client.post(
        f"/api/v1/papers/{paper_id}/extractions",
        headers=headers(key),
        json={"reason": "provider extraction", "provider_session_id": session_id},
    )


def assert_database_has_no_plaintext_key(app) -> None:
    from sqlalchemy import inspect as sa_inspect, text as sql_text

    factory = app.state.runtime["session_factory"]
    with factory() as db:
        engine = db.get_bind()
        for table in sa_inspect(engine).get_table_names():
            rows = db.execute(sql_text(f'SELECT * FROM "{table}"')).mappings().all()
            for row in rows:
                blob = json.dumps(dict(row), default=str, ensure_ascii=False)
                assert API_KEY not in blob, f"api_key leaked into table {table}"


def test_real_extraction_flow_over_provider_session(seeded):
    app, ids = seeded
    transport = ScriptedTransport()
    rewire_extraction_service(app, transport)

    with TestClient(app) as client:
        paper_id = upload_text_paper(client, ids["project"])
        # The scripted completion must name this exact paper and its digest.
        transport.body = openai_body(json.dumps(document(paper_id)))

        session_id = create_provider_session(client)
        result = request_extraction(client, paper_id, session_id, key="extract-ok")
        assert result.status_code == 202, result.text
        assert result.json()["data"]["status"] == "ready"
        extraction_id = result.json()["data"]["extraction_id"]
        assert extraction_id

        fetched = client.get(f"/api/v1/papers/{paper_id}/extraction",
                             headers={"X-Actor-ID": "owner"})
        assert fetched.status_code == 200, fetched.text
        payload = fetched.json()["data"]
        assert payload["extraction_id"] == extraction_id
        assert payload["candidate_experiments"], "candidate experiments missing"
        assert payload["candidate_experiments"][0]["candidate_id"] == "cand-1"
        assert payload["candidate_experiments"][0]["execution_support"] == "supported"
        assert payload["provider"]["provider_config_id"] == ENDPOINT_ALIAS
        assert payload["provider"]["model_id"] == MODEL_ID

        # The request really traversed the OpenAI-compatible chain.
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["url"] == f"{BASE_URL}/chat/completions"
        assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
        sent = json.loads(call["body"])
        assert sent["model"] == MODEL_ID
        sent_content = sent["messages"][1]["content"]
        # Provenance block with the exact identifiers, then the article text.
        assert sent_content.startswith("PROVENANCE (every source_ref must cite these exact values):")
        assert f"paper_id: {paper_id}" in sent_content
        assert f"pdf_sha256: {PAPER_SHA256}" in sent_content
        assert sent_content.endswith(TEXT_BODY)

        # No response ever carried the api_key plaintext.
        assert API_KEY not in result.text
        assert API_KEY not in fetched.text

        # Replay with the same idempotency key is a no-op at the provider.
        replay = request_extraction(client, paper_id, session_id, key="extract-ok")
        assert replay.json()["data"] == result.json()["data"]
        assert len(transport.calls) == 1

    with app.state.runtime["session_factory"]() as db:
        rows = db.query(ExtractionRecord).filter_by(paper_id=paper_id).all()
        assert len(rows) == 1 and rows[0].status == "ready"
        assert API_KEY not in json.dumps(rows[0].payload_json, default=str)
    assert_database_has_no_plaintext_key(app)


def test_re_extraction_replaces_an_unused_extraction(seeded):
    app, ids = seeded
    transport = ScriptedTransport()
    rewire_extraction_service(app, transport)

    with TestClient(app) as client:
        paper_id = upload_text_paper(client, ids["project"], key="text-again")
        transport.body = openai_body(json.dumps(document(paper_id)))
        session_id = create_provider_session(client, key="provider-again")
        first = request_extraction(client, paper_id, session_id, key="extract-1")
        second = request_extraction(client, paper_id, session_id, key="extract-2")
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        new_id = second.json()["data"]["extraction_id"]
        assert new_id != first.json()["data"]["extraction_id"]
        fetched = client.get(f"/api/v1/papers/{paper_id}/extraction", headers={"X-Actor-ID": "owner"})
        assert fetched.json()["data"]["extraction_id"] == new_id
        assert len(transport.calls) == 2

    with app.state.runtime["session_factory"]() as db:
        rows = db.query(ExtractionRecord).filter_by(paper_id=paper_id).all()
        assert [row.id for row in rows] == [new_id]


def test_provider_auth_failure_returns_safe_code_and_persists_nothing(seeded):
    app, ids = seeded
    transport = ScriptedTransport(b"", status=401)
    rewire_extraction_service(app, transport)

    with TestClient(app) as client:
        paper_id = upload_text_paper(client, ids["project"], key="text-auth")
        transport.body = openai_body(json.dumps(document(paper_id)))
        session_id = create_provider_session(client, key="provider-auth")

        result = request_extraction(client, paper_id, session_id, key="extract-auth")
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "PROVIDER_AUTH_INVALID"
        assert API_KEY not in result.text
        assert len(transport.calls) == 1

        with app.state.runtime["session_factory"]() as db:
            assert db.query(ExtractionRecord).filter_by(paper_id=paper_id).count() == 0
