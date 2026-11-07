"""Is this machine ready to run experiments? Read-only checks, plus a background import of the bundled image.

Standalone like the rest of p2r_sandbox: it imports no p2r_* package. Every check answers with a row
``{key, label, status, detail, fix}`` where status is ok / warn / bad; ``fix`` says what to do about it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IMAGE = "paper2run-experiment:1"
MIN_FREE_BYTES = 8 * 1024 ** 3
LOAD_TIMEOUT = 1800
DOCKER_WAIT_SECONDS = 120  # Docker Desktop may still be starting when the app opens
MIRRORS = (("阿里云 PyPI 镜像", "https://mirrors.aliyun.com/pypi/simple/"),
           ("阿里云 Debian 镜像", "https://mirrors.aliyun.com/debian/dists/bookworm/Release"))
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def decode(raw: bytes) -> str:
    """wsl.exe prints UTF-16; other tools print UTF-8 or the Windows ANSI page (GBK here)."""
    if b"\x00" in raw:
        return raw.decode("utf-16-le", "replace").replace("\x00", "")
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run(argv: list[str], timeout: float = 15):
    """(returncode, text), or None when the command is missing or does not answer in time."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.returncode, decode(done.stdout + done.stderr).replace("\r", "").strip()


def _row(key: str, label: str, status: str, detail: str, fix: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "fix": fix}


def check_virtualization() -> dict:
    script = ("$c = Get-CimInstance Win32_ComputerSystem; $p = Get-CimInstance Win32_Processor | Select-Object -First 1; "
              "\"$($c.HypervisorPresent) $($p.VirtualizationFirmwareEnabled)\"")
    got = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 25)
    parts = got[1].lower().split() if got and got[0] == 0 else []
    if len(parts) != 2:
        return _row("virtualization", "硬件虚拟化", "warn", "无法读取虚拟化状态", "确认 BIOS 已开启虚拟化（Intel VT-x / AMD-V）")
    hypervisor, firmware = parts[0] == "true", parts[1] == "true"
    # With a hypervisor already running the firmware flag reads False; the hypervisor is the real evidence.
    if hypervisor:
        return _row("virtualization", "硬件虚拟化", "ok", "虚拟化已启用")
    if firmware:
        return _row("virtualization", "硬件虚拟化", "warn", "BIOS 已开启虚拟化，但 Windows 虚拟化平台未运行",
                    "启用 WSL2（wsl --install）后重启")
    return _row("virtualization", "硬件虚拟化", "bad", "未检测到硬件虚拟化", "在 BIOS 中开启虚拟化（Intel VT-x / AMD-V）后重启")


def check_wsl() -> dict:
    got = run(["wsl.exe", "--status"])
    if got is None:
        return _row("wsl", "WSL2", "bad", "未检测到 WSL", "以管理员身份运行 wsl --install，然后重启电脑")
    code, text = got
    if code != 0:
        return _row("wsl", "WSL2", "bad", "WSL 未启用或不可用", "以管理员身份运行 wsl --install，然后重启电脑")
    match = re.search(r"(?:默认版本|Default Version)\D*(\d)", text)
    if match and match.group(1) != "2":
        return _row("wsl", "WSL2", "warn", f"默认版本为 {match.group(1)}", "运行 wsl --set-default-version 2")
    return _row("wsl", "WSL2", "ok", "WSL2 可用")


def check_docker() -> dict:
    got = run(["docker", "info", "--format", "{{.OSType}}"], 20)
    if got is None:
        return _row("docker", "Docker", "bad", "未检测到 Docker", "安装 Docker Desktop（使用 WSL2 后端）并启动")
    code, text = got
    if code != 0:
        return _row("docker", "Docker", "bad", "Docker 已安装但未运行", "启动 Docker Desktop，等托盘图标稳定后重新检测")
    if text == "linux":
        return _row("docker", "Docker", "ok", "Docker 使用 Linux 容器")
    return _row("docker", "Docker", "bad", f"Docker 当前为 {text or '未知'} 容器模式", "在 Docker Desktop 托盘菜单切换到 Linux 容器")


def check_image(archive: Path | None) -> dict:
    got = run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], 20)
    if got and got[0] == 0:
        return _row("image", "实验镜像", "ok", f"{IMAGE} 已就绪")
    if IMAGE_LOADER.state()["state"] == "loading":
        return _row("image", "实验镜像", "warn", "正在导入实验镜像（约 1 分钟）", "等待导入完成后重新检测")
    if archive and archive.is_file():
        return _row("image", "实验镜像", "warn", "镜像尚未导入，应用启动后会自动导入", "确认 Docker 已运行；也可手动导入")
    return _row("image", "实验镜像", "bad", "缺少实验镜像，安装包内也没有镜像文件", "重新安装完整安装包")


def check_disk(path: Path) -> dict:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return _row("disk", "磁盘空间", "warn", "无法读取剩余空间")
    gb = free / 1024 ** 3
    if free >= MIN_FREE_BYTES:
        return _row("disk", "磁盘空间", "ok", f"剩余 {gb:.0f} GB")
    return _row("disk", "磁盘空间", "bad", f"剩余 {gb:.1f} GB，至少需要 {MIN_FREE_BYTES // 1024 ** 3} GB", "清理磁盘后重新检测")


def _reach(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=6):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500  # the server answered
    except Exception:
        return False


def check_network() -> dict:
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as pool:
        reached = list(pool.map(lambda item: _reach(item[1]), MIRRORS))
    if all(reached):
        return _row("network", "国内镜像源", "ok", "pip / apt 镜像源可直接访问")
    down = "、".join(name for (name, _), ok in zip(MIRRORS, reached) if not ok)
    if any(reached):
        return _row("network", "国内镜像源", "warn", f"部分镜像源不可达：{down}", "检查网络或代理设置")
    return _row("network", "国内镜像源", "bad", "国内镜像源均不可达，实验中安装依赖会失败", "检查网络或代理设置")


def run_all(archive: Path | str | None, data_dir: Path | str, platform: str | None = None) -> dict:
    platform = platform or sys.platform
    archive = Path(archive) if archive else None
    checks = []
    if platform == "win32":
        checks += [check_virtualization, check_wsl]
    checks += [check_docker, lambda: check_image(archive), lambda: check_disk(Path(data_dir)), check_network]
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda check: check(), checks))
    return {"platform": platform, "ok": all(row["status"] != "bad" for row in rows), "checks": rows,
            "image_load": IMAGE_LOADER.state()}


class ImageLoader:
    """Imports the bundled image archive into Docker in the background; the state is readable at any time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = {"state": "idle", "error": None}

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self._state = {"state": state, "error": error}

    def start(self, archive: Path | str | None, wait: bool = False) -> dict:
        with self._lock:
            if self._state["state"] == "loading":
                return dict(self._state)
            self._state = {"state": "loading", "error": None}
        thread = threading.Thread(target=self._work, args=(Path(archive) if archive else None,), daemon=True, name="image-load")
        thread.start()
        if wait:
            thread.join()
        return self.state()

    @staticmethod
    def _present() -> bool:
        got = run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], 20)
        return bool(got and got[0] == 0)

    @staticmethod
    def _docker_up() -> bool:
        deadline = time.monotonic() + DOCKER_WAIT_SECONDS
        while True:
            got = run(["docker", "info", "--format", "{{.OSType}}"], 20)
            if got and got[0] == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(5)

    def _work(self, archive: Path | None) -> None:
        try:
            if not self._docker_up():
                self._set("failed", "Docker 未运行")
            elif self._present():
                self._set("ready")
            elif archive is None or not archive.is_file():
                self._set("absent", "安装包内没有实验镜像文件")
            else:
                # The archive goes in through stdin: no path (and no non-ASCII user name) reaches Docker.
                with archive.open("rb") as handle:
                    done = subprocess.run(["docker", "load"], stdin=handle, capture_output=True,
                                          timeout=LOAD_TIMEOUT, creationflags=_NO_WINDOW)
                if done.returncode != 0:
                    self._set("failed", decode(done.stderr).strip()[-500:] or "docker load 失败")
                elif not self._present():
                    self._set("failed", "导入完成但未找到实验镜像")
                else:
                    self._set("ready")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set("failed", repr(exc)[:300])


IMAGE_LOADER = ImageLoader()
