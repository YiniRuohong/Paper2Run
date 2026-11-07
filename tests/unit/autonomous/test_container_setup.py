"""In-container workspace setup, run on the host with every system call recorded.

The experiment container drops every capability except a fixed few; root has no
CAP_FOWNER. Once /work belongs to the experiment user, only that user may change
its mode. Docker Desktop on Windows (9p/drvfs bind mount) enforces this: a root
chmod after the chown failed with EPERM on a real Windows 11 machine.
"""
from __future__ import annotations

import pytest

from p2r_sandbox.autonomous import ExecutionTarget
from p2r_sandbox.autonomous import _container as container

from .test_execution import config

AGENT = "--reuid=10001"


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(container, "CONTROL", tmp_path / "control")
    monkeypatch.setattr(container.subprocess, "run", lambda argv, **kw: calls.append(("run", list(argv))))
    monkeypatch.setattr(container.os, "chown", lambda path, uid, gid: calls.append(("chown", path, uid, gid)))
    real_chmod = container.os.chmod
    def chmod(path, mode):
        if str(path).startswith(str(tmp_path)):
            return real_chmod(path, mode)
        calls.append(("chmod", path, mode))
    monkeypatch.setattr(container.os, "chmod", chmod)
    container.setup({"host_addresses": ["192.168.77.2"], "public_internet": True})
    return calls


def as_agent(argv):
    return argv[0] == "setpriv" and AGENT in argv and "--no-new-privs" in argv


def test_root_never_changes_the_mode_of_the_agent_workspace(recorded):
    assert not [c for c in recorded if c[0] == "chmod" and str(c[1]).startswith("/work")]
    root_chmods = [c for c in recorded if c[0] == "run" and "chmod" in c[1] and not as_agent(c[1])]
    assert root_chmods == []


def test_workspace_is_handed_to_the_agent_then_set_up_by_it(recorded):
    chown = recorded.index(("chown", "/work", 10001, 10001))
    agent = [(i, c[1]) for i, c in enumerate(recorded) if c[0] == "run" and as_agent(c[1])]
    commands = [argv[argv.index("--no-new-privs") + 1:] for _, argv in agent]
    assert ["chmod", "2775", "/work"] in commands
    assert ["python3", "-I", "-m", "venv", "/work/.venv"] in commands
    assert ["chmod", "-R", "g+rwX", "/work/.venv"] in commands
    assert all(i > chown for i, _ in agent)
    assert commands.index(["python3", "-I", "-m", "venv", "/work/.venv"]) < commands.index(["chmod", "-R", "g+rwX", "/work/.venv"])


def test_prepare_allows_a_slow_workspace_mount(tmp_path, monkeypatch):
    # Creating the venv alone took 36 s on a Windows bind mount; 90 s left no margin.
    target = ExecutionTarget(config(tmp_path), tmp_path / "state")
    seen = {}
    monkeypatch.setattr(target, "_host", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(target, "_docker", lambda *a, **k: type("R", (), {
        "returncode": 1 if a[:1] == ("inspect",) or a[:2] == ("network", "inspect") else 0,
        "stdout": '[{"Id": "x", "State": {"StartedAt": "t", "Running": true}, "NetworkSettings": {"Networks": {}}}]',
        "stderr": ""})())
    monkeypatch.setattr(target, "_current_addresses", lambda obj: ["192.168.77.2"])
    def call(op, args, **kw):
        seen[op] = kw.get("timeout", 45)
        raise RuntimeError("stop")
    monkeypatch.setattr(target, "_call", call)
    with pytest.raises(RuntimeError, match="stop"):
        target.prepare()
    assert seen["setup"] >= 300


class _Stop(Exception):
    pass


def test_jobs_get_the_image_mirror_variables_and_nothing_else(tmp_path, monkeypatch):
    import json
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "spec.json").write_text(json.dumps({"command": "pip install six", "timeout_seconds": 5}))
    monkeypatch.setattr(container, "job_path", lambda job: job_dir)
    monkeypatch.setattr(container, "boot", lambda: "boot")
    monkeypatch.setattr(container, "identity", lambda pid: "1")
    monkeypatch.setattr(container.os, "chown", lambda *a: None)
    import ctypes
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: type("C", (), {"prctl": staticmethod(lambda *x: 0)})())
    for name, value in {"PIP_INDEX_URL": "https://mirrors.aliyun.com/pypi/simple/", "HF_ENDPOINT": "https://hf-mirror.com",
                        "UV_DEFAULT_INDEX": "", "AWS_SECRET_ACCESS_KEY": "must-not-leak"}.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def popen(argv, **kwargs):
        if "guardian" in argv:
            return None
        seen.update(kwargs["env"])
        raise _Stop

    monkeypatch.setattr(container.subprocess, "Popen", popen)
    with pytest.raises(_Stop):
        container.run_worker("job", 20007, 5)
    assert seen["PIP_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert seen["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert "UV_DEFAULT_INDEX" not in seen  # empty values are not passed
    assert "AWS_SECRET_ACCESS_KEY" not in seen and seen["P2R_JOB_UID"] == "20007"
