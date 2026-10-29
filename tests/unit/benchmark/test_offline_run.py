"""Offline benchmark end-to-end: sandbox runs, metrics, schema, determinism."""
from __future__ import annotations

import pytest

import run_benchmark

PAPERS_DIR = run_benchmark.DEFAULT_PAPERS_DIR
TASK_IDS = tuple(path.name for path in run_benchmark.list_paper_dirs(PAPERS_DIR))
RESULT_KEYS = {
    "task_id", "paper_dir", "metric", "claimed_low", "claimed_high", "seed",
    "metric_value", "predictions_sha256", "passed", "stages",
}
STAGE_KEYS = {"status", "duration_ms", "error"}
SUMMARY_KEYS = {"total", "passed", "failed", "pass_rate", "stage_pass_rates"}
PAYLOAD_KEYS = {"schema_version", "mode", "papers_dir", "results", "summary"}


@pytest.fixture(scope="module")
def offline_payload():
    return run_benchmark.run_offline_benchmark(papers_dir=PAPERS_DIR)


def test_offline_benchmark_all_papers_pass(offline_payload):
    assert offline_payload["mode"] == "offline"
    assert [r["task_id"] for r in offline_payload["results"]] == list(TASK_IDS)
    assert offline_payload["summary"]["failed"] == 0
    assert offline_payload["summary"]["pass_rate"] == 1.0
    for result in offline_payload["results"]:
        assert result["passed"] is True, (result["task_id"], result["stages"])
        assert result["metric_value"] is not None
        assert result["claimed_low"] <= result["metric_value"] <= result["claimed_high"]


def test_offline_stage_pass_rates_all_reach_one(offline_payload):
    assert offline_payload["summary"]["stage_pass_rates"] == {
        stage: 1.0 for stage in run_benchmark.OFFLINE_STAGES
    }


def test_results_json_schema_is_stable(offline_payload):
    assert set(offline_payload) == PAYLOAD_KEYS
    assert offline_payload["schema_version"] == run_benchmark.SCHEMA_VERSION
    assert set(offline_payload["summary"]) == SUMMARY_KEYS
    for result in offline_payload["results"]:
        assert set(result) == RESULT_KEYS
        assert set(result["stages"]) == set(run_benchmark.OFFLINE_STAGES)
        for stage in result["stages"].values():
            assert set(stage) == STAGE_KEYS
            assert stage["status"] in ("pass", "fail", "skipped")
            assert isinstance(stage["duration_ms"], int)
            assert stage["error"] is None or isinstance(stage["error"], str)


def test_same_seed_reruns_are_byte_identical():
    """Same seed => identical metrics and identical predictions bytes."""
    first = run_benchmark.run_offline_benchmark(papers_dir=PAPERS_DIR)
    second = run_benchmark.run_offline_benchmark(papers_dir=PAPERS_DIR)
    for left, right in zip(first["results"], second["results"]):
        assert left["task_id"] == right["task_id"]
        assert left["metric_value"] == right["metric_value"], left["task_id"]
        assert (
            left["predictions_sha256"] == right["predictions_sha256"]
        ), left["task_id"]


def test_subset_selection_runs_only_requested_papers():
    payload = run_benchmark.run_offline_benchmark(
        "01-weighted-knn,04-decision-stump", papers_dir=PAPERS_DIR
    )
    assert [r["task_id"] for r in payload["results"]] == [
        "01-weighted-knn", "04-decision-stump",
    ]
    assert payload["summary"]["total"] == 2


def test_parse_outputs_rejects_tampered_metrics_file():
    files = {
        "predictions.json": b'[{"id": "a", "prediction": 1}]',
        "labels.json": b'{"a": 1}',
        "metrics.json": b'{"metric": "f1", "value": 0.5}',
    }
    with pytest.raises(run_benchmark.BenchmarkError, match="metrics.json"):
        run_benchmark.parse_outputs(files, "accuracy")


def test_parse_outputs_rejects_missing_files():
    with pytest.raises(run_benchmark.BenchmarkError, match="missing sandbox output"):
        run_benchmark.parse_outputs({}, "accuracy")


def test_compute_metric_accuracy_matches_trusted_evaluator():
    predictions = [{"id": "a", "prediction": 1}, {"id": "b", "prediction": 0}]
    labels = {"a": 1, "b": 1}
    assert run_benchmark.compute_metric("accuracy", predictions, labels) == 0.5
    with pytest.raises(ValueError):
        run_benchmark.compute_metric("accuracy", predictions, {"a": 1})  # missing id


def test_compute_metric_r2_perfect_fit_is_one():
    predictions = [{"id": "a", "prediction": 1.5}, {"id": "b", "prediction": 2.5}]
    labels = {"a": 1.5, "b": 2.5}
    assert run_benchmark.compute_metric("r2", predictions, labels) == 1.0


def test_compute_metric_purity_counts_dominant_class():
    # cluster 0 holds {a: class 1}; cluster 1 holds {b: class 0, c: class 1}
    # -> dominant counts 1 + 1 = 2 of 3 rows.
    predictions = [{"id": "a", "prediction": 0}, {"id": "b", "prediction": 1},
                   {"id": "c", "prediction": 1}]
    labels = {"a": 1, "b": 0, "c": 1}
    assert run_benchmark.compute_metric("purity", predictions, labels) == pytest.approx(2 / 3)
