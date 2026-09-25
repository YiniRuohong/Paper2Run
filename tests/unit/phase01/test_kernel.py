from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

# Kernel is not yet in the distribution's package discovery configuration.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "kernel"))

import pytest
from pydantic import ValidationError

from p2r_kernel import worker
from p2r_kernel.protocol import ExecuteCellRequest
from p2r_kernel.supervisor import KernelSupervisor


def test_basic_stdout_and_result(tmp_path: Path):
    kernel = KernelSupervisor(workspace=tmp_path)
    result = kernel.execute(ExecuteCellRequest(source="print('hello')\n40 + 2"))
    assert result.error_code is None
    assert result.stdout == "hello\n"
    assert "42" in result.result_summary
    assert len(result.namespace_hash) == 64


def test_stderr_is_captured(tmp_path: Path):
    result = KernelSupervisor(workspace=tmp_path).execute(
        ExecuteCellRequest(source="import sys\nprint('bad', file=sys.stderr)")
    )
    assert result.stderr == "bad\n"


def test_timeout_terminates_child(tmp_path: Path):
    kernel = KernelSupervisor(workspace=tmp_path)
    result = kernel.execute(ExecuteCellRequest(source="while True: pass", timeout_seconds=0.2))
    assert result.error_code == "CELL_TIMEOUT"
    assert not mp.active_children()
    started = time.monotonic()
    followup = kernel.execute(ExecuteCellRequest(source="print('alive')"))
    assert time.monotonic() - started < 2
    assert followup.stdout == "alive\n"


def test_source_size_is_rejected():
    with pytest.raises(ValidationError):
        ExecuteCellRequest(source="x" * (256 * 1024 + 1))


@pytest.mark.parametrize("source", ["import subprocess", "import socket", "import os"])
def test_forbidden_imports_are_rejected(tmp_path: Path, source: str):
    result = KernelSupervisor(workspace=tmp_path).execute(ExecuteCellRequest(source=source))
    assert result.error_code == "IMPORT_NOT_ALLOWED"


def test_namespace_hash_changes(tmp_path: Path):
    kernel = KernelSupervisor(workspace=tmp_path)
    first = kernel.execute(ExecuteCellRequest(source="value = 1"))
    second = kernel.execute(ExecuteCellRequest(source="value = 2"))
    assert first.namespace_hash != second.namespace_hash


def test_output_is_bounded(tmp_path: Path):
    result = KernelSupervisor(workspace=tmp_path).execute(
        ExecuteCellRequest(source="print('x' * 300_000)")
    )
    assert len(result.stdout.encode()) <= 256 * 1024
    assert result.error_code == "OUTPUT_LIMIT"


def test_output_quota_cannot_be_changed_through_stdout_facade(tmp_path: Path):
    result = KernelSupervisor(workspace=tmp_path).execute(ExecuteCellRequest(source=(
        "import sys\n"
        "sys.stdout.limit = 1\n"
        "sys.stdout.buffered = None\n"
        "print('x' * 300_000)"
    )))
    assert len(result.stdout.encode()) <= 256 * 1024
    assert result.error_code == "OUTPUT_LIMIT"
