"""Benchmark task-suite structure: 8 mini-paper tasks, complete and consistent."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_benchmark

PAPERS_DIR = run_benchmark.DEFAULT_PAPERS_DIR
EXPECTED_TASK_IDS = (
    "01-weighted-knn",
    "02-logistic-regression",
    "03-naive-bayes",
    "04-decision-stump",
    "05-kmeans-purity",
    "06-linear-regression-r2",
    "07-perceptron",
    "08-nearest-centroid",
)


def paper_dirs():
    return run_benchmark.list_paper_dirs(PAPERS_DIR)


def test_exactly_eight_expected_task_dirs_exist():
    assert [path.name for path in paper_dirs()] == list(EXPECTED_TASK_IDS)


@pytest.mark.parametrize("task_id", EXPECTED_TASK_IDS)
def test_each_task_has_complete_file_set(task_id):
    task_dir = PAPERS_DIR / task_id
    for name in ("paper.md", "task.json", "reference_solution.py"):
        assert (task_dir / name).is_file(), f"{task_id} missing {name}"


@pytest.mark.parametrize("task_id", EXPECTED_TASK_IDS)
def test_task_json_schema(task_id):
    task = run_benchmark.load_task(PAPERS_DIR / task_id)
    assert task["task_id"] == task_id
    assert task["metric"] in run_benchmark.KNOWN_METRICS
    assert 0.0 <= task["claimed_low"] <= task["claimed_high"] <= 1.0
    assert isinstance(task["seed"], int) and task["seed"] > 0
    assert task["n_train"] > 0 and task["n_test"] > 0
    assert task["title"] and task["method"] and task["difficulty"]
    assert task["eval_scope"] in ("test", "all")


@pytest.mark.parametrize("task_id", EXPECTED_TASK_IDS)
def test_paper_md_matches_task_json_and_line_budget(task_id):
    paper_text = (PAPERS_DIR / task_id / "paper.md").read_text(encoding="utf-8")
    task = run_benchmark.load_task(PAPERS_DIR / task_id)
    assert len(paper_text.splitlines()) <= 40, f"{task_id} paper.md exceeds 40 lines"
    assert f"seed: {task['seed']}" in paper_text
    interval_text = f"[{task['claimed_low']:.2f}, {task['claimed_high']:.2f}]"
    assert interval_text in paper_text, f"{task_id} paper.md must state {interval_text}"
    assert "指标" in paper_text and "难度设计" in paper_text


@pytest.mark.parametrize("task_id", EXPECTED_TASK_IDS)
def test_reference_solution_seed_matches_task_json(task_id):
    code = (PAPERS_DIR / task_id / "reference_solution.py").read_text(encoding="utf-8")
    seed = run_benchmark.load_task(PAPERS_DIR / task_id)["seed"]
    assert f"SEED = {seed}" in code


@pytest.mark.parametrize("task_id", EXPECTED_TASK_IDS)
def test_difficulty_point_is_documented_and_distinct(task_id):
    """Each paper carries exactly one deliberate difficulty design."""
    task = run_benchmark.load_task(PAPERS_DIR / task_id)
    assert len(task["difficulty"]) >= 12
    paper_text = (PAPERS_DIR / task_id / "paper.md").read_text(encoding="utf-8")
    assert "难度设计" in paper_text


def test_metric_coverage_spans_the_required_methods():
    tasks = [run_benchmark.load_task(path) for path in paper_dirs()]
    metrics = {task["metric"] for task in tasks}
    assert metrics == {"accuracy", "purity", "r2"}


def test_load_task_rejects_unknown_metric(tmp_path):
    task_dir = tmp_path / "x-task"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(json.dumps({
        "task_id": "x-task", "title": "t", "metric": "f1", "claimed_low": 0.0,
        "claimed_high": 1.0, "seed": 1, "n_train": 2, "n_test": 2,
        "method": "m", "difficulty": "d",
    }), encoding="utf-8")
    with pytest.raises(run_benchmark.BenchmarkError, match="unknown metric"):
        run_benchmark.load_task(task_dir)


def test_resolve_papers_rejects_unknown_name():
    with pytest.raises(run_benchmark.BenchmarkError, match="unknown paper task"):
        run_benchmark.resolve_papers("01-weighted-knn,not-a-paper", PAPERS_DIR)
