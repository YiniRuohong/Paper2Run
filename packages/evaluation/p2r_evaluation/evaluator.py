from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any, Iterable, Mapping

EVALUATOR_VERSION = "offline-fixture-evaluator-v1"
FIXTURE_LABELS: dict[str, int] = {"sample-001": 0, "sample-002": 1, "sample-003": 1, "sample-004": 0}

class PredictionValidationError(ValueError):
    pass

def _accuracy_ci(correctness: list[float], *, replicates: int = 2000, seed: int = 20261104) -> dict[str, float]:
    rng = random.Random(seed)
    size = len(correctness)
    means = sorted(sum(correctness[rng.randrange(size)] for _ in range(size)) / size
                   for _ in range(replicates))
    return {"lower": means[int(0.025 * (replicates - 1))],
            "upper": means[int(0.975 * (replicates - 1))]}

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
    correctness = [float(normalized[key] == expected[key]) for key in sorted(expected)]
    metrics = {"accuracy": correct / len(expected), "correct": correct, "total": len(expected)}
    confidence_intervals = {"accuracy_95": _accuracy_ci(correctness)}
    return {"metrics": metrics, "confidence_intervals": confidence_intervals,
            "metrics_hash": hashlib.sha256(_canonical(metrics)).hexdigest(), "verdict": "matched"}
