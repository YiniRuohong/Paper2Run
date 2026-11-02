"""Layering contract for the Python code (see docs/architecture.md).

A package may import only from strictly lower layers. The sandbox is a
standalone component and must not import any other p2r_* package.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LAYERS = {
    "p2r_contracts": 0,
    "p2r_sandbox": 1, "p2r_docparse": 1, "p2r_evaluation": 1, "p2r_codegen": 1, "p2r_artifacts": 1, "p2r_persistence": 1,
    "p2r_runner": 2, "p2r_kernel": 2,
    "p2r_orchestration": 3,
    "p2r_api": 4,
}
STANDALONE = {"p2r_sandbox", "p2r_docparse"}


def _package_dirs():
    for base in ("packages", "services"):
        for root in sorted((ROOT / base).iterdir()):
            for pkg in root.glob("p2r_*"):
                if (pkg / "__init__.py").is_file():
                    yield pkg


def _imports(pkg: Path):
    for f in pkg.rglob("*.py"):
        if "alembic" in f.parts:
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"), str(f))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top.startswith("p2r_"):
                    yield f.relative_to(ROOT), top


def test_every_package_has_a_layer():
    found = {p.name for p in _package_dirs()}
    assert found == set(LAYERS), f"unlayered: {found ^ set(LAYERS)}"


def test_imports_only_point_downwards():
    violations = []
    for pkg in _package_dirs():
        for where, target in _imports(pkg):
            if target == pkg.name:
                continue
            if pkg.name in STANDALONE or LAYERS.get(target, 99) >= LAYERS[pkg.name]:
                violations.append(f"{where}: {pkg.name}(L{LAYERS[pkg.name]}) -> {target}(L{LAYERS.get(target)})")
    assert not violations, "\n".join(violations)
