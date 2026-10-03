from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

EVALUATOR_VERSION = "offline-fixture-evaluator-v1"
FIXTURE_LABELS: dict[str, int] = {"sample-001": 0, "sample-002": 1, "sample-003": 1, "sample-004": 0}

class PredictionValidationError(ValueError):
    pass

def labels_fixture() -> dict[str, int]:
    return dict(FIXTURE_LABELS)

def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()

def evaluate_predictions(predictions: Iterable[Mapping[str, Any]], labels: Mapping[str, int] | None = None) -> dict[str, Any]:
    expected = dict(FIXTURE_LABELS if labels is None else labels)
    normalized: dict[str, int] = {}
    for item in predictions:
        if not isinstance(item, Mapping) or set(item) != {"id", "prediction"}:
            raise PredictionValidationError("prediction must contain exactly id and prediction")
        sample_id, prediction = item["id"], item["prediction"]
        if not isinstance(sample_id, str) or not sample_id:
            raise PredictionValidationError("prediction id must be a non-empty string")
        if sample_id in normalized:
            raise PredictionValidationError(f"duplicate prediction id: {sample_id}")
        if sample_id not in expected:
            raise PredictionValidationError(f"unknown prediction id: {sample_id}")
        if isinstance(prediction, bool) or not isinstance(prediction, int):
            raise PredictionValidationError("prediction must be a finite integer class")
        if not math.isfinite(float(prediction)):
            raise PredictionValidationError("prediction must be finite")
        normalized[sample_id] = prediction
    missing = sorted(set(expected) - set(normalized))
    if missing:
        raise PredictionValidationError(f"missing prediction ids: {','.join(missing)}")
    correct = sum(normalized[key] == expected[key] for key in expected)
    metrics = {"accuracy": correct / len(expected), "correct": correct, "total": len(expected)}
    return {"metrics": metrics, "metrics_hash": hashlib.sha256(_canonical(metrics)).hexdigest(), "verdict": "matched"}
