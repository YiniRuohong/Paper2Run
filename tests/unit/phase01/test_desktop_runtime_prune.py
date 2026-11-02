"""The bundled desktop runtime keeps pip so MinerU can be installed on demand.

``p2r_docparse.install`` probes ``<runtime python> -m pip --version`` and then
drives a ``--without-pip`` venv from outside with ``-m pip --python``; that
only works if the build keeps an importable pip (module + dist-info) while
still dropping ``ensurepip``.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "desktop"


def _load(name: str):
    """Import a script by path (scripts/ is not a package); import has no side effects."""
    module_name = f"_p2r_desktop_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves string annotations through sys.modules[cls.__module__].
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


build_runtime = _load("build_runtime")
smoke_runtime = _load("smoke_runtime")


def test_pip_not_pruned():
    assert "pip" not in build_runtime.PRUNE_SITE
    assert "ensurepip" in build_runtime.PRUNE_STDLIB


def _touch(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_prune_keeps_an_importable_pip(tmp_path):
    target = build_runtime.TARGETS["darwin-arm64"]
    python_dir = tmp_path / "python"
    paths = build_runtime.layout(python_dir, target)
    lib, site = paths["lib"], paths["site"]
    _touch(paths["exe"])
    _touch(python_dir / "bin" / "pip3")
    _touch(lib / "ensurepip" / "__init__.py")
    _touch(lib / "venv" / "__init__.py")
    (lib / "lib-dynload").mkdir(parents=True)
    _touch(site / "pip" / "__init__.py", '__version__ = "26.2.2"\n')
    _touch(site / "pip" / "__main__.py")
    _touch(site / "pip" / "__pycache__" / "__init__.cpython-312.pyc")
    _touch(site / "pip" / "_vendor" / "certifi" / "tests" / "__init__.py")
    _touch(site / "pip-26.2.2.dist-info" / "METADATA", "Name: pip\nVersion: 26.2.2\n")
    _touch(site / "setuptools" / "__init__.py")
    _touch(site / "setuptools-80.0.0.dist-info" / "METADATA", "Name: setuptools\n")
    _touch(site / "thirdparty" / "tests" / "test_x.py")

    build_runtime.prune(python_dir, paths, target)

    # pip ships intact: module, entry point, metadata, vendored tree.
    assert (site / "pip" / "__init__.py").is_file()
    assert (site / "pip" / "__main__.py").is_file()
    assert (site / "pip-26.2.2.dist-info" / "METADATA").is_file()
    assert (site / "pip" / "_vendor" / "certifi" / "tests" / "__init__.py").is_file()
    assert (lib / "venv" / "__init__.py").is_file()
    # Everything else is still pruned as before.
    assert not (lib / "ensurepip").exists()
    assert not (site / "setuptools").exists()
    assert not (site / "setuptools-80.0.0.dist-info").exists()
    assert not (site / "thirdparty" / "tests").exists()
    assert not (site / "pip" / "__pycache__").exists()  # recompiled later
    assert not (python_dir / "bin" / "pip3").exists()  # host-shebang script; use -m pip
    assert paths["exe"].is_file()


@pytest.mark.parametrize(("stdout", "expected"), [
    ("pip 26.2.2 from /rt/lib/python3.12/site-packages/pip (python 3.12)\n", (26, 2)),
    ("pip 22.3 from C:\\rt\\Lib\\site-packages\\pip (python 3.12)", (22, 3)),
    ("pip 22.2.2 from /x (python 3.12)", (22, 2)),
    ("No module named pip", None),
    ("", None),
])
def test_parse_pip_version(stdout, expected):
    assert smoke_runtime.parse_pip_version(stdout) == expected


def test_minimum_pip_supports_python_flag():
    # ``pip --python <venv python>`` (used by p2r_docparse.install) needs pip >= 22.3.
    assert smoke_runtime.PIP_MIN == (22, 3)


def _has_pip(python: str) -> bool:
    return subprocess.run([python, "-I", "-m", "pip", "--version"], capture_output=True).returncode == 0


@pytest.mark.skipif(_has_pip(sys.executable), reason="needs an interpreter without pip")
def test_check_pip_fails_clearly_without_pip(tmp_path):
    failure = smoke_runtime.check_pip(sys.executable, str(tmp_path))
    assert failure is not None and "pip" in failure
