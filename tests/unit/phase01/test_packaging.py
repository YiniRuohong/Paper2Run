from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Top-level single-file modules that live under services/runner and are imported
# by runtime code (p2r_api.routes.runs) and tests respectively. The API server is
# started with `uvicorn p2r_api.main:create_demo_app` and relies on the editable
# install (NOT pytest's pythonpath), so these modules must be declared to
# setuptools as py-modules; otherwise `pip install -e .` silently drops them and
# uvicorn fails with ModuleNotFoundError.
RUNNER_MODULES = ("p2r_runner", "p2r_runner_service")


def _load_setuptools_config() -> dict:
    raw = (PROJECT_ROOT / "pyproject.toml").read_bytes()
    return tomllib.loads(raw.decode())["tool"]["setuptools"]


def test_runner_modules_are_declared_as_py_modules():
    config = _load_setuptools_config()
    declared = tuple(config.get("py-modules", ()))
    for module in RUNNER_MODULES:
        assert module in declared, (
            f"{module} is imported at runtime but not declared in "
            "[tool.setuptools] py-modules; editable installs will not expose it"
        )


def test_runner_modules_map_to_existing_files():
    config = _load_setuptools_config()
    package_dir = config.get("package-dir", {})
    for module in RUNNER_MODULES:
        location = package_dir.get(module)
        assert location, (
            f"{module} has no package-dir mapping; setuptools cannot locate it"
        )
        module_file = PROJECT_ROOT / location / f"{module}.py"
        assert module_file.is_file(), f"declared module file is missing: {module_file}"
