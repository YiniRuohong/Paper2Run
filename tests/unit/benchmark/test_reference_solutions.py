"""Reference solutions must pass the production AST policy gate."""
from __future__ import annotations

import pytest
from p2r_orchestration.codegen_service import validate_generated_code

import run_benchmark

PAPERS_DIR = run_benchmark.DEFAULT_PAPERS_DIR
TASK_IDS = tuple(path.name for path in run_benchmark.list_paper_dirs(PAPERS_DIR))


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_solution_passes_validate_generated_code(task_id):
    code = (PAPERS_DIR / task_id / "reference_solution.py").read_text(encoding="utf-8")
    # Must not raise: same gate real model-generated code has to clear.
    validate_generated_code(code)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_solution_writes_required_output_files(task_id):
    code = (PAPERS_DIR / task_id / "reference_solution.py").read_text(encoding="utf-8")
    for required in ("predictions.json", "labels.json", "metrics.json"):
        assert required in code, f"{task_id} must write {required}"
    for forbidden in ("eval(", "exec(", "compile(", "__import__", "globals("):
        assert forbidden not in code, f"{task_id} must not use {forbidden}"
