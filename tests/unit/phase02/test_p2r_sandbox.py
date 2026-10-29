"""Unit tests for the p2r_sandbox host-isolation package.

Every test runs real child processes on this host (no mocks): seatbelt,
rlimits, timeouts and output caps are exercised for real. Where behaviour
depends on platform capability, tests assert that results stay consistent
with ``sandbox_capabilities()`` instead of assuming a specific platform.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from p2r_sandbox import (
    MAX_COLLECT_FILE_BYTES,
    SandboxLimits,
    SandboxOutputError,
    run_python_sandbox,
    sandbox_capabilities,
    validate_output_files,
)

# Keys the guest process may legitimately see: our whitelist (PATH, TMPDIR)
# plus constants macOS CoreFoundation injects into every process at startup.
DARWIN_PLATFORM_ENV_KEYS = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
WHITELIST_ENV_KEYS = {"PATH", "TMPDIR"} | (
    DARWIN_PLATFORM_ENV_KEYS if sys.platform == "darwin" else set()
)

SEATBELT_EXPECTED = sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")


def test_sandbox_capabilities_structure():
    caps = sandbox_capabilities()
    # Legacy keys keep their semantics; newer keys are additive only.
    assert {"seatbelt", "rlimits", "platform_walls"} <= set(caps)
    assert "sensitive_read_denied" in caps
    assert all(isinstance(value, bool) for value in caps.values())
    if SEATBELT_EXPECTED:
        # Verified live on this host: seatbelt must actually deny network and
        # outside writes, otherwise it is reported as unavailable.
        assert caps["seatbelt"] is True


def test_normal_script_runs_and_files_collected():
    code = (
        "import json\n"
        "print('hello from sandbox')\n"
        "with open('predictions.json', 'w') as fh:\n"
        "    json.dump([{'id': 's1', 'prediction': 1}], fh)\n"
        "with open('labels.json', 'w') as fh:\n"
        "    json.dump({'s1': 1}, fh)\n"
    )
    result = run_python_sandbox(code)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert b"hello from sandbox" in result.stdout
    assert set(result.files) == {"predictions.json", "labels.json"}
    predictions, labels = validate_output_files(result.files)
    assert predictions == [{"id": "s1", "prediction": 1}]
    assert labels == {"s1": 1}
    assert result.duration_ms >= 0


def test_network_denied_and_network_isolated_matches_capability():
    caps = sandbox_capabilities()
    # The exception is left uncaught: under isolation the connect must fail.
    code = (
        "import socket\n"
        "socket.create_connection(('127.0.0.1', 1), timeout=2)\n"
        "print('CONNECTED-BAD')\n"
    )
    result = run_python_sandbox(code)
    assert result.network_isolated is caps["seatbelt"]
    assert result.exit_code != 0
    assert b"CONNECTED-BAD" not in result.stdout
    if result.network_isolated:
        # seatbelt denials surface as Operation not permitted (EPERM/EACCES)
        assert b"Operation not permitted" in result.stderr


def test_timeout_kills_process_group():
    code = "import time\ntime.sleep(30)\nprint('slept')\n"
    result = run_python_sandbox(code, limits=SandboxLimits(max_seconds=2))
    assert result.timed_out is True
    assert result.exit_code != 0
    assert b"slept" not in result.stdout
    assert result.duration_ms < 8000


def test_memory_limit_kills_or_memory_error():
    code = "x = bytearray(500 * 1024 * 1024)\nprint('allocated', len(x))\n"
    result = run_python_sandbox(
        code, limits=SandboxLimits(max_seconds=30, max_memory_mb=64)
    )
    assert result.exit_code != 0
    # Either the OS raised MemoryError inside the guest (RLIMIT_AS, Linux,
    # proactive) or the RSS monitor SIGKILLed the process group (macOS
    # fallback, reactive - the guest may finish a quick print before the
    # kill lands, but the run is still marked failed).
    killed = result.exit_code in (-9, 137)
    assert (
        killed
        or b"MemoryError" in result.stderr
        or b"memory limit exceeded" in result.stderr
    )
    if killed:
        # visible marker, never a silent success
        assert b"memory limit exceeded" in result.stderr


def test_output_truncated_to_cap():
    code = (
        "import sys\n"
        "for i in range(20000):\n"
        "    sys.stdout.write('x' * 31 + chr(10))\n"
        "sys.stdout.flush()\n"
    )
    result = run_python_sandbox(code, limits=SandboxLimits(max_output_bytes=4096))
    assert result.exit_code == 0
    assert len(result.stdout) == 4096
    assert result.truncated is True


def test_collect_file_over_cap_is_skipped_and_marked():
    code = (
        "open('predictions.json', 'w').write('x' * %d)\n"
        "print('written')\n" % (MAX_COLLECT_FILE_BYTES + 4096)
    )
    result = run_python_sandbox(code, collect_files=("predictions.json",))
    assert result.exit_code == 0
    assert "predictions.json" not in result.files
    assert result.truncated is True
    assert b"collection cap" in result.stderr  # visible, not silently dropped


PROBE_OUTSIDE = "/tmp/p2r_sbx_probe"


def test_write_outside_workdir_denied_or_honestly_marked():
    caps = sandbox_capabilities()
    code = (
        "fh = open(%r, 'w')\n"
        "fh.write('leaked')\n"
        "fh.close()\n"
        "print('WROTE-OUTSIDE-BAD')\n" % PROBE_OUTSIDE
    )
    try:
        result = run_python_sandbox(code)
        if caps["seatbelt"]:
            assert result.network_isolated is True
            assert result.exit_code != 0
            assert b"Operation not permitted" in result.stderr
            assert not os.path.exists(PROBE_OUTSIDE)
        else:
            # Degraded mode must not claim isolation it does not have.
            assert result.network_isolated is False
    finally:
        if os.path.exists(PROBE_OUTSIDE):
            os.unlink(PROBE_OUTSIDE)


def test_guest_env_is_whitelist_constructed_not_inherited(monkeypatch):
    monkeypatch.setenv("P2R_SECRET_API_KEY", "super-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aksk-should-not-leak")
    code = (
        "import json, os\n"
        "print(json.dumps({'keys': sorted(os.environ.keys()),"
        " 'path': os.environ.get('PATH', '')}))\n"
    )
    result = run_python_sandbox(code)
    assert result.exit_code == 0
    payload = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
    assert set(payload["keys"]) <= WHITELIST_ENV_KEYS
    assert "P2R_SECRET_API_KEY" not in payload["keys"]
    # Nothing secret may appear anywhere in the captured output either.
    assert b"super-secret-value" not in result.stdout + result.stderr
    assert b"aksk-should-not-leak" not in result.stdout + result.stderr
    # PATH is constructed: python dir first, no host PATH leakage.
    assert payload["path"].startswith(os.path.dirname(sys.executable) + os.pathsep) or \
        payload["path"].startswith(os.path.dirname(os.path.abspath(sys.executable)) + os.pathsep)


def test_sandbox_limits_rejects_nonpositive_values():
    with pytest.raises(ValueError):
        SandboxLimits(max_seconds=0)
    with pytest.raises(ValueError):
        SandboxLimits(max_output_bytes=-1)


# ---------------------------------------------------------------------------
# validate_output_files
# ---------------------------------------------------------------------------


def valid_files() -> dict[str, bytes]:
    return {
        "predictions.json": json.dumps(
            [{"id": "s1", "prediction": 1}, {"id": "s2", "prediction": 0}]
        ).encode(),
        "labels.json": json.dumps({"s1": 1, "s2": 0}).encode(),
    }


def test_validate_output_files_positive():
    predictions, labels = validate_output_files(valid_files())
    assert predictions == [{"id": "s1", "prediction": 1}, {"id": "s2", "prediction": 0}]
    assert labels == {"s1": 1, "s2": 0}


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda f: f.pop("predictions.json"), id="missing-predictions"),
        pytest.param(lambda f: f.pop("labels.json"), id="missing-labels"),
        pytest.param(lambda f: f.update({"predictions.json": b"not json"}), id="invalid-json"),
        pytest.param(
            lambda f: f.update({"predictions.json": json.dumps({"s1": 1}).encode()}),
            id="predictions-not-a-list",
        ),
        pytest.param(
            lambda f: f.update({"predictions.json": json.dumps([{"id": "s1"}]).encode()}),
            id="row-missing-prediction-key",
        ),
        pytest.param(
            lambda f: f.update(
                {"predictions.json": json.dumps(
                    [{"id": "s1", "prediction": 1, "extra": 2}]
                ).encode()}
            ),
            id="row-extra-key",
        ),
        pytest.param(
            lambda f: f.update(
                {"predictions.json": json.dumps([{"id": "s1", "prediction": True}]).encode()}
            ),
            id="bool-prediction",
        ),
        pytest.param(
            lambda f: f.update(
                {"predictions.json": json.dumps([{"id": 7, "prediction": 1}]).encode()}
            ),
            id="non-string-id",
        ),
        pytest.param(
            lambda f: f.update(
                {"predictions.json": json.dumps(
                    [{"id": "s1", "prediction": 1}, {"id": "s1", "prediction": 0}]
                ).encode()}
            ),
            id="duplicate-id",
        ),
        pytest.param(
            lambda f: f.update({"labels.json": json.dumps([1, 2]).encode()}),
            id="labels-not-a-mapping",
        ),
        pytest.param(
            lambda f: f.update({"labels.json": json.dumps({"s1": True}).encode()}),
            id="labels-bool-value",
        ),
        pytest.param(
            lambda f: f.update({"labels.json": json.dumps({"s1": "1"}).encode()}),
            id="labels-string-value",
        ),
    ],
)
def test_validate_output_files_negative(mutate):
    files = valid_files()
    mutate(files)
    with pytest.raises(SandboxOutputError):
        validate_output_files(files)
