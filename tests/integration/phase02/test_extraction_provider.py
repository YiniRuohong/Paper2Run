"""Provider-backed extraction seam: mapping + HTTP wiring.

Covers the pure projection→payload_json mapping and the create_extraction
provider seam (runtime-injected ExtractionService + runtime secret store):
a legal fake provider end-to-end, secret/raw-response never persisted, and
source refs that point at another paper being refused before any write.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from p2r_api.extraction_payload import (
    ExtractionPayloadError,
    build_extraction_payload,
)
from p2r_api.main import create_app
from p2r_artifacts.store import LocalArtifactStore
from p2r_docparse.testing import minimal_pdf
from p2r_orchestration.extraction_service import ExtractionService
from p2r_orchestration.providers import (
    Capabilities,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    TransportResponse,
)
from p2r_persistence.models import ExtractionRecord, MembershipRecord, ProjectRecord, UserRecord

PAPER_ID = "paper-seam"
PAPER_SHA256 = "c" * 64
SECRET = "sk-live-secret-abcdef123456"
RAW_RESPONSE_SNIPPET = '{"raw": "provider internals"}'


def evidence(value, status="supported", refs=("ref-1",)):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def document(**changes):
    body = {
        "schema_version": "article-extraction.v1",
        "title": evidence("Deep learning"),
        "task": evidence("classification"),
        "methods": [evidence("CNN baseline", "inferred")],
        "formulas": [evidence("y = Wx")],
        "datasets": [evidence("CIFAR-10")],
        "candidate_experiments": [{
            "candidate_id": "cand-1", "recipe_id": "fixture-recipe", "recipe_version": "1",
            "support_status": "supported", "parameters": {}, "required_dependencies": [],
            "required_datasets": [], "resource_requirements": {}, "estimated_runtime": None,
            "source_refs": ["ref-1"],
        }],
        "warnings": [], "limitations": [],
        "source_refs": [{
            "id": "ref-1", "paper_id": PAPER_ID, "quote": "classify images",
            "page": 1, "pdf_sha256": PAPER_SHA256,
        }],
    }
    body.update(changes)
    return body


def projection(**changes):
    body = {
        "paper_id": PAPER_ID,
        "content_sha256": PAPER_SHA256,
        "context_hash": "b" * 64,
        "provider_config_id": "prov-1",
        "model_id": "model-a",
        "prompt_version": "p-1",
        "policy_version": "v-1",
        "extraction": document(),
    }
    body.update(changes)
    return body


# ---------------------------------------------------------------- pure mapping


def test_mapping_keeps_payload_json_contract_and_safe_metadata():
    payload = build_extraction_payload(projection(), paper_id=PAPER_ID, paper_sha256=PAPER_SHA256)
    assert payload["paper_id"] == PAPER_ID
    assert payload["schema_version"] == "1.0"
    assert set(payload) == {
        "extraction_id", "paper_id", "schema_version", "fields", "methods", "formulas",
        "candidate_experiments", "source_refs", "warnings", "provider",
    }
    assert payload["fields"]["task"] == {
        "value": "classification", "evidence_status": "supported", "source_refs": ["ref-1"],
    }
    assert payload["methods"][0]["label"] == "CNN baseline"
    assert payload["formulas"][0]["label"] == "y = Wx"
    candidate = payload["candidate_experiments"][0]
    assert candidate["id"] == "cand-1" and candidate["recipe_id"] == "fixture-recipe"
    assert candidate["execution_support"] == "supported"
    assert payload["source_refs"][0]["pdf_sha256"] == PAPER_SHA256
    # Provider metadata preserved from the safe projection (allow-listed keys only).
    assert payload["provider"] == {
        "provider_config_id": "prov-1", "model_id": "model-a",
        "prompt_version": "p-1", "policy_version": "v-1",
        "content_sha256": PAPER_SHA256, "context_hash": "b" * 64,
    }
    # No secret or raw response key may ever appear in the payload.
    dumped = json.dumps(payload)
    assert SECRET not in dumped and "raw" not in payload.get("provider", {})


def test_mapping_rejects_foreign_source_refs():
    foreign = projection(extraction=document(source_refs=[{
        "id": "ref-1", "paper_id": "another-paper", "quote": "classify images",
        "page": 1, "pdf_sha256": PAPER_SHA256,
    }]))
    with pytest.raises(ExtractionPayloadError):
        build_extraction_payload(foreign, paper_id=PAPER_ID, paper_sha256=PAPER_SHA256)

    wrong_sha = projection(extraction=document(source_refs=[{
        "id": "ref-1", "paper_id": PAPER_ID, "quote": "classify images",
        "page": 1, "pdf_sha256": "0" * 64,
    }]))
    with pytest.raises(ExtractionPayloadError):
        build_extraction_payload(wrong_sha, paper_id=PAPER_ID, paper_sha256=PAPER_SHA256)

    foreign_paper = projection(paper_id="another-paper")
    with pytest.raises(ExtractionPayloadError):
        build_extraction_payload(foreign_paper, paper_id=PAPER_ID, paper_sha256=PAPER_SHA256)


# ---------------------------------------------------------------- HTTP fixtures

PDF = minimal_pdf(["P seam source text"]).rstrip(b"\r\n").replace(b"\xe2\xe3\xcf\xd3", b"ABCD")  # utf-8 safe, xref offsets unchanged


def openai_body(text: str, model_id: str = "model-a") -> bytes:
    return json.dumps({
        "model": model_id,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }).encode()


class ScriptedTransport:
    """AsyncTransport fake: returns one scripted OpenAI-compatible body, records calls."""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status, self.calls = body, status, []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return TransportResponse(status=self.status, body=self.body)


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'seam.sqlite3'}",
        supervisor=object(),
        secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "store"),
        development_auth=True,
    )


@pytest.fixture
def seeded(app):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        user = UserRecord(id="owner", display_name="Seam owner", can_create_project=True)
        db.add(user)
        db.flush()
        project = ProjectRecord(name="Seam project", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(MembershipRecord(project_id=project.id, user_id=user.id, role="owner"))
        ids = {"user": user.id, "project": project.id}
    return app, ids


def headers(key):
    return {"X-Actor-ID": "owner", "Idempotency-Key": key}


def upload_pdf(client, project_id, key="pdf-seam"):
    result = client.post(
        f"/api/v1/projects/{project_id}/papers",
        headers=headers(key),
        files={"file": ("paper.pdf", PDF, "application/pdf")},
        data={"source_processing_consent": "true"},
    )
    assert result.status_code == 202, result.text
    return result.json()["data"]["paper_id"]


def inject_service(app, provider):
    app.state.runtime["extraction_service"] = ExtractionService(provider)


def make_session(app, *, api_key=SECRET):
    return app.state.runtime["provider_secret_store"].put(
        base_url="https://provider.example/v1",
        endpoint_alias="seam-endpoint",
        model_id="model-a",
        api_key=api_key,
    )


def extract_body(session_id, **extra):
    return {"reason": "provider extraction", "provider_session_id": session_id, **extra}


def test_injected_fake_provider_extracts_and_persists_safe_payload(seeded, monkeypatch):
    app, ids = seeded
    # The uploaded PDF's artifact digest differs from the canned document's
    # refs, so pin the paper hash the fake document must match.
    import hashlib
    paper_sha = hashlib.sha256(PDF).hexdigest()
    transport = ScriptedTransport(openai_body(json.dumps(document(source_refs=[{
        "id": "ref-1", "paper_id": PAPER_ID, "quote": "classify images",
        "page": 1, "pdf_sha256": paper_sha,
    }]))))
    config = ProviderConfig(
        id="seam-endpoint", revision="1", model_id="model-a", display_name="Seam",
        endpoint_alias="seam-endpoint", base_url="https://provider.example/v1",
        secret_env="P2R_SEAM_KEY", capabilities=Capabilities(text="supported", structured_output="supported"),
    )
    monkeypatch.setenv("P2R_SEAM_KEY", SECRET)
    provider = OpenAICompatibleProvider(config, transport=transport)
    inject_service(app, provider)

    with TestClient(app) as client:
        paper_id = upload_pdf(client, ids["project"])
        transport.body = openai_body(json.dumps(document(source_refs=[{
            "id": "ref-1", "paper_id": paper_id, "quote": "classify images",
            "page": 1, "pdf_sha256": paper_sha,
        }])))
        session = make_session(app)
        result = client.post(
            f"/api/v1/papers/{paper_id}/extractions",
            headers=headers("seam-ok"),
            json=extract_body(session.id),
        )
        assert result.status_code == 202, result.text
        extraction_id = result.json()["data"]["extraction_id"]
        assert extraction_id

        fetched = client.get(f"/api/v1/papers/{paper_id}/extraction", headers={"X-Actor-ID": "owner"})
        assert fetched.status_code == 200, fetched.text
        payload = fetched.json()["data"]
        assert payload["paper_id"] == paper_id
        assert payload["extraction_id"] == extraction_id
        assert payload["fields"]["task"]["value"] == "classification"
        assert payload["source_refs"][0]["pdf_sha256"] == paper_sha
        assert payload["provider"]["provider_config_id"] == "seam-endpoint"
        assert payload["provider"]["model_id"] == "model-a"
        # Prove this reached the transport rather than a pre-provider guard.
        assert len(transport.calls) == 1
        assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
        sent_content = json.loads(transport.calls[0]["body"])["messages"][1]["content"]
        # Provenance block with the exact identifiers, then the article bytes.
        assert sent_content.startswith("PROVENANCE (every source_ref must cite these exact values):")
        assert f"paper_id: {paper_id}" in sent_content
        assert f"pdf_sha256: {paper_sha}" in sent_content
        assert sent_content.endswith(PDF.decode())
        assert SECRET not in fetched.text
        replay = client.post(
            f"/api/v1/papers/{paper_id}/extractions",
            headers=headers("seam-ok"), json=extract_body(session.id),
        )
        assert replay.json()["data"] == result.json()["data"]
        assert len(transport.calls) == 1

        with app.state.runtime["session_factory"]() as db:
            rows = db.query(ExtractionRecord).filter_by(paper_id=paper_id).all()
            assert len(rows) == 1 and rows[0].status == "ready"
            blob = json.dumps(rows[0].payload_json, default=str)
            assert SECRET not in blob and RAW_RESPONSE_SNIPPET not in blob


def test_api_key_in_body_is_refused_and_never_used(seeded):
    app, ids = seeded
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    config = ProviderConfig(
        id="seam-endpoint", revision="1", model_id="model-a", display_name="Seam",
        endpoint_alias="seam-endpoint", base_url="https://provider.example/v1",
        secret_env="P2R_SEAM_KEY", capabilities=Capabilities(text="supported", structured_output="supported"),
    )
    provider = OpenAICompatibleProvider(config, transport=transport)
    inject_service(app, provider)

    with TestClient(app) as client:
        paper_id = upload_pdf(client, ids["project"], key="pdf-apikey")
        session = make_session(app, api_key=SECRET)
        # Unknown field (api_key) in the extraction body is refused outright.
        bad = client.post(
            f"/api/v1/papers/{paper_id}/extractions",
            headers=headers("seam-apikey"),
            json={"reason": "provider extraction", "provider_session_id": session.id, "api_key": SECRET},
        )
        assert bad.status_code == 422, bad.text
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        assert SECRET not in bad.text
        # No provider call was attempted, and nothing was persisted.
        assert transport.calls == []
        with app.state.runtime["session_factory"]() as db:
            assert db.query(ExtractionRecord).filter_by(paper_id=paper_id).count() == 0


def test_provider_failure_persists_only_safe_code(seeded):
    app, ids = seeded
    # Provider fails; only the safe error code may reach the client and disk.
    class ExplodingProvider:
        async def complete(self, request):
            raise ProviderError("server_error")
            yield  # pragma: no cover
    inject_service(app, ExplodingProvider())
    # Session must exist for the seam to resolve config; key lives in the store only.
    session = make_session(app)

    with TestClient(app) as client:
        paper_id = upload_pdf(client, ids["project"], key="pdf-fail")
        result = client.post(
            f"/api/v1/papers/{paper_id}/extractions",
            headers=headers("seam-fail"),
            json=extract_body(session.id),
        )
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "PROVIDER_SERVER_ERROR"
        assert SECRET not in result.text
        with app.state.runtime["session_factory"]() as db:
            # Failure wrote no ready extraction; only the operation row (status
            # outside payload) may exist, and no payload/error carries a secret.
            for row in db.query(ExtractionRecord).filter_by(paper_id=paper_id).all():
                assert row.status != "ready"
                assert SECRET not in json.dumps(row.payload_json or {}, default=str)


def test_source_ref_for_another_paper_is_refused_without_persisting(seeded, monkeypatch):
    app, ids = seeded
    monkeypatch.setenv("P2R_SEAM_KEY", SECRET)
    # Valid OpenAI-compatible response, but its source_ref names another paper.
    foreign = document(source_refs=[{
        "id": "ref-1", "paper_id": "another-paper", "quote": "classify images",
        "page": 1, "pdf_sha256": PAPER_SHA256,
    }])
    transport = ScriptedTransport(openai_body(json.dumps(foreign)))
    config = ProviderConfig(
        id="seam-endpoint", revision="1", model_id="model-a", display_name="Seam",
        endpoint_alias="seam-endpoint", base_url="https://provider.example/v1",
        secret_env="P2R_SEAM_KEY", capabilities=Capabilities(text="supported", structured_output="supported"),
    )
    provider = OpenAICompatibleProvider(config, transport=transport)
    inject_service(app, provider)
    session = make_session(app)

    with TestClient(app) as client:
        paper_id = upload_pdf(client, ids["project"], key="pdf-foreign")
        result = client.post(
            f"/api/v1/papers/{paper_id}/extractions",
            headers=headers("seam-foreign"),
            json=extract_body(session.id),
        )
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "PROVIDER_INVALID_RESPONSE"
        assert len(transport.calls) == 1
        with app.state.runtime["session_factory"]() as db:
            assert db.query(ExtractionRecord).filter_by(paper_id=paper_id).count() == 0


def test_without_injected_service_existing_deterministic_behavior_unchanged(seeded):
    app, ids = seeded
    with TestClient(app) as client:
        paper_id = upload_pdf(client, ids["project"], key="pdf-det")
        # No extraction_service injected → provider body fields are irrelevant;
        # request without provider_session_id takes the deterministic PDF path.
        result = client.post(
            f"/api/v1/papers/{paper_id}/extractions",
            headers=headers("seam-det"),
            json={"reason": "deterministic"},
        )
        assert result.status_code == 202, result.text
        payload = client.get(f"/api/v1/papers/{paper_id}/extraction", headers={"X-Actor-ID": "owner"}).json()["data"]
        assert payload["fields"] == {
            "task": {"value": None, "evidence_status": "missing", "source_refs": []},
            "dataset": {"value": None, "evidence_status": "missing", "source_refs": []},
            "optimizer": {"value": None, "evidence_status": "missing", "source_refs": []},
        }
        assert "provider" not in payload
        assert payload["source_refs"] and payload["source_refs"][0]["page"] == 1
