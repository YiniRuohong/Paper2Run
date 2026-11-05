import hashlib
import json
import math
import random
import sys

import pytest

from p2r_contracts.autonomous import canonical_hash, freeze_protocol
from p2r_evaluation.autonomous import bootstrap_mean_ci, evaluate_artifacts, evidence_manifest


def protocol(adapter="accuracy", metric=None):
    return {"objective": "test", "mode": "comparable", "method": "reference", "entrypoint": "train.py",
            "evaluation": {"adapter": adapter, "metric": metric, "reference_value": 1.0, "tolerance": 0.1, "direction": "maximize"},
            "budget": {"wall_time_seconds": 60, "max_model_calls": 10, "max_model_tokens": 1000, "storage_bytes": 1024}}


def artifacts(root, predictions=(1, 0), labels=(1, 1)):
    (root / "predictions.json").write_text(json.dumps([{"id": str(i), "prediction": p} for i, p in enumerate(predictions)]))
    (root / "labels.json").write_text(json.dumps([{"id": str(i), "label": p} for i, p in enumerate(labels)]))


def test_accuracy_recomputed_no_fabricated_comparison(tmp_path):
    artifacts(tmp_path)
    result = evaluate_artifacts(freeze_protocol(protocol()), tmp_path)
    assert result["metrics"]["accuracy"] == 0.5
    assert result["metrics"]["total"] == 2
    assert result["metrics"]["correct"] == 1
    assert result["confidence_intervals"]["accuracy_95"] == {"lower": 0.0, "upper": 1.0}
    assert result["verdict"] == "not_comparable"
    assert not result["comparison_eligible"]
    assert any("provenance" in reason for reason in result["reasons"])
    (tmp_path / "score.json").write_text('{"accuracy": 1.0}')
    assert evaluate_artifacts(freeze_protocol(protocol()), tmp_path)["metrics"]["accuracy"] == 0.5


@pytest.mark.parametrize("metric,expected", [("rmse", 5**0.5), ("mae", 2.0)])
def test_regression(tmp_path, metric, expected):
    artifacts(tmp_path, (1.0, 4.0), (0.0, 1.0))
    result = evaluate_artifacts(freeze_protocol(protocol("regression", metric)), tmp_path)
    assert result["metrics"][metric] == pytest.approx(expected)
    assert result["confidence_intervals"][f"{metric}_95"]["lower"] <= expected
    assert result["confidence_intervals"][f"{metric}_95"]["upper"] >= expected


def test_bootstrap_is_deterministic_and_validates_inputs():
    values = [0.0, 0.5, 1.0]
    assert bootstrap_mean_ci(values) == bootstrap_mean_ci(values)
    with pytest.raises(ValueError, match="nonempty finite"):
        bootstrap_mean_ci([])
    with pytest.raises(ValueError, match="nonempty finite"):
        bootstrap_mean_ci([float("nan")])
    with pytest.raises(ValueError, match="at least 100"):
        bootstrap_mean_ci(values, replicates=99)


@pytest.mark.parametrize("value", [1e308, -1e308, sys.float_info.max, 5e-324])
def test_bootstrap_handles_large_finite_values(value):
    assert bootstrap_mean_ci([value, value]) == {"lower": value, "upper": value}


@pytest.mark.parametrize("value", [10**400, float("inf"), float("-inf"), True, "1"])
def test_bootstrap_invalid_numbers_raise_value_error(value):
    with pytest.raises(ValueError, match="nonempty finite"):
        bootstrap_mean_ci([value])


def test_bootstrap_preserves_ordinary_precision_and_sampling():
    values = [-1.1, 0.2, 0.7, 3.3, 8.1]
    rng = random.Random(20261104)
    means = sorted(math.fsum(values[rng.randrange(len(values))] for _ in values) / len(values)
                   for _ in range(2000))
    assert bootstrap_mean_ci(values) == {"lower": means[49], "upper": means[1949]}
    assert bootstrap_mean_ci([-1e308, 1e308]) == {"lower": -1e308, "upper": 1e308}


@pytest.mark.parametrize("metric", ["mae", "rmse"])
@pytest.mark.parametrize("value", [1e200, 1e308, sys.float_info.max, 1e-200])
def test_regression_confidence_interval_handles_finite_extremes(tmp_path, metric, value):
    artifacts(tmp_path, (value, value), (0.0, 0.0))
    frozen = freeze_protocol(protocol("regression", metric))
    result = evaluate_artifacts(frozen, tmp_path)
    assert result["metrics"][metric] / value == pytest.approx(1.0)
    interval = result["confidence_intervals"][f"{metric}_95"]
    assert interval == {"lower": value, "upper": value}
    json.dumps(result, allow_nan=False)
    manifest = evidence_manifest(frozen, tmp_path)
    assert manifest["evaluation"] == result
    assert manifest["sha256"] == canonical_hash(result)
    assert result["protocol_sha256"] == frozen["sha256"]
    assert result["artifact_hashes"] == {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("predictions.json", "labels.json")}


@pytest.mark.parametrize("metric", ["mae", "rmse"])
def test_mixed_extremes_preserve_small_resampled_endpoint(tmp_path, metric):
    artifacts(tmp_path, (1e308, 1e-200), (0.0, 0.0))
    result = evaluate_artifacts(freeze_protocol(protocol("regression", metric)), tmp_path)
    assert result["confidence_intervals"][f"{metric}_95"] == {
        "lower": 1e-200, "upper": 1e308}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("metric", ["mae", "rmse"])
def test_unrepresentable_residual_raises_value_error(tmp_path, metric):
    artifacts(tmp_path, (sys.float_info.max,), (-sys.float_info.max,))
    with pytest.raises(ValueError, match="metric calculation"):
        evaluate_artifacts(freeze_protocol(protocol("regression", metric)), tmp_path)


@pytest.mark.parametrize("metric", ["mae", "rmse"])
def test_ordinary_regression_preserves_metrics_and_intervals(tmp_path, metric):
    errors = [0.1, 0.2, 0.3, 1.1, 9.9]
    artifacts(tmp_path, errors, [0.0] * len(errors))
    result = evaluate_artifacts(freeze_protocol(protocol("regression", metric)), tmp_path)
    expected_score = (math.hypot(*errors) / math.sqrt(len(errors)) if metric == "rmse"
                      else math.fsum(e / len(errors) for e in errors))
    assert result["metrics"] == {metric: expected_score, "total": len(errors)}
    samples = [e * e for e in errors] if metric == "rmse" else errors
    rng = random.Random(20261104)
    means = sorted(math.fsum(samples[rng.randrange(len(samples))] for _ in samples) / len(samples)
                   for _ in range(2000))
    expected = {"lower": means[49], "upper": means[1949]}
    if metric == "rmse":
        expected = {key: math.sqrt(value) for key, value in expected.items()}
    assert result["confidence_intervals"][f"{metric}_95"] == expected


def test_frozen_evidence_gate_and_tamper(tmp_path):
    artifacts(tmp_path, (1, 1))
    data = protocol()
    data.update(evidence=["paper:p1"], data_sources=["dataset:v1"], split_hashes={"test": "a" * 64},
                provenance={"labels_sha256": hashlib.sha256((tmp_path / "labels.json").read_bytes()).hexdigest(),
                            "labels_independently_frozen": True, "code_sha256": "b" * 64, "environment_sha256": "c" * 64})
    frozen = freeze_protocol(data)
    data["objective"] = "changed"
    assert frozen["protocol"]["objective"] == "test"
    result = evaluate_artifacts(frozen, tmp_path)
    assert result["comparison_eligible"] and result["within_tolerance"]
    assert result["verdict"] == "comparison_available"
    assert evidence_manifest(frozen, tmp_path)["sha256"]
    (tmp_path / "labels.json").write_text("[]")
    with pytest.raises(ValueError, match="labels differ"):
        evaluate_artifacts(frozen, tmp_path)
    frozen["protocol"]["mode"] = "exploratory"
    with pytest.raises(ValueError, match="integrity"):
        evaluate_artifacts(frozen, tmp_path)


@pytest.mark.parametrize("predictions", [
    '[{"id":"0","prediction":NaN}]', '[{"id":"0","prediction":Infinity}]',
    '[{"id":"0","prediction":true}]', '[{"id":"0","prediction":1,"score":1}]',
    '[{"id":"0","prediction":1},{"id":"0","prediction":1}]', '[]',
    '[{"id":"0","prediction":1}]',
])
def test_invalid_artifacts(tmp_path, predictions):
    artifacts(tmp_path)
    (tmp_path / "predictions.json").write_text(predictions)
    with pytest.raises(ValueError):
        evaluate_artifacts(freeze_protocol(protocol()), tmp_path)


def test_parent_symlink_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    artifacts(actual)
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe artifact root"):
        evaluate_artifacts(freeze_protocol(protocol()), tmp_path / "alias")
    with pytest.raises(ValueError, match="unsafe artifact root"):
        evaluate_artifacts(freeze_protocol(protocol()), tmp_path / "alias" / "child")


@pytest.mark.parametrize("replace_after_open", [False, True])
def test_file_replacement_never_follows_symlink(tmp_path, monkeypatch, replace_after_open):
    import os
    import p2r_evaluation.autonomous as evaluator

    artifacts(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('[{"id":"0","prediction":9}]')
    original_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        if path == "predictions.json":
            if replace_after_open:
                fd = original_open(path, flags, *args, **kwargs)
            os.replace(tmp_path / "predictions.json", tmp_path / "original.json")
            (tmp_path / "predictions.json").symlink_to(outside)
            if replace_after_open:
                return fd
        return original_open(path, flags, *args, **kwargs)

    # Keep capability detection independent of the monkeypatched open function.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {racing_open})
    monkeypatch.setattr(evaluator.os, "open", racing_open)
    if replace_after_open:
        result = evaluate_artifacts(freeze_protocol(protocol()), tmp_path)
        assert result["metrics"]["accuracy"] == 0.5
    else:
        with pytest.raises(ValueError, match="unsafe"):
            evaluate_artifacts(freeze_protocol(protocol()), tmp_path)


def test_missing_and_symlink(tmp_path):
    assert evaluate_artifacts(freeze_protocol(protocol()), tmp_path)["verdict"] == "not_comparable"
    (tmp_path / "predictions.json").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="unsafe"):
        evaluate_artifacts(freeze_protocol(protocol()), tmp_path)
