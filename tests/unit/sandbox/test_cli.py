"""CLI behaviour and standalone-package contract tests.

``python -m p2r_sandbox`` must work with ONLY ``packages/sandbox`` on the
path (no other repository package importable), expose stable ``--json``
output, and the package metadata must promise zero runtime dependencies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from p2r_sandbox.__main__ import build_parser, main

REPO_ROOT = Path(__file__).resolve().parents[3]
SANDBOX_PKG_ROOT = REPO_ROOT / "packages" / "sandbox"

VALID_BACKENDS = {"seatbelt", "bwrap", "unshare", "none"}


# ---------------------------------------------------------------------------
# in-process CLI
# ---------------------------------------------------------------------------


def test_cli_capabilities_json(capsys):
    assert main(["capabilities", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"seatbelt", "rlimits", "platform_walls"} <= set(data)
    assert all(isinstance(v, bool) for v in data.values())


def test_cli_capabilities_human_readable(capsys):
    assert main(["capabilities"]) == 0
    out = capsys.readouterr().out
    assert "seatbelt:" in out


def test_cli_run_json(capsys, tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('cli-hello')\n", encoding="utf-8")
    rc = main(
        [
            "run", str(script),
            "--timeout", "30",
            "--max-memory-mb", "256",
            "--collect", "*.json",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["exit_code"] == 0
    assert data["backend"] in VALID_BACKENDS
    assert isinstance(data["network_isolated"], bool)
    assert data["timed_out"] is False
    assert "cli-hello" in data["stdout"]
    assert data["files"] == []  # no *.json produced by this script
    assert isinstance(data["duration_ms"], int)


def test_cli_run_collects_glob(capsys, tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(
        "open('a.json', 'w').write('{\"x\": 1}')\n"
        "open('b.txt', 'w').write('nope')\n",
        encoding="utf-8",
    )
    rc = main(["run", str(script), "--json", "--collect", "*.json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["files"] == ["a.json"]


def test_cli_run_guest_failure_maps_exit_code(capsys, tmp_path):
    script = tmp_path / "boom.py"
    script.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    rc = main(["run", str(script), "--json"])
    assert rc == 7


def test_cli_run_missing_script_is_usage_error(capsys, tmp_path):
    assert main(["run", str(tmp_path / "nope.py")]) == 2


def test_cli_run_invalid_limits_are_usage_error(capsys, tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('x')\n", encoding="utf-8")
    assert main(["run", str(script), "--timeout", "0"]) == 2
    assert main(["run", str(script), "--max-memory-mb", "-5"]) == 2


def test_cli_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ---------------------------------------------------------------------------
# standalone operation: PYTHONPATH contains ONLY packages/sandbox
# ---------------------------------------------------------------------------


def _standalone_env() -> dict[str, str]:
    assert (SANDBOX_PKG_ROOT / "p2r_sandbox" / "__main__.py").exists()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(SANDBOX_PKG_ROOT),
        # keep HOME: capabilities probes expand ~ on macOS
        "HOME": os.environ.get("HOME", str(Path.home())),
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX env handling")
def test_standalone_capabilities_subprocess():
    proc = subprocess.run(
        [sys.executable, "-m", "p2r_sandbox", "capabilities", "--json"],
        env=_standalone_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "seatbelt" in data


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX env handling")
def test_standalone_run_subprocess(tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('standalone-ok')\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "p2r_sandbox", "run", str(script), "--json"],
        env=_standalone_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["exit_code"] == 0
    assert "standalone-ok" in data["stdout"]
    assert data["backend"] in VALID_BACKENDS


# ---------------------------------------------------------------------------
# package metadata: standalone contract
# ---------------------------------------------------------------------------


def test_pyproject_declares_standalone_contract():
    import tomllib

    pyproject = tomllib.loads(
        (SANDBOX_PKG_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["name"] == "p2r-sandbox"
    assert pyproject["project"]["dependencies"] == []
    assert pyproject["project"]["requires-python"] == ">=3.11"
    found = pyproject["tool"]["setuptools"]["packages"]["find"]
    assert "p2r_sandbox*" in found["include"]


def test_no_imports_of_other_p2r_packages():
    """grep-proof: the sandbox package imports no sibling p2r_* package."""
    pattern = re.compile(r"^\s*(?:from|import)\s+p2r_(?!sandbox)", re.MULTILINE)
    offenders: list[str] = []
    pkg_dir = SANDBOX_PKG_ROOT / "p2r_sandbox"
    for path in sorted(pkg_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{line_no}")
    assert offenders == []
