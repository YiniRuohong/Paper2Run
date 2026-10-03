from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from p2r_artifacts.store import LocalArtifactStore
from p2r_orchestration.codegen_service import (
    CodegenPolicyError,
    CodegenService,
    validate_generated_code,
)
from p2r_orchestration.providers import (
    CompletionRequest,
    NormalizedEvent,
    ProviderError,
)

# A policy-legal self-contained script: seeded synthetic binary
# classification with a simple rule, writing both contract files.
LEGAL_CODE = '''# Method: seeded rule-based binary classifier (nearest-baseline variant).
# Data source: fully synthetic, generated locally via random.Random(seed).
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


def completed_events(payload: dict) -> list[NormalizedEvent]:
    text = json.dumps(payload)
    return [
        NormalizedEvent(type="completed", call_id="c", text=text),
    ]


class FakeProvider:
    def __init__(self, payload: dict):
        self.payload = payload
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest):
        self.requests.append(request)
        for event in completed_events(self.payload):
            yield event


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(tmp_path / "artifacts")


def run_generate(service: CodegenService, **overrides):
    kwargs = dict(
        project_id="proj",
        experiment_id="exp",
        revision_id="rev",
        paper_sha256="b" * 64,
        extraction_payload={"candidate_experiments": [{"id": "c1"}]},
        spec={"recipe": {"id": "sandbox_python", "version": "1"}},
        provider_config_id="alias",
        model_id="model-x",
        call_id="call-1",
        prompt_version="prompt-1",
        policy_version="policy-1",
    )
    kwargs.update(overrides)
    return asyncio.run(service.generate(**kwargs))


def test_valid_script_passes_and_artifact_is_content_addressed(store):
    service = CodegenService(FakeProvider({"code": LEGAL_CODE, "notes": "ok"}), store)
    result = run_generate(service)
    assert result["code"] == LEGAL_CODE
    assert result["notes"] == "ok"
    digest = hashlib.sha256(LEGAL_CODE.encode("utf-8")).hexdigest()
    assert result["code_sha256"] == digest
    assert result["code_artifact_id"] == digest
    # The stored bytes read back byte-identical and hash to the manifest id.
    data = store.open_bytes(digest)
    assert data == LEGAL_CODE.encode("utf-8")
    assert hashlib.sha256(data).hexdigest() == digest
    assert result["generator"]["model_id"] == "model-x"
    assert result["generator"]["prompt_version"] == "prompt-1"


def test_import_os_rejected(store):
    code = LEGAL_CODE.replace("import random", "import os\nimport random")
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)
    service = CodegenService(FakeProvider({"code": code, "notes": "n"}), store)
    with pytest.raises(CodegenPolicyError):
        run_generate(service)
    # Rejected code is never stored: no artifact file exists under the root.
    assert [p for p in store.root.rglob("*") if p.is_file()] == []


def test_import_socket_rejected():
    code = LEGAL_CODE.replace("import random", "import socket\nimport random")
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_from_os_import_rejected():
    code = LEGAL_CODE.replace("import random", "from os import path\nimport random")
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_relative_import_rejected():
    code = LEGAL_CODE.replace("import random", "from . import sibling\nimport random")
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_eval_rejected():
    code = LEGAL_CODE.replace("main()\n", "eval('1')\nmain()\n")
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


@pytest.mark.parametrize("name", ["exec", "compile", "__import__", "globals"])
def test_dynamic_exec_call_family_rejected(name):
    code = LEGAL_CODE.replace("main()\n", f"{name}('x')\nmain()\n")
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_absolute_path_write_rejected():
    code = LEGAL_CODE.replace('open("predictions.json", "w")', 'open("/tmp/predictions.json", "w")')
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_parent_traversal_write_rejected():
    code = LEGAL_CODE.replace('open("labels.json", "w")', 'open("../labels.json", "w")')
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_append_mode_absolute_path_rejected():
    code = LEGAL_CODE.replace('open("predictions.json", "w")', 'open("/var/tmp/predictions.json", "a")')
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_exclusive_mode_absolute_path_rejected():
    code = LEGAL_CODE.replace('open("predictions.json", "w")', 'open("/var/tmp/p.json", "x")')
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_unresolvable_write_path_rejected():
    code = LEGAL_CODE.replace(
        'with open("predictions.json", "w") as handle:',
        'target = "predictions.json"\n    with open(target, "w") as handle:',
    )
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_read_of_absolute_path_is_allowed_by_static_policy():
    # The static policy restricts *write* modes; plain reads pass (runtime
    # isolation is the sandbox's job, not the static checker's).
    code = LEGAL_CODE.replace("\nmain()\n", '\nopen("/etc/hostname")\nmain()\n')
    validate_generated_code(code)  # no raise


def test_missing_predictions_reference_rejected():
    code = LEGAL_CODE.replace('"predictions.json"', '"preds.json"')
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_missing_labels_reference_rejected():
    code = LEGAL_CODE.replace('"labels.json"', '"truth.json"')
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_oversized_code_rejected():
    filler = "#" + "x" * (64 * 1024)
    code = LEGAL_CODE.replace("SEED = 7", filler)
    assert len(code.encode("utf-8")) > 64 * 1024
    with pytest.raises(CodegenPolicyError):
        validate_generated_code(code)


def test_syntax_error_rejected():
    with pytest.raises(CodegenPolicyError):
        validate_generated_code("def broken(:\n    pass\n")


def test_provider_request_shape(store):
    provider = FakeProvider({"code": LEGAL_CODE, "notes": "ok"})
    service = CodegenService(provider, store)
    run_generate(service)
    request = provider.requests[-1]
    assert request.max_output_tokens == 3000
    assert request.timeout_seconds == 240.0
    assert request.json_schema == {
        "name": "codegen",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["code", "notes"],
            "additionalProperties": False,
        },
    }
    assert request.messages[0].role == "system"
    assert "predictions.json" in request.messages[0].content


def test_non_json_provider_response_maps_to_provider_error(store):
    class RawProvider(FakeProvider):
        async def complete(self, request):
            yield NormalizedEvent(type="completed", call_id="c", text="not json")

    service = CodegenService(RawProvider({"code": LEGAL_CODE, "notes": "n"}), store)
    with pytest.raises(ProviderError):
        run_generate(service)


def test_wrong_shape_payload_maps_to_provider_error(store):
    service = CodegenService(FakeProvider({"code": LEGAL_CODE}), store)  # notes missing
    with pytest.raises(ProviderError):
        run_generate(service)
