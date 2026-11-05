from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from p2r_evaluation import EVALUATOR_VERSION
from p2r_runner import (
    REAL_EVALUATOR_VERSION, REAL_RECIPE_ID, REAL_RECIPE_VERSION,
    SANDBOX_EVALUATOR_VERSION, SANDBOX_RECIPE_ID, SANDBOX_RECIPE_VERSION,
)

LIMITATIONS = ["local demo", "controlled smoke", "not full reproduction"]
REAL_LIMITATIONS = ["fixed repository dataset", "local CPU baseline", "not full paper reproduction"]
SANDBOX_LIMITATIONS = [
    "model-generated code executed in a local sandbox",
    "stdlib-only, self-contained approximation",
    "not full paper reproduction",
]

_FIXTURE_RECIPE = {"id": "offline_fixture", "version": "1"}
_REAL_RECIPE = {"id": REAL_RECIPE_ID, "version": REAL_RECIPE_VERSION}
_SANDBOX_RECIPE = {"id": SANDBOX_RECIPE_ID, "version": SANDBOX_RECIPE_VERSION}

_EVIDENCE_KEYS = frozenset({"run_id", "sha256", "bytes", "media_type"})
_EVIDENCE_MEDIA_TYPES = frozenset({"application/json", "text/plain"})


def _evidence_entries(value: Any, run_id: str) -> list[dict[str, Any]]:
    """Copy only fully whitelisted registration records; reject everything else."""
    if not isinstance(value, list):
        raise ValueError("REPORT_INPUT_INVALID")
    entries: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict) or frozenset(entry) != _EVIDENCE_KEYS:
            raise ValueError("REPORT_INPUT_INVALID")
        if entry["run_id"] != run_id:
            raise ValueError("REPORT_INPUT_INVALID")
        sha256 = entry["sha256"]
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("REPORT_INPUT_INVALID")
        nbytes = entry["bytes"]
        if type(nbytes) is not int or isinstance(nbytes, bool) or nbytes < 0:
            raise ValueError("REPORT_INPUT_INVALID")
        media_type = entry["media_type"]
        if not isinstance(media_type, str) or media_type not in _EVIDENCE_MEDIA_TYPES:
            raise ValueError("REPORT_INPUT_INVALID")
        entries.append({"run_id": entry["run_id"], "sha256": sha256, "bytes": nbytes, "media_type": media_type})
    return entries


def report_hash(value: Any) -> str:
    """One canonical encoding for both frozen input and public report hashes."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _project(value, allowed):
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise ValueError("REPORT_INPUT_INVALID")
    return {key: value[key] for key in allowed}


def _identifier(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("REPORT_INPUT_INVALID")
    return value


def project_metrics(value):
    metrics = _project(value, ("accuracy", "correct", "total"))
    accuracy, correct, total = (metrics[key] for key in ("accuracy", "correct", "total"))
    if (type(accuracy) not in (int, float) or not math.isfinite(accuracy)
            or type(correct) is not int or type(total) is not int
            or total <= 0 or not 0 <= correct <= total
            or not 0 <= accuracy <= 1 or accuracy != correct / total):
        raise ValueError("REPORT_INPUT_INVALID")
    return metrics


EVALUATION_EVENT_TYPE = "run.evaluation.completed"


def project_confidence_intervals(value):
    """Public evaluator CI contract; never forward arbitrary nested payloads."""
    intervals = _project(value, ("accuracy_95",))
    bounds = _project(intervals["accuracy_95"], ("lower", "upper"))
    lower, upper = bounds["lower"], bounds["upper"]
    if (type(lower) not in (int, float) or type(upper) not in (int, float)
            or not math.isfinite(lower) or not math.isfinite(upper)
            or not 0 <= lower <= upper <= 1):
        raise ValueError("REPORT_INPUT_INVALID")
    return {"accuracy_95": bounds}


def build_report(snapshot: dict) -> dict:
    """Pure controlled-fixture DTO. Arbitrary nested payloads never pass through.

    Evidence entries are accepted only as fully whitelisted registration
    records (run_id/sha256/bytes/media_type) belonging to this Run, as
    registered by the run worker. An empty list stays valid: the ZIP export
    honestly refuses such reports with EXPORT_INCOMPLETE instead of claiming a
    complete reproduction bundle.
    """
    required = {"run_id", "revision_id", "recipe", "metrics", "evaluator_version", "manifest", "evidence"}
    if not isinstance(snapshot, dict) or not required.issubset(snapshot):
        raise ValueError("REPORT_INPUT_INVALID")
    run_id = _identifier(snapshot["run_id"], r"run_[0-9a-f-]{36}")
    revision_id = _identifier(snapshot["revision_id"], r"spr_[0-9a-f-]{36}")
    recipe = _project(snapshot["recipe"], ("id", "version"))
    real_recipe = recipe == _REAL_RECIPE
    sandbox_recipe = recipe == _SANDBOX_RECIPE
    if recipe != _FIXTURE_RECIPE and not real_recipe and not sandbox_recipe:
        raise ValueError("REPORT_INPUT_INVALID")
    metrics = project_metrics(snapshot["metrics"])
    manifest = _project(snapshot["manifest"], ("status", "manifest_hash"))
    if manifest["status"] != "provisional":
        raise ValueError("REPORT_INPUT_INVALID")
    _identifier(manifest["manifest_hash"], r"[0-9a-f]{64}")
    # A controlled-smoke report can only cite the fixture evaluator; a real
    # local-CPU report only the real evaluator; a sandbox report only the
    # sandbox evaluator. Cross pairs are refused rather than silently
    # relabelled.
    evaluator_version = (
        SANDBOX_EVALUATOR_VERSION if sandbox_recipe
        else REAL_EVALUATOR_VERSION if real_recipe
        else EVALUATOR_VERSION
    )
    if snapshot["evaluator_version"] != evaluator_version:
        raise ValueError("REPORT_INPUT_INVALID")
    evidence = _evidence_entries(snapshot["evidence"], run_id)
    metrics_source = (
        "sandboxed_model_code_evaluation" if sandbox_recipe
        else "real_local_cpu_evaluation" if real_recipe
        else "controlled_fixture_evaluation"
    )
    evidence_class = (
        "sandboxed_model_code" if sandbox_recipe
        else "real_local_cpu" if real_recipe
        else "controlled_smoke"
    )
    limitations = (
        SANDBOX_LIMITATIONS if sandbox_recipe
        else REAL_LIMITATIONS if real_recipe
        else LIMITATIONS
    )
    return {
        "schema_version": "guided-report-v1",
        "run_id": run_id,
        "revision_id": revision_id,
        "recipe": recipe,
        "metrics": metrics,
        **({"confidence_intervals": project_confidence_intervals(snapshot["confidence_intervals"])}
           if "confidence_intervals" in snapshot else {}),
        "metrics_source": metrics_source,
        "evidence_class": evidence_class,
        "evaluator_version": evaluator_version,
        "verdict": "not_comparable",
        "manifest": manifest,
        "evidence": evidence,
        "limitations": list(limitations),
    }
