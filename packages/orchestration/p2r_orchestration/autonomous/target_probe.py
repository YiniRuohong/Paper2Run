"""C6 target capability probe for a project's saved execution target.

Reports only what was observed. It never creates, starts or execs into a
container, so the network policy is never claimed here: ``prepare`` verifies
it inside the container for every task. Transport error text is not echoed
(SSH stderr can carry host details); only the program-chosen code is mapped.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sys

from p2r_sandbox.autonomous import ExecutionError

PROBE_TIMEOUT_SECONDS = 5  # per Docker call; three calls plus the GPU query stay under ~20 s
GPU_TIMEOUT_SECONDS = 4
NETWORK_DETAIL = "隔离规则在任务启动时由容器内探针验证"

_TRANSPORT = {
    "transport_timeout": "执行目标响应超时：请确认 Docker 已启动、SSH 目标可达后重新探测",
    "ssh_host_key": "SSH 主机密钥未登记或已变化：请核对目标身份并登记到本机 known_hosts",
    "ssh_authentication": "SSH 认证失败：请检查本机 SSH 配置别名与密钥",
}


def target_config(config: dict) -> dict:
    """ExecutionTarget config for the project workspace root (no task suffix)."""
    target = config["target"]
    ssh = target["kind"] == "ssh"
    result = {"kind": target["kind"],
              "workspace": target["remote_workspace"] if ssh else config["project"]["workspace"],
              "image": target["image"], "gpu_devices": list(config["limits"].get("gpu_devices", [])),
              "storage_bytes": config["limits"]["storage_bytes"], "network": dict(config["network"])}
    if ssh:
        result["ssh_alias"] = target["ssh_config_alias"]
    return result


def _platform(name: str) -> str:
    return "linux" if name.startswith("linux") else name


def _docker_missing(kind: str, code: str) -> str:
    if code in _TRANSPORT:
        return _TRANSPORT[code]
    if kind == "ssh":
        return f"无法在 SSH 目标上运行 Docker（{code}）：请确认目标已安装并启动 Docker"
    return "未检测到可用的 Docker：请安装并启动 Docker Desktop（Windows 需启用 WSL2 后端）"


def probe_target(config: dict, factory, state_dir, *, platform: str | None = None) -> dict:
    """Probe the target in ``config`` with ``factory(target_config, state_dir)``."""
    kind = config["target"]["kind"]
    image = config["target"]["image"]
    requested_gpus = list(config["limits"].get("gpu_devices", []))
    docker = {"available": False, "os_type": None, "server_version": None}
    present, gpus, missing = False, [], []
    detail = ""
    target_cfg = target_config(config)
    try:
        target = factory(target_cfg, state_dir)
        report = target.probe(timeout=PROBE_TIMEOUT_SECONDS)
    except ExecutionError as exc:
        # _host already turns OSError and subprocess.TimeoutExpired into ExecutionError.
        if exc.code == "invalid_config":
            detail = "执行目标配置无效"
            missing.append("执行目标配置无效：请检查工作区路径与 SSH 别名后保存配置")
        else:
            detail = f"未检测到可用的 Docker（{exc.code}）"
            missing.append(_docker_missing(kind, exc.code))
    except OSError:
        # Host-side failure before any Docker call (e.g. the probe state dir); not a Docker verdict.
        detail = "本机无法运行探测"
        missing.append("本机无法运行执行目标探测：请检查数据目录是否可写")
    else:
        docker.update(available=True, os_type=report.get("docker_os_type"),
                      server_version=report.get("docker_server_version"))
        present = report.get("image_available") is True
        if docker["os_type"] == "windows":
            detail = "Docker 处于 Windows 容器模式"
            missing.append("Docker 当前为 Windows 容器模式：请在 Docker Desktop 中切换到 Linux 容器")
        elif docker["os_type"] != "linux":
            detail = "无法确认 Docker 使用 Linux 容器"
            missing.append("无法读取 Docker 的 OSType，不能确认使用 Linux 容器")
        if not present:
            detail = detail or f"缺少实验镜像 {image}"
            missing.append(f"缺少实验镜像 {image}：请在执行目标上运行 bash scripts/autonomous/build-image.sh")
        if docker["os_type"] == "linux":
            gpus = target.observe_gpus(timeout=GPU_TIMEOUT_SECONDS)
    # Only a GPU probe that actually ran can say the requested GPUs are absent.
    if requested_gpus and not gpus and docker["os_type"] == "linux":
        missing.append("配置请求了 GPU 设备，但执行目标上未观测到 NVIDIA GPU")
    verified = docker["available"] and docker["os_type"] == "linux" and present
    if verified:
        detail = "Docker 使用 Linux 容器，实验镜像已就绪；容器隔离在任务启动时逐项验证"
    return {
        "platform": _platform(platform or sys.platform),
        "target_kind": kind,
        "isolation": {"backend": "docker", "verified": verified, "detail": detail},
        "docker": docker,
        "image": {"name": image, "present": present},
        "network_policy": {"public_internet": config["network"]["public_internet"],
                           "host_and_metadata_blocked": None, "verified": False, "detail": NETWORK_DETAIL},
        "gpu": gpus,
        "missing": missing,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }
