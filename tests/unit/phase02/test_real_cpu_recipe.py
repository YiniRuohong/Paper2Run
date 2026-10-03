"""Unit boundaries of the real local-CPU recipe (real_cpu_baseline/1).

The recipe is the first non-fixture execution path: it trains a real pure
stdlib logistic regression on a frozen repository dataset, is deterministic
for a given seed, refuses every out-of-schema parameter, and is bounded in
wall-clock time. Its output must never be confusable with the controlled
smoke fixture.
"""
from __future__ import annotations

import csv
import pytest
from pathlib import Path

import p2r_runner
from p2r_evaluation import evaluate_predictions
from p2r_runner import (
    DATASET_SHA256,
    EVIDENCE_CLASS_REAL_LOCAL_CPU,
    REAL_EVALUATOR_VERSION,
    REAL_RECIPE_ID,
    REAL_RECIPE_MAX_SECONDS,
    REAL_RECIPE_VERSION,
    REAL_RUNNER_ID,
    RECIPE_REGISTRY,
    FixtureRunnerError,
    RecipeTimeout,
    is_registered_recipe,
    run_fixture,
    run_real_cpu_baseline,
)

DATASET_PATH = Path(p2r_runner.__file__).parent / "data" / "real_cpu_baseline_v1.csv"


def run(parameters=None, target="local-cpu"):
    return run_real_cpu_baseline(
        recipe_id=REAL_RECIPE_ID, recipe_version=REAL_RECIPE_VERSION,
        target=target, parameters=parameters,
    )


def csv_test_labels() -> dict[str, int]:
    with DATASET_PATH.open(newline="", encoding="utf-8") as handle:
        return {
            row["row_id"]: int(row["label"])
            for row in csv.DictReader(handle)
            if row["split"] == "test"
        }


def test_real_recipe_is_registered_with_a_single_version():
    assert REAL_RECIPE_ID == "real_cpu_baseline"
    assert REAL_RECIPE_VERSION == "1"
    assert RECIPE_REGISTRY[REAL_RECIPE_ID] == (REAL_RECIPE_VERSION,)
    assert is_registered_recipe(REAL_RECIPE_ID, REAL_RECIPE_VERSION) is True
    assert is_registered_recipe(REAL_RECIPE_ID, "2") is False
    # The fixture stays registered and the fixture entry point still refuses
    # the real recipe: the two evidence classes never mix.
    assert is_registered_recipe("offline_fixture", "1") is True
    with pytest.raises(FixtureRunnerError):
        run_fixture(recipe_id=REAL_RECIPE_ID, recipe_version=REAL_RECIPE_VERSION)


@pytest.mark.parametrize("parameters", [
    {"seed": -1},
    {"seed": 2 ** 31},
    {"seed": "abc"},
    {"max_epochs": 0},
    {"max_epochs": 201},
    {"learning_rate": 1e-5},
    {"learning_rate": 1.1},
    {"batch_size": 7},
    {"batch_size": 257},
    {"seed": 7, "unknown_key": 1},
    [1, 2, 3],
])
def test_out_of_schema_parameters_are_rejected(parameters):
    with pytest.raises(FixtureRunnerError):
        run(parameters)


def test_unknown_recipe_version_or_target_are_rejected():
    with pytest.raises(FixtureRunnerError):
        run_real_cpu_baseline(recipe_id="no_such_recipe", recipe_version="1")
    with pytest.raises(FixtureRunnerError):
        run_real_cpu_baseline(recipe_id=REAL_RECIPE_ID, recipe_version="2")
    with pytest.raises(FixtureRunnerError):
        run(target="ssh")
    with pytest.raises(FixtureRunnerError):
        run(target="local-gpu")


def test_keyword_only_signature_cannot_express_shell_or_argv():
    with pytest.raises(TypeError):
        run_real_cpu_baseline(REAL_RECIPE_ID, REAL_RECIPE_VERSION)  # type: ignore[misc]
    with pytest.raises(TypeError):
        run_real_cpu_baseline(recipe_id=REAL_RECIPE_ID, recipe_version=REAL_RECIPE_VERSION,
                              shell="/bin/sh")
    with pytest.raises(TypeError):
        run_real_cpu_baseline(recipe_id=REAL_RECIPE_ID, recipe_version=REAL_RECIPE_VERSION,
                              argv=["ls", "-la"])
    with pytest.raises(TypeError):
        run_real_cpu_baseline(recipe_id=REAL_RECIPE_ID, recipe_version=REAL_RECIPE_VERSION,
                              workdir="/tmp")


def test_same_seed_is_deterministic_predictions_and_metrics_hash():
    first, second = run(), run()
    assert first["predictions"] == second["predictions"]
    assert first["training_log"] == second["training_log"]
    evaluated_first = evaluate_predictions(first["predictions"], labels=first["labels"])
    evaluated_second = evaluate_predictions(second["predictions"], labels=second["labels"])
    assert evaluated_first["metrics_hash"] == evaluated_second["metrics_hash"]
    assert evaluated_first["metrics"] == evaluated_second["metrics"]


def test_dataset_integrity_failure_when_pinned_hash_does_not_match(monkeypatch):
    monkeypatch.setattr(p2r_runner, "DATASET_SHA256", "0" * 64)
    with pytest.raises(FixtureRunnerError, match="integrity"):
        run()


def test_zero_time_budget_raises_recipe_timeout(monkeypatch):
    assert REAL_RECIPE_MAX_SECONDS == 60
    monkeypatch.setattr(p2r_runner, "REAL_RECIPE_MAX_SECONDS", 0)
    with pytest.raises(RecipeTimeout):
        run(parameters={"max_epochs": 1})


def test_timeout_is_a_runner_error_subclass():
    assert issubclass(RecipeTimeout, FixtureRunnerError)


def test_output_structure_marks_real_local_cpu_evidence():
    result = run()
    assert set(result) == {
        "recipe", "target", "evidence_class", "dataset", "parameters",
        "predictions", "labels", "training_log", "evaluator_version", "stdout",
    }
    assert result["recipe"] == {"id": REAL_RECIPE_ID, "version": REAL_RECIPE_VERSION}
    assert result["target"] == {"kind": "local-cpu", "runner": REAL_RUNNER_ID}
    assert result["evidence_class"] == EVIDENCE_CLASS_REAL_LOCAL_CPU == "real_local_cpu"
    assert result["evaluator_version"] == REAL_EVALUATOR_VERSION
    assert result["parameters"] == {"seed": 7, "max_epochs": 60, "learning_rate": 0.15, "batch_size": 32}
    assert result["dataset"] == {
        "id": "real_cpu_baseline_v1", "sha256": DATASET_SHA256,
        "rows": 400, "train_rows": 320, "test_rows": 80,
    }
    assert isinstance(result["stdout"], str) and 0 < len(result["stdout"]) <= 200


def test_predictions_are_test_rows_only_and_labels_match_the_csv():
    result = run()
    ground_truth = csv_test_labels()
    assert len(result["predictions"]) == len(result["labels"]) == 80
    assert set(result["labels"]) == set(ground_truth)
    assert result["labels"] == ground_truth
    for item in result["predictions"]:
        assert set(item) == {"id", "prediction"}
        assert type(item["prediction"]) is int
        assert item["prediction"] in (0, 1)
        assert item["id"] in ground_truth
    assert sum(result["labels"].values()) == 40


def test_training_log_is_bounded_and_well_formed():
    result = run(parameters={"max_epochs": 200})
    log = result["training_log"]
    assert len(log) <= 200
    assert len(log) == 200
    assert [entry["epoch"] for entry in log] == list(range(1, 201))
    for entry in log:
        assert set(entry) == {"epoch", "train_loss", "train_accuracy"}
        assert 0.0 <= entry["train_accuracy"] <= 1.0
        assert entry["train_loss"] > 0.0


def test_real_accuracy_is_in_the_designed_band_and_not_perfect():
    result = run()
    evaluated = evaluate_predictions(result["predictions"], labels=result["labels"])
    accuracy = evaluated["metrics"]["accuracy"]
    # The frozen dataset is built so the classes overlap: a real training run
    # lands in the mid-80s to low-90s, never at perfect separation.
    assert 0.85 <= accuracy <= 0.90
    assert evaluated["metrics"]["total"] == 80
    assert evaluated["verdict"] == "matched"
