from __future__ import annotations

import csv
import hashlib
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from p2r_sandbox import SandboxLimits, SandboxUnavailable, run_python_sandbox, validate_output_files
from pydantic import BaseModel, ConfigDict, Field, ValidationError

FIXTURE_RECIPE_ID = "offline_fixture"
FIXTURE_RECIPE_VERSION = "1"
REAL_RECIPE_ID = "real_cpu_baseline"
REAL_RECIPE_VERSION = "1"
REAL_RUNNER_ID = "local-cpu-real-v1"
SANDBOX_RECIPE_ID = "sandbox_python"
SANDBOX_RECIPE_VERSION = "1"
SANDBOX_RUNNER_ID = "local-sandbox-v1"
SANDBOX_EVALUATOR_VERSION = "sandbox-python-evaluator-v1"

RECIPE_REGISTRY: dict[str, tuple[str, ...]] = {
    FIXTURE_RECIPE_ID: (FIXTURE_RECIPE_VERSION,),
    REAL_RECIPE_ID: (REAL_RECIPE_VERSION,),
    SANDBOX_RECIPE_ID: (SANDBOX_RECIPE_VERSION,),
}
LOCAL_CPU_TARGET = "local-cpu"
FIXTURE_RUNNER_ID = "local-fixture-v1"
EVIDENCE_CLASS_CONTROLLED_SMOKE = "controlled_smoke"
EVIDENCE_CLASS_REAL_LOCAL_CPU = "real_local_cpu"
EVIDENCE_CLASS_SANDBOXED_MODEL_CODE = "sandboxed_model_code"
REAL_EVALUATOR_VERSION = "real-cpu-baseline-evaluator-v1"
REAL_RECIPE_MAX_SECONDS = 60
DATASET_SHA256 = "607d8d8c0cf7c62ebc026554fc4f5b6e7c13f0dc934a1239c4f5e72671349854"
# Raw guest output never passes through in full: failure reasons and the
# success summary both stop at this many bytes of decoded stdout/stderr.
SANDBOX_STDOUT_SUMMARY_BYTES = 2048
#: Model-generated code only runs under a backend that verifiably denies the
#: network AND writes outside the workdir (fail closed). Setting this env var
#: to 1/true/yes is an explicit operator opt-out for hosts without such a
#: backend; the run then records ``isolation_required: false``.
ALLOW_UNISOLATED_ENV = "P2R_ALLOW_UNISOLATED_SANDBOX"


def sandbox_isolation_required() -> bool:
    return os.environ.get(ALLOW_UNISOLATED_ENV, "").strip().lower() not in {"1", "true", "yes"}


class FixtureRunnerError(ValueError):
    pass


class RecipeTimeout(FixtureRunnerError):
    pass


class RealCpuBaselineParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    seed: int = Field(default=7, ge=0, le=2**31 - 1)
    max_epochs: int = Field(default=60, ge=1, le=200)
    learning_rate: float = Field(default=0.15, ge=1e-4, le=1.0)
    batch_size: int = Field(default=32, ge=8, le=256)


class SandboxPythonParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    seed: int = Field(default=7, ge=0, le=2**31 - 1)
    max_seconds: int = Field(default=60, ge=10, le=180)


def is_complete_sandbox_code_manifest(manifest: Any) -> bool:
    """True when a model attempt's code manifest carries the full codegen contract.

    A complete manifest names the content-addressed artifact and its sha256
    (which must be equal, since the store is content-addressed) plus the three
    generator provenance fields. Anything less cannot identify the exact code
    bytes a sandbox run must execute.
    """
    if not isinstance(manifest, dict):
        return False
    for key in ("code_artifact_id", "code_sha256", "generator_model", "prompt_version", "policy_version"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value:
            return False
    artifact_id = manifest["code_artifact_id"]
    code_sha = manifest["code_sha256"]
    hex64 = re.fullmatch(r"[0-9a-f]{64}", artifact_id) and re.fullmatch(r"[0-9a-f]{64}", code_sha)
    return bool(hex64) and artifact_id == code_sha


def local_target() -> dict[str, str]:
    return {"kind": LOCAL_CPU_TARGET, "runner": FIXTURE_RUNNER_ID}


def is_registered_recipe(recipe_id: str, recipe_version: str) -> bool:
    return isinstance(recipe_id, str) and isinstance(recipe_version, str) and recipe_version in RECIPE_REGISTRY.get(recipe_id, ())


def run_fixture(*, recipe_id: str, recipe_version: str, target: str = LOCAL_CPU_TARGET, parameters: dict[str, Any] | None = None) -> dict[str, object]:
    if not is_registered_recipe(recipe_id, recipe_version) or recipe_id != FIXTURE_RECIPE_ID:
        raise FixtureRunnerError("unknown recipe")
    if target != LOCAL_CPU_TARGET:
        raise FixtureRunnerError("unsupported execution target")
    if parameters:
        raise FixtureRunnerError("the controlled fixture accepts no parameters")
    return {
        "recipe": {"id": recipe_id, "version": recipe_version},
        "target": local_target(),
        "evidence_class": EVIDENCE_CLASS_CONTROLLED_SMOKE,
        "predictions": [
            {"id": "sample-001", "prediction": 0},
            {"id": "sample-002", "prediction": 1},
            {"id": "sample-003", "prediction": 1},
            {"id": "sample-004", "prediction": 0},
        ],
        "stdout": "fixture completed\n",
    }


def _real_target() -> dict[str, str]:
    return {"kind": LOCAL_CPU_TARGET, "runner": REAL_RUNNER_ID}


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _load_real_dataset() -> tuple[list[dict[str, Any]], str]:
    path = Path(__file__).parent / "data" / "real_cpu_baseline_v1.csv"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FixtureRunnerError("real dataset unavailable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATASET_SHA256:
        raise FixtureRunnerError("real dataset integrity check failed")
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({"id": row["row_id"], "features": [float(row[f"x{i}"]) for i in range(1, 9)], "label": int(row["label"]), "split": row["split"]})
    if len(rows) != 400:
        raise FixtureRunnerError("real dataset row count mismatch")
    return rows, digest


def run_real_cpu_baseline(*, recipe_id: str, recipe_version: str, target: str = LOCAL_CPU_TARGET, parameters: dict[str, Any] | None = None) -> dict[str, object]:
    if not is_registered_recipe(recipe_id, recipe_version) or recipe_id != REAL_RECIPE_ID or recipe_version != REAL_RECIPE_VERSION:
        raise FixtureRunnerError("unknown recipe")
    if target != LOCAL_CPU_TARGET:
        raise FixtureRunnerError("unsupported execution target")
    try:
        normalized = RealCpuBaselineParameters.model_validate(parameters or {})
    except (ValidationError, TypeError) as exc:
        raise FixtureRunnerError("invalid real recipe parameters") from exc
    rows, digest = _load_real_dataset()
    train = [row for row in rows if row["split"] == "train"]
    test = [row for row in rows if row["split"] == "test"]
    means = [sum(row["features"][j] for row in train) / len(train) for j in range(8)]
    variances = [sum((row["features"][j] - means[j]) ** 2 for row in train) / len(train) for j in range(8)]
    scales = [math.sqrt(value) or 1.0 for value in variances]

    def vector(row):
        return [(value - means[j]) / scales[j] for j, value in enumerate(row["features"])]

    weights = [0.0] * 8
    bias = 0.0
    rng = random.Random(normalized.seed)
    indices = list(range(len(train)))
    log: list[dict[str, Any]] = []
    started = time.monotonic()
    for epoch in range(1, normalized.max_epochs + 1):
        # >= so a zero budget trips even when the clock has not ticked yet
        # (Windows monotonic resolution is ~15.6 ms).
        if time.monotonic() - started >= REAL_RECIPE_MAX_SECONDS:
            raise RecipeTimeout("real recipe exceeded time budget")
        rng.shuffle(indices)
        for start in range(0, len(indices), normalized.batch_size):
            batch = indices[start:start + normalized.batch_size]
            grad_w = [0.0] * 8
            grad_b = 0.0
            for index in batch:
                row = train[index]
                values = vector(row)
                probability = _sigmoid(sum(weight * value for weight, value in zip(weights, values)) + bias)
                error = probability - row["label"]
                for j, value in enumerate(values):
                    grad_w[j] += error * value
                grad_b += error
            rate = normalized.learning_rate / len(batch)
            for j in range(8):
                weights[j] -= rate * grad_w[j]
            bias -= rate * grad_b
        loss = 0.0
        correct = 0
        for row in train:
            probability = _sigmoid(sum(weight * value for weight, value in zip(weights, vector(row))) + bias)
            loss -= row["label"] * math.log(max(probability, 1e-15)) + (1 - row["label"]) * math.log(max(1 - probability, 1e-15))
            correct += int((probability >= 0.5) == bool(row["label"]))
        log.append({"epoch": epoch, "train_loss": float(loss / len(train)), "train_accuracy": float(correct / len(train))})
        if len(log) > 200:
            log.pop(0)

    predictions = []
    labels = {}
    for row in test:
        probability = _sigmoid(sum(weight * value for weight, value in zip(weights, vector(row))) + bias)
        predictions.append({"id": row["id"], "prediction": int(probability >= 0.5)})
        labels[row["id"]] = row["label"]
    return {
        "recipe": {"id": recipe_id, "version": recipe_version}, "target": _real_target(),
        "evidence_class": EVIDENCE_CLASS_REAL_LOCAL_CPU,
        "dataset": {"id": "real_cpu_baseline_v1", "sha256": digest, "rows": len(rows), "train_rows": len(train), "test_rows": len(test)},
        "parameters": normalized.model_dump(), "predictions": predictions, "labels": labels,
        "training_log": log, "evaluator_version": REAL_EVALUATOR_VERSION,
        "stdout": "trained logistic regression on real_cpu_baseline_v1 (pure-python cpu)\n",
    }


def reject_command(*_args, **_kwargs):
    raise FixtureRunnerError("shell and argv are not accepted")


def _sandbox_target() -> dict[str, str]:
    return {"kind": LOCAL_CPU_TARGET, "runner": SANDBOX_RUNNER_ID}


def _sandbox_stream_summary(raw: bytes, *, limit: int = SANDBOX_STDOUT_SUMMARY_BYTES) -> str:
    """Decode guest output to a bounded, single-line-safe summary."""
    text = raw[:limit].decode("utf-8", errors="replace")
    if len(raw) > limit:
        text += f"…[truncated at {limit} bytes]"
    return text


def run_sandbox_python(
    *,
    recipe_id: str,
    recipe_version: str,
    target: str = LOCAL_CPU_TARGET,
    parameters: dict[str, Any] | None = None,
    code: str = "",
    artifact_store: Any = None,
    attempt_manifest: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Execute model-generated code under the host sandbox and collect its outputs.

    The code is never trusted: it runs through p2r_sandbox's isolation stack
    (separate process, whitelisted environment, resource limits, wall-clock
    timeout, seatbelt network/write denial where the host provides it), and the
    only accepted outputs are the shape-validated predictions/labels files it
    wrote inside its throwaway workdir. Any visible degradation — non-zero
    exit, timeout, truncated output, missing/malformed files — fails the run
    instead of being scored as a result.
    """
    if not is_registered_recipe(recipe_id, recipe_version) or recipe_id != SANDBOX_RECIPE_ID or recipe_version != SANDBOX_RECIPE_VERSION:
        raise FixtureRunnerError("unknown recipe")
    if target != LOCAL_CPU_TARGET:
        raise FixtureRunnerError("unsupported execution target")
    try:
        normalized = SandboxPythonParameters.model_validate(parameters or {})
    except (ValidationError, TypeError) as exc:
        raise FixtureRunnerError("invalid sandbox recipe parameters") from exc
    if not isinstance(code, str) or not code:
        raise FixtureRunnerError("sandbox recipe requires non-empty model code")
    code_bytes = code.encode("utf-8")
    code_sha256 = hashlib.sha256(code_bytes).hexdigest()
    if attempt_manifest is not None:
        manifest_sha = attempt_manifest.get("code_sha256") if isinstance(attempt_manifest, dict) else None
        if manifest_sha != code_sha256:
            raise FixtureRunnerError("code sha256 does not match the attempt manifest")

    limits = SandboxLimits(max_seconds=normalized.max_seconds)
    isolation_required = sandbox_isolation_required()
    try:
        outcome = run_python_sandbox(code, limits=limits, require_isolation=isolation_required)
    except FixtureRunnerError:
        raise
    except SandboxUnavailable as exc:
        hint = (f" (no isolating sandbox backend; set {ALLOW_UNISOLATED_ENV}=1 to run "
                "model code without isolation at your own risk)") if isolation_required else ""
        raise FixtureRunnerError(f"sandbox execution unavailable: {exc}{hint}") from exc
    except Exception as exc:
        raise FixtureRunnerError(f"sandbox execution unavailable: {type(exc).__name__}") from exc

    stdout_summary = _sandbox_stream_summary(outcome.stdout)
    stderr_summary = _sandbox_stream_summary(outcome.stderr)
    if outcome.timed_out:
        raise FixtureRunnerError(
            f"sandboxed model code exceeded the {normalized.max_seconds}s wall-clock limit; stderr: {stderr_summary}")
    if outcome.truncated:
        raise FixtureRunnerError(f"sandboxed model code output was truncated; stderr: {stderr_summary}")
    if outcome.exit_code != 0:
        raise FixtureRunnerError(
            f"sandboxed model code exited with code {outcome.exit_code}; stderr: {stderr_summary}")
    try:
        predictions, labels = validate_output_files(outcome.files)
    except Exception as exc:
        raise FixtureRunnerError(f"sandboxed model code produced invalid output files: {exc}") from exc
    return {
        "recipe": {"id": recipe_id, "version": recipe_version},
        "target": _sandbox_target(),
        "evidence_class": EVIDENCE_CLASS_SANDBOXED_MODEL_CODE,
        "parameters": normalized.model_dump(),
        "predictions": predictions,
        "labels": labels,
        "sandbox": {
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "truncated": outcome.truncated,
            "network_isolated": outcome.network_isolated,
            "backend": outcome.backend,
            "isolation_required": isolation_required,
            "duration_ms": outcome.duration_ms,
            "code_sha256": code_sha256,
            "limits": {"max_seconds": limits.max_seconds, "max_output_bytes": limits.max_output_bytes,
                       "max_memory_mb": limits.max_memory_mb, "max_cpu_seconds": limits.max_cpu_seconds,
                       "max_processes": limits.max_processes},
        },
        "evaluator_version": SANDBOX_EVALUATOR_VERSION,
        "stdout": stdout_summary,
    }
