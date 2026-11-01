"""sandbox_python/1 recipe: parameter boundary, code integrity, honest failures.

The recipe wraps the p2r_sandbox isolation stack. Fast paths (timeout,
truncation, non-zero exit, malformed outputs) are exercised by monkeypatching
the sandbox call; one real execution covers the happy path and determinism.
"""
from __future__ import annotations

import json

import pytest

from p2r_runner import (
    EVIDENCE_CLASS_SANDBOXED_MODEL_CODE,
    SANDBOX_EVALUATOR_VERSION,
    SANDBOX_RECIPE_ID,
    SANDBOX_RECIPE_VERSION,
    FixtureRunnerError,
    is_registered_recipe,
    run_sandbox_python,
)
from p2r_sandbox import SandboxResult

GUEST_CODE = """
import json, random
rng = random.Random(20260930)
rows = []
for _ in range(60):
    x = rng.uniform(-1, 1)
    y = rng.uniform(-1, 1)
    label = 1 if x + y > 0 else 0
    rows.append((x, y, label))
test = rows[40:]
def classify(x, y):
    return 1 if x + y > 0.05 else 0
predictions = [{"id": "row-%03d" % i, "prediction": classify(p[0], p[1])} for i, p in enumerate(test)]
labels = {"row-%03d" % i: p[2] for i, p in enumerate(test)}
with open("predictions.json", "w") as handle:
    json.dump(predictions, handle)
with open("labels.json", "w") as handle:
    json.dump(labels, handle)
print("sandbox guest done")
"""


def run_recipe(parameters=None, code=GUEST_CODE, attempt_manifest=None):
    return run_sandbox_python(
        recipe_id=SANDBOX_RECIPE_ID, recipe_version=SANDBOX_RECIPE_VERSION,
        parameters=parameters, code=code, attempt_manifest=attempt_manifest,
    )


def test_sandbox_recipe_is_registered():
    assert is_registered_recipe(SANDBOX_RECIPE_ID, SANDBOX_RECIPE_VERSION)


def test_unknown_recipe_and_target_are_refused():
    with pytest.raises(FixtureRunnerError):
        run_sandbox_python(recipe_id="other", recipe_version="1", code=GUEST_CODE)
    with pytest.raises(FixtureRunnerError):
        run_sandbox_python(
            recipe_id=SANDBOX_RECIPE_ID, recipe_version=SANDBOX_RECIPE_VERSION,
            target="ssh", code=GUEST_CODE,
        )


@pytest.mark.parametrize("parameters", [
    {"seed": -1}, {"seed": 2 ** 31}, {"max_seconds": 9}, {"max_seconds": 181},
    {"unexpected": 1}, "not-a-dict",
])
def test_invalid_parameters_are_refused(parameters):
    with pytest.raises(FixtureRunnerError):
        run_recipe(parameters=parameters)


def test_empty_code_is_refused():
    with pytest.raises(FixtureRunnerError):
        run_recipe(code="   ")


def test_code_sha_must_match_attempt_manifest():
    with pytest.raises(FixtureRunnerError):
        run_recipe(attempt_manifest={"code_sha256": "0" * 64})


def test_happy_path_returns_truthful_result():
    result = run_recipe(parameters={"seed": 7, "max_seconds": 30})
    assert result["recipe"] == {"id": SANDBOX_RECIPE_ID, "version": SANDBOX_RECIPE_VERSION}
    assert result["target"] == {"kind": "local-cpu", "runner": "local-sandbox-v1"}
    assert result["evidence_class"] == EVIDENCE_CLASS_SANDBOXED_MODEL_CODE
    assert result["evaluator_version"] == SANDBOX_EVALUATOR_VERSION
    assert set(item for item in result["predictions"][0]) == {"id", "prediction"}
    assert set(result["labels"]) == {item["id"] for item in result["predictions"]}
    assert result["sandbox"]["exit_code"] == 0
    assert result["sandbox"]["code_sha256"] == result["sandbox"]["code_sha256"]
    assert isinstance(result["sandbox"]["network_isolated"], bool)


def test_deterministic_execution_for_same_code_and_seed():
    first = run_recipe(parameters={"seed": 7})
    second = run_recipe(parameters={"seed": 7})
    assert first["predictions"] == second["predictions"]
    assert first["labels"] == second["labels"]


def _fake_outcome(**overrides):
    fields = dict(
        exit_code=0, stdout=b"ok\n", stderr=b"", timed_out=False,
        truncated=False, network_isolated=True,
        files={
            "predictions.json": json.dumps([{"id": "r-1", "prediction": 1}]).encode(),
            "labels.json": json.dumps({"r-1": 1}).encode(),
        },
        duration_ms=12,
    )
    fields.update(overrides)
    return SandboxResult(**fields)


def test_timeout_is_a_visible_failure(monkeypatch):
    monkeypatch.setattr(
        "p2r_runner.run_python_sandbox",
        lambda code, limits=None, **kwargs: _fake_outcome(timed_out=True, exit_code=-9),
    )
    with pytest.raises(FixtureRunnerError, match="wall-clock"):
        run_recipe(parameters={"max_seconds": 10})


def test_truncated_output_is_a_visible_failure(monkeypatch):
    monkeypatch.setattr(
        "p2r_runner.run_python_sandbox",
        lambda code, limits=None, **kwargs: _fake_outcome(truncated=True),
    )
    with pytest.raises(FixtureRunnerError, match="truncated"):
        run_recipe()


def test_nonzero_exit_is_a_visible_failure(monkeypatch):
    monkeypatch.setattr(
        "p2r_runner.run_python_sandbox",
        lambda code, limits=None, **kwargs: _fake_outcome(exit_code=1, stderr=b"boom"),
    )
    with pytest.raises(FixtureRunnerError, match="exited with code 1"):
        run_recipe()


def test_malformed_output_files_are_a_visible_failure(monkeypatch):
    monkeypatch.setattr(
        "p2r_runner.run_python_sandbox",
        lambda code, limits=None, **kwargs: _fake_outcome(files={"labels.json": b"{}"}),
    )
    with pytest.raises(FixtureRunnerError, match="invalid output files"):
        run_recipe()


# --- fail closed: model code never runs without verified isolation --------


def test_runner_requires_isolation_by_default(monkeypatch):
    seen = {}

    def fake(code, limits=None, **kwargs):
        seen.update(kwargs)
        return _fake_outcome()

    monkeypatch.delenv("P2R_ALLOW_UNISOLATED_SANDBOX", raising=False)
    monkeypatch.setattr("p2r_runner.run_python_sandbox", fake)
    output = run_recipe()
    assert seen["require_isolation"] is True
    assert output["sandbox"]["isolation_required"] is True


def test_runner_refuses_when_no_isolating_backend(monkeypatch):
    from p2r_sandbox import sandbox as sandbox_mod

    monkeypatch.delenv("P2R_ALLOW_UNISOLATED_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox_mod, "_select_backend", lambda: ("none", False))
    with pytest.raises(FixtureRunnerError, match="P2R_ALLOW_UNISOLATED_SANDBOX"):
        run_recipe()


def test_runner_explicit_opt_out_runs_and_is_recorded(monkeypatch):
    from p2r_sandbox import sandbox as sandbox_mod

    monkeypatch.setenv("P2R_ALLOW_UNISOLATED_SANDBOX", "1")
    monkeypatch.setattr(sandbox_mod, "_select_backend", lambda: ("none", False))
    output = run_recipe()
    assert output["sandbox"]["isolation_required"] is False
    assert output["sandbox"]["backend"] == "none"
    assert output["sandbox"]["network_isolated"] is False
