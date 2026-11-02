"""Experiment-level codegen HTTP contract (paper -> reviewed code artifact).

Full-HTTP flow: project -> paper -> provider extraction (fake transport) ->
experiment -> POST codegen with a runtime-injected fake-provider CodegenService.
Covers the attempt record shape, artifact read-back via GET, membership
checks, idempotency (replay/conflict/missing key), and policy rejection with
an honest retry afterwards.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "api"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "persistence"))

from p2r_api.main import create_app  # noqa: E402
from p2r_artifacts.store import LocalArtifactStore  # noqa: E402
from p2r_docparse.testing import minimal_pdf  # noqa: E402
from p2r_orchestration.codegen_service import CodegenService  # noqa: E402
from p2r_orchestration.extraction_service import ExtractionService  # noqa: E402
from p2r_orchestration.providers import (  # noqa: E402
    Capabilities,
    CompletionRequest,
    NormalizedEvent,
    OpenAICompatibleProvider,
    ProviderConfig,
    TransportResponse,
)
from p2r_persistence.models import (  # noqa: E402
    AttemptRecord,
    MembershipRecord,
    ProjectRecord,
    UserRecord,
)

PDF = minimal_pdf(["Codegen seam source text"]).rstrip(b"\r\n").replace(b"\xe2\xe3\xcf\xd3", b"ABCD")  # utf-8 safe, xref offsets unchanged
SECRET = "sk-codegen-secret-abcdef123456"
ENDPOINT = "codegen-endpoint"
MODEL_ID = "model-codegen"

FAKE_CODE = '''# Method: seeded rule-based binary classifier.
# Data source: synthetic, generated locally via random.Random(seed).
import json
import random

SEED = 7
N = 40


def main():
    rng = random.Random(SEED)
    rows = []
    labels = {}
    for i in range(N):
        x = rng.random()
        y = rng.random()
        label = 1 if (x + y) > 1.0 else 0
        rows.append((str(i), x, y, label))
        labels[str(i)] = label
    predictions = [
        {"id": rid, "prediction": (1 if (x + y) > 0.98 else 0)}
        for rid, x, y, _ in rows
    ]
    with open("predictions.json", "w") as handle:
        json.dump(predictions, handle)
    with open("labels.json", "w") as handle:
        json.dump(labels, handle)
    correct = sum(1 for p in predictions if p["prediction"] == labels[p["id"]])
    print("SUMMARY " + json.dumps({"n": N, "accuracy": correct / N}))


main()
'''

BAD_CODE = FAKE_CODE.replace("import random", "import os\nimport random")


class ScriptedTransport:
    """One scripted OpenAI-compatible body per call; records requests."""

    def __init__(self, body: bytes):
        self.body, self.calls = body, []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return TransportResponse(status=200, body=self.body)


class FakeCodegenProvider:
    """Provider protocol fake for CodegenService (no transport, no network)."""

    def __init__(self, code: str, notes: str = "fake notes"):
        self.code, self.notes = code, notes
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest):
        self.requests.append(request)
        yield NormalizedEvent(
            type="completed", call_id=request.call_id,
            text=json.dumps({"code": self.code, "notes": self.notes}),
        )


def openai_body(text: str) -> bytes:
    return json.dumps({
        "model": MODEL_ID,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }).encode()


def evidence(value, status="supported", refs=("ref-1",)):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def extraction_document(paper_id: str, paper_sha: str) -> dict:
    return {
        "schema_version": "article-extraction.v1",
        "title": evidence("Seeded baseline study"),
        "task": evidence("binary classification"),
        "methods": [evidence("rule-based baseline", "inferred")],
        "formulas": [evidence("y = 1[x1 + x2 > t]")],
        "datasets": [evidence("synthetic local dataset")],
        "candidate_experiments": [{
            "candidate_id": "cand-sandbox", "recipe_id": "sandbox_python",
            "recipe_version": "1", "support_status": "supported",
            "parameters": {"seed": 7, "max_seconds": 60},
            "required_dependencies": [], "required_datasets": [],
            "resource_requirements": {"cpu": "1", "memory": "512mb", "gpu": None},
            "estimated_runtime": None, "source_refs": ["ref-1"],
        }],
        "warnings": [], "limitations": [],
        "source_refs": [{
            "id": "ref-1", "paper_id": paper_id, "quote": "seeded rule baseline",
            "page": 1, "pdf_sha256": paper_sha,
        }],
    }


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_url=f"sqlite:///{tmp_path / 'codegen.sqlite3'}",
        supervisor=object(),
        secure_cookies=False,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        development_auth=True,
    )


@pytest.fixture
def seeded(app):
    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        owner = UserRecord(id="owner", display_name="owner", can_create_project=True)
        outsider = UserRecord(id="outsider", display_name="outsider", can_create_project=True)
        db.add_all([owner, outsider])
    return app


def headers(key, actor="owner"):
    return {"X-Actor-ID": actor, "Idempotency-Key": key}


def make_session(app) -> str:
    return app.state.runtime["provider_secret_store"].put(
        base_url="https://provider.example/v1",
        endpoint_alias=ENDPOINT,
        model_id=MODEL_ID,
        api_key=SECRET,
        capabilities=Capabilities(text="supported", structured_output="supported"),
    ).id


def inject_extraction(app, paper_id: str, paper_sha: str):
    transport = ScriptedTransport(openai_body(json.dumps(extraction_document(paper_id, paper_sha))))
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id=ENDPOINT, revision="1", model_id=MODEL_ID, display_name="Codegen seam",
            endpoint_alias=ENDPOINT, base_url="https://provider.example/v1",
            secret_env="P2R_CODEGEN_SEAM_KEY",
            capabilities=Capabilities(text="supported", structured_output="supported"),
        ),
        transport=transport,
        secret_resolver=lambda: SECRET,
    )
    app.state.runtime["extraction_service"] = ExtractionService(provider)


def inject_codegen(app, code: str = FAKE_CODE):
    app.state.runtime["codegen_service"] = CodegenService(
        FakeCodegenProvider(code), app.state.runtime["artifact_store"])


def setup_flow(app) -> dict:
    """Drive project -> paper -> extraction -> experiment over pure HTTP."""
    with TestClient(app, base_url="https://testserver") as client:
        project = client.post("/api/v1/projects", headers=headers("proj"),
                              json={"name": "codegen project"})
        assert project.status_code in (200, 201), project.text
        project_id = project.json()["data"]["id"]

        paper = client.post(
            f"/api/v1/projects/{project_id}/papers", headers=headers("paper"),
            files={"file": ("paper.pdf", PDF, "application/pdf")},
            data={"source_processing_consent": "true"},
        )
        assert paper.status_code == 202, paper.text
        paper_id = paper.json()["data"]["paper_id"]

        session_id = make_session(app)
        paper_sha = hashlib.sha256(PDF).hexdigest()
        inject_extraction(app, paper_id, paper_sha)
        extraction = client.post(
            f"/api/v1/papers/{paper_id}/extractions", headers=headers("extract"),
            json={"reason": "codegen flow", "provider_session_id": session_id},
        )
        assert extraction.status_code == 202, extraction.text
        extraction_id = extraction.json()["data"]["extraction_id"]

        experiment = client.post(
            f"/api/v1/projects/{project_id}/experiments", headers=headers("experiment"),
            json={"paper_id": paper_id, "extraction_id": extraction_id,
                  "candidate_experiment_id": "cand-sandbox",
                  "recipe_id": "sandbox_python", "recipe_version": "1"},
        )
        assert experiment.status_code == 201, experiment.text
        experiment_id = experiment.json()["data"]["experiment_id"]
    return {"project": project_id, "paper": paper_id, "extraction": extraction_id,
            "experiment": experiment_id, "session": session_id}


def test_full_http_codegen_creates_attempt_artifact_and_readback(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app)
    expected_sha = hashlib.sha256(FAKE_CODE.encode("utf-8")).hexdigest()

    with TestClient(app, base_url="https://testserver") as client:
        created = client.post(
            f"/api/v1/experiments/{ids['experiment']}/codegen",
            headers=headers("codegen-1"),
            json={"provider_session_id": ids["session"], "reason": "generate",
                  "prompt_version": "codegen-prompt-1"},
        )
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        assert data["code"] == FAKE_CODE
        assert data["code_sha256"] == expected_sha
        assert data["attempt_index"] == 1
        assert data["notes"] == "fake notes"
        assert SECRET not in created.text

        # Attempt record is experiment-level with the cross-task manifest.
        factory = app.state.runtime["session_factory"]
        with factory() as db:
            rows = db.scalars(select(AttemptRecord).where(
                AttemptRecord.experiment_id == ids["experiment"])).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.id == data["attempt_id"]
            assert row.run_id is None
            assert row.source == "model"
            assert row.status == "candidate"
            assert row.base_code_hash == expected_sha
            assert row.code_manifest_json == {
                "code_artifact_id": expected_sha,
                "code_sha256": expected_sha,
                "generator_model": MODEL_ID,
                "prompt_version": "codegen-prompt-1",
                "policy_version": "codegen-policy-1",
            }

        # Artifact bytes read back byte-identical from the content address.
        store = app.state.runtime["artifact_store"]
        assert store.open_bytes(expected_sha) == FAKE_CODE.encode("utf-8")

        readback = client.get(
            f"/api/v1/experiments/{ids['experiment']}/attempts/1/code",
            headers={"X-Actor-ID": "owner"},
        )
        assert readback.status_code == 200, readback.text
        body = readback.json()["data"]
        assert body["code"] == FAKE_CODE
        assert body["code_sha256"] == expected_sha
        assert body["attempt_id"] == data["attempt_id"]
        assert body["manifest"] == row.code_manifest_json

        # Unknown attempt index is a plain 404.
        missing = client.get(
            f"/api/v1/experiments/{ids['experiment']}/attempts/2/code",
            headers={"X-Actor-ID": "owner"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_second_codegen_increments_attempt_index(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app)
    with TestClient(app, base_url="https://testserver") as client:
        first = client.post(f"/api/v1/experiments/{ids['experiment']}/codegen",
                            headers=headers("gen-a"), json={"provider_session_id": ids["session"]})
        second = client.post(f"/api/v1/experiments/{ids['experiment']}/codegen",
                             headers=headers("gen-b"), json={"provider_session_id": ids["session"]})
        assert first.status_code == 201 and second.status_code == 201
        assert first.json()["data"]["attempt_index"] == 1
        assert second.json()["data"]["attempt_index"] == 2
        assert first.json()["data"]["attempt_id"] != second.json()["data"]["attempt_id"]


def test_codegen_idempotency_replay_conflict_and_missing_key(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app)
    body = {"provider_session_id": ids["session"], "reason": "generate"}
    with TestClient(app, base_url="https://testserver") as client:
        url = f"/api/v1/experiments/{ids['experiment']}/codegen"
        first = client.post(url, headers=headers("idem-1"), json=body)
        assert first.status_code == 201
        replay = client.post(url, headers=headers("idem-1"), json=body)
        # The cached response replays with the endpoint's declared status.
        assert replay.status_code in (200, 201)
        assert replay.json()["data"] == first.json()["data"]
        conflict = client.post(url, headers=headers("idem-1"),
                               json={**body, "reason": "different"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        missing = client.post(url, headers={"X-Actor-ID": "owner"}, json=body)
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "VALIDATION_ERROR"


def test_codegen_policy_rejection_is_visible_and_key_stays_usable(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app, code=BAD_CODE)
    with TestClient(app, base_url="https://testserver") as client:
        url = f"/api/v1/experiments/{ids['experiment']}/codegen"
        rejected = client.post(url, headers=headers("rejected"),
                               json={"provider_session_id": ids["session"]})
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "CODEGEN_POLICY_REJECTED"
        assert "reason" in rejected.json()["error"]["details"]

        factory = app.state.runtime["session_factory"]
        with factory() as db:
            assert db.scalars(select(AttemptRecord).where(
                AttemptRecord.experiment_id == ids["experiment"])).all() == []

        # Fixing the provider output and retrying with the SAME key succeeds:
        # a rejected generation must not poison the idempotency claim.
        inject_codegen(app, code=FAKE_CODE)
        retry = client.post(url, headers=headers("rejected"),
                            json={"provider_session_id": ids["session"]})
        assert retry.status_code == 201, retry.text
        assert retry.json()["data"]["attempt_index"] == 1


def test_codegen_membership_and_body_allowlist(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app)
    with TestClient(app, base_url="https://testserver") as client:
        url = f"/api/v1/experiments/{ids['experiment']}/codegen"
        outsider = client.post(url, headers=headers("outsider-key", actor="outsider"),
                               json={"provider_session_id": ids["session"]})
        assert outsider.status_code == 404
        assert outsider.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        # Unknown body keys (specifically credentials) are refused.
        smuggled = client.post(url, headers=headers("smuggle-key"),
                               json={"provider_session_id": ids["session"],
                                     "api_key": "sk-leak"})
        assert smuggled.status_code == 422
        assert smuggled.json()["error"]["code"] == "VALIDATION_ERROR"

        unknown_experiment = client.post("/api/v1/experiments/missing/codegen",
                                         headers=headers("missing-key"),
                                         json={"provider_session_id": ids["session"]})
        assert unknown_experiment.status_code == 404


def test_codegen_requires_live_provider_session(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app)
    with TestClient(app, base_url="https://testserver") as client:
        url = f"/api/v1/experiments/{ids['experiment']}/codegen"
        no_session = client.post(url, headers=headers("nosession"), json={})
        assert no_session.status_code == 409
        assert no_session.json()["error"]["code"] == "PROVIDER_SESSION_UNAVAILABLE"

        dead = client.post(url, headers=headers("deadsession"),
                           json={"provider_session_id": "ps_dead"})
        assert dead.status_code == 409
        assert dead.json()["error"]["code"] == "PROVIDER_SESSION_UNAVAILABLE"


def test_codegen_read_requires_membership(seeded):
    app = seeded
    ids = setup_flow(app)
    inject_codegen(app)
    with TestClient(app, base_url="https://testserver") as client:
        created = client.post(f"/api/v1/experiments/{ids['experiment']}/codegen",
                              headers=headers("readback"), json={"provider_session_id": ids["session"]})
        assert created.status_code == 201
        outsider = client.get(
            f"/api/v1/experiments/{ids['experiment']}/attempts/1/code",
            headers={"X-Actor-ID": "outsider"},
        )
        assert outsider.status_code == 404
