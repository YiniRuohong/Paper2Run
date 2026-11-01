"""Packaging and source-layout invariants.

Every ``p2r_*`` package lives in exactly one source root that is declared in
``pyproject.toml`` (setuptools discovery + pytest pythonpath) and mirrored by
``scripts/launch.py``. Data files shipped inside packages are declared.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())


def test_every_source_root_holds_one_p2r_package():
    roots = _pyproject()["tool"]["setuptools"]["packages"]["find"]["where"]
    for root in roots:
        pkgs = [p for p in (PROJECT_ROOT / root).iterdir() if p.name.startswith("p2r_") and (p / "__init__.py").is_file()]
        assert len(pkgs) == 1, f"{root} should contain exactly one p2r_* package, found {pkgs}"


def test_pytest_and_setuptools_and_launcher_agree_on_source_roots():
    cfg = _pyproject()["tool"]
    find_roots = cfg["setuptools"]["packages"]["find"]["where"]
    assert cfg["pytest"]["ini_options"]["pythonpath"] == find_roots
    launch = ast.parse((PROJECT_ROOT / "scripts" / "launch.py").read_text())
    launcher_roots = next(
        ast.literal_eval(n.value) for n in ast.walk(launch)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "SOURCE_ROOTS" for t in n.targets)
    )
    assert list(launcher_roots) == find_roots


def test_no_single_file_modules_or_legacy_service_dirs():
    cfg = _pyproject()["tool"]["setuptools"]
    assert "py-modules" not in cfg
    assert sorted(p.name for p in (PROJECT_ROOT / "services").iterdir() if p.is_dir()) == ["api"]


def test_runner_dataset_is_packaged():
    data = _pyproject()["tool"]["setuptools"]["package-data"]["p2r_runner"]
    assert "data/*.csv" in data
    assert (PROJECT_ROOT / "packages/runner/p2r_runner/data/real_cpu_baseline_v1.csv").is_file()
