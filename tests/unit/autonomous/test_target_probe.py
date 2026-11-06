"""C6 target capability probe: observed facts only, never a container."""
from __future__ import annotations

import json
import subprocess

import pytest

from p2r_orchestration.autonomous.target_probe import NETWORK_DETAIL, probe_target, target_config
from p2r_sandbox.autonomous import ExecutionError, ExecutionTarget

from .test_execution import config as execution_config


# ---- sandbox: ExecutionTarget.probe reports Docker OSType, bounded ---------

def recorder(responses):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        joined = " ".join(argv)
        for needle, result in responses.items():
            if needle in joined:
                if isinstance(result, BaseException):
                    raise result
                code, stdout = result
                return subprocess.CompletedProcess(argv, code, stdout, "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")
    return run, calls


def test_probe_reports_docker_identity_without_creating_anything(tmp_path, monkeypatch):
    run, calls = recorder({"docker info": (0, '{"os_type":"linux","server_version":"27.3.1"}\n'),
                           "image inspect": (0, "[]"), "docker inspect": (1, "")})
    monkeypatch.setattr(subprocess, "run", run)
    report = ExecutionTarget(execution_config(tmp_path), tmp_path / "state").probe(timeout=5)
    assert report["docker_os_type"] == "linux" and report["docker_server_version"] == "27.3.1"
    assert report["image_available"] is True and report["ready"] is False
    assert {kwargs["timeout"] for _, kwargs in calls} == {5}
    verbs = [argv[1] for argv, _ in calls]
    assert verbs == ["info", "image", "inspect"]  # never create/start/exec/network create


@pytest.mark.parametrize("stdout,os_type,version", [
    ('{"os_type":"windows","server_version":"27.0"}', "windows", "27.0"),
    ('{"os_type":"plan9","server_version":""}', None, None),
    ("not json", None, None),
])
def test_probe_docker_identity_unknowns_stay_unknown(tmp_path, monkeypatch, stdout, os_type, version):
    run, _ = recorder({"docker info": (0, stdout), "image inspect": (1, ""), "docker inspect": (1, "")})
    monkeypatch.setattr(subprocess, "run", run)
    report = ExecutionTarget(execution_config(tmp_path), tmp_path / "state").probe(timeout=5)
    assert (report["docker_os_type"], report["docker_server_version"]) == (os_type, version)


def test_observe_gpus_parses_nvidia_smi_and_never_raises(tmp_path, monkeypatch):
    target = ExecutionTarget(execution_config(tmp_path), tmp_path / "state")
    run, calls = recorder({"nvidia-smi": (0, "0, NVIDIA A100-SXM4-80GB\n1, NVIDIA A100-SXM4-80GB\n\n")})
    monkeypatch.setattr(subprocess, "run", run)
    assert target.observe_gpus(timeout=4) == [{"index": "0", "name": "NVIDIA A100-SXM4-80GB"},
                                              {"index": "1", "name": "NVIDIA A100-SXM4-80GB"}]
    assert calls[0][1]["timeout"] == 4
    for failure in ((1, "NVIDIA-SMI has failed"), FileNotFoundError("nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 4)):
        run, _ = recorder({"nvidia-smi": failure})
        monkeypatch.setattr(subprocess, "run", run)
        assert target.observe_gpus(timeout=4) == []


# ---- orchestration: mapping observations to the C6 response ---------------

class FakeTarget:
    def __init__(self, report=None, error=None, gpus=(), gpu_error=None):
        self.report, self.error, self.gpus, self.gpu_error = report, error, list(gpus), gpu_error
        self.calls = []

    def probe(self, timeout):
        self.calls.append(("probe", timeout))
        if self.error:
            raise self.error
        return dict(self.report)

    def observe_gpus(self, timeout):
        self.calls.append(("gpus", timeout))
        return self.gpus

    def prepare(self):  # pragma: no cover - must never be called
        raise AssertionError("the probe must not prepare a container")


def project_config(kind="local", public=True, gpus=()):
    target = {"kind": kind, "profile": kind, "allocation": "dedicated", "isolation": "required", "image": "paper2run-experiment:1"}
    if kind == "ssh":
        target.update(ssh_config_alias="gpu-box", remote_workspace="/srv/paper2run/p1")
    return {"project": {"id": "p1", "workspace": "/data/autonomous-workspaces/abc/ws"}, "target": target,
            "limits": {"gpu_devices": list(gpus), "storage_bytes": 1000},
            "network": {"public_internet": public, "host_and_metadata_access": False}}


def ready_report(**kwargs):
    return {"image": "paper2run-experiment:1", "image_available": True, "docker_os_type": "linux",
            "docker_server_version": "27.3.1", **kwargs}


def run_probe(target, config=None, platform="darwin"):
    built = []

    def factory(cfg, state_dir):
        built.append((cfg, state_dir))
        if isinstance(target, Exception):
            raise target
        return target
    result = probe_target(config or project_config(), factory, "/state/probe", platform=platform)
    json.dumps(result)  # plain JSON
    return result, built


def test_ready_local_target_is_verified_but_network_never_is():
    target = FakeTarget(ready_report(), gpus=[])
    result, built = run_probe(target, platform="win32")
    assert result["platform"] == "win32" and result["target_kind"] == "local"
    assert result["isolation"] == {"backend": "docker", "verified": True, "detail": result["isolation"]["detail"]}
    assert result["docker"] == {"available": True, "os_type": "linux", "server_version": "27.3.1"}
    assert result["image"] == {"name": "paper2run-experiment:1", "present": True}
    assert result["network_policy"] == {"public_internet": True, "host_and_metadata_blocked": None,
                                        "verified": False, "detail": NETWORK_DETAIL}
    assert result["gpu"] == [] and result["missing"] == []
    assert result["probed_at"].endswith("+00:00")
    # The probe target is the project workspace root, never a task workspace or container.
    assert built[0][0]["workspace"] == "/data/autonomous-workspaces/abc/ws"
    assert [name for name, _ in target.calls] == ["probe", "gpus"]
    assert all(timeout <= 6 for _, timeout in target.calls)


@pytest.mark.parametrize("code,needle", [
    ("transport_unavailable", "Docker"), ("transport_error", "Docker"), ("transport_timeout", "超时"),
    ("ssh_host_key", "known_hosts"), ("ssh_authentication", "SSH 认证"),
])
def test_docker_unavailable_is_reported_not_raised(code, needle):
    target = FakeTarget(error=ExecutionError("secret-looking stderr text", code))
    result, _ = run_probe(target)
    assert result["docker"] == {"available": False, "os_type": None, "server_version": None}
    assert result["isolation"]["verified"] is False and result["image"]["present"] is False
    assert any(needle in item for item in result["missing"])
    assert "secret-looking" not in json.dumps(result, ensure_ascii=False)  # transport text is not echoed
    assert [name for name, _ in target.calls] == ["probe"]  # no GPU probe without Docker


def test_windows_container_mode_is_missing():
    result, _ = run_probe(FakeTarget(ready_report(docker_os_type="windows", image_available=False)))
    assert result["docker"]["os_type"] == "windows" and result["isolation"]["verified"] is False
    assert any("Linux 容器" in item for item in result["missing"])


def test_unknown_os_type_is_not_verified():
    result, _ = run_probe(FakeTarget(ready_report(docker_os_type=None)))
    assert result["docker"]["os_type"] is None and result["isolation"]["verified"] is False
    assert any("OSType" in item for item in result["missing"])


def test_missing_image_is_reported():
    result, _ = run_probe(FakeTarget(ready_report(image_available=False)))
    assert result["image"]["present"] is False and result["isolation"]["verified"] is False
    assert any("build-image.sh" in item for item in result["missing"])


def test_requested_gpu_not_observed_is_missing_and_observed_gpu_is_listed():
    config = project_config(kind="ssh", public=False, gpus=["0"])
    result, built = run_probe(FakeTarget(ready_report()), config)
    assert result["target_kind"] == "ssh" and result["gpu"] == []
    assert result["network_policy"]["public_internet"] is False
    assert any("GPU" in item for item in result["missing"])
    assert built[0][0]["workspace"] == "/srv/paper2run/p1" and built[0][0]["ssh_alias"] == "gpu-box"
    observed = [{"index": "0", "name": "NVIDIA L4"}]
    result, _ = run_probe(FakeTarget(ready_report(), gpus=observed), config)
    assert result["gpu"] == observed and result["missing"] == [] and result["isolation"]["verified"] is True


def test_programming_errors_are_not_reported_as_missing_docker():
    class Broken(FakeTarget):
        def probe(self, timeout):
            raise KeyError("docker_os_type")
    with pytest.raises(KeyError):
        run_probe(Broken())
    with pytest.raises(KeyError):
        run_probe(KeyError("target"))


def test_host_os_error_is_reported_without_blaming_docker():
    result, _ = run_probe(PermissionError(13, "state dir not writable"))
    assert result["docker"]["available"] is False and result["isolation"]["verified"] is False
    assert result["missing"] and not any("Docker" in item for item in result["missing"])
    assert "state dir" not in json.dumps(result, ensure_ascii=False)


def test_gpu_missing_only_when_the_gpu_probe_ran():
    config = project_config(gpus=["0"])
    windows_mode = FakeTarget(ready_report(docker_os_type="windows"))
    result, _ = run_probe(windows_mode, config)
    assert not any("GPU" in item for item in result["missing"])
    assert [name for name, _ in windows_mode.calls] == ["probe"]


def test_invalid_target_config_is_reported():
    result, _ = run_probe(ExecutionError("workspace must be ...", "invalid_config"))
    assert result["isolation"]["verified"] is False and result["docker"]["available"] is False
    assert any("配置" in item for item in result["missing"])


@pytest.mark.parametrize("platform,expected", [("linux", "linux"), ("darwin", "darwin"), ("win32", "win32"), ("cygwin", "cygwin")])
def test_platform_names(platform, expected):
    assert run_probe(FakeTarget(ready_report()), platform=platform)[0]["platform"] == expected


def test_target_config_matches_execution_shape():
    local = target_config(project_config(gpus=["0"]))
    assert local == {"kind": "local", "workspace": "/data/autonomous-workspaces/abc/ws", "image": "paper2run-experiment:1",
                     "gpu_devices": ["0"], "storage_bytes": 1000,
                     "network": {"public_internet": True, "host_and_metadata_access": False}}
    ssh = target_config(project_config(kind="ssh"))
    assert ssh["workspace"] == "/srv/paper2run/p1" and ssh["ssh_alias"] == "gpu-box"
