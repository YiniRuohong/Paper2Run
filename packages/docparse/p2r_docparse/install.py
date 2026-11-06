"""On-demand installer for the isolated offline MinerU runtime."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from .atomic import write_json_atomic
from .errors import DocParseError
from .offline import install_env, offline_env, parse_offline
from .platform import OFFLINE_PLATFORMS, current_platform
from .proc import CompletedRun, mineru_kit, run_tree, venv_python
from .testing import minimal_pdf

__all__ = ["InstallState", "OfflineInstaller", "OFFLINE_PLATFORMS", "current_platform",
           "MINERU_VERSION", "PIP_MIRROR", "MIN_FREE_BYTES", "STEPS"]

MINERU_VERSION = "4.1.3"
PIP_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"
MIN_FREE_BYTES = 6 * 1024**3
STEPS = ("preflight", "venv", "packages", "tier", "models", "smoke")

_TIERS = frozenset({"flash", "basic", "standard", "advanced"})
# MinerU 4.1.3 `hardware detect --json` reports `default_request_tier` (its own pick) and `ceiling`
# (highest tier the machine opens); the earlier keys are kept for other versions.
_TIER_KEYS = (("recommended_tier",), ("tier",), ("recommendation", "tier"), ("recommended", "tier"),
              ("default_request_tier",), ("ceiling",))
_TIMEOUTS = {"venv": 300.0, "pip": 1800.0, "detect": 600.0, "download": 3600.0,
             "verify": 600.0, "smoke": 600.0, "version": 30.0}
_ERRORS = {
    "preflight": "磁盘空间不足（需要约 6 GB）",
    "venv": "创建 Python 环境失败",
    "packages": "安装 MinerU 失败（已尝试镜像源）",
    "download": "下载模型失败",
    "verify": "模型校验失败",
    "smoke": "冒烟测试失败",
    "interrupted": "安装被中断，请重试",
    "unexpected": "安装失败",
}
_STATES = ("not_installed", "installing", "ready", "broken", "failed", "unsupported")


@dataclass
class InstallState:
    state: Literal["not_installed", "installing", "ready", "broken", "failed", "unsupported"]
    step: str | None = None
    progress: float = 0.0
    tier: str | None = None
    version: str | None = None
    bytes_on_disk: int = 0
    error: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _pick_tier(stdout: str) -> str:
    """Recommended tier from `mineru-kit hardware detect --json` output ("basic" if unknown)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return "basic"
    for path in _TIER_KEYS:
        node = data
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip().lower() in _TIERS:
            tier = node.strip().lower()
            return "standard" if tier == "advanced" else tier  # Advanced shares Standard's models
    return "basic"


def _tree_size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def _ok(done: CompletedRun) -> bool:
    return done.returncode == 0 and not done.timed_out and not getattr(done, "cancelled", False)


class OfflineInstaller:
    def __init__(self, root: Path, *, base_python: Path = Path(sys.executable),
                 run: Callable[..., CompletedRun] = run_tree,
                 disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
                 platform: Callable[[], str] = current_platform) -> None:
        self._root = Path(root).resolve()  # children run with another cwd
        self._base_python = Path(base_python)
        self._run = run
        self._disk_free = disk_free
        self._platform = platform
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)  # signalled when a worker ends / uninstall ends
        self._uninstalling = False
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._working = False
        self._pip_standalone = True  # base python has pip: drive the venv from outside

    # ---- persisted state -------------------------------------------------
    @property
    def _state_path(self) -> Path:
        return self._root / "install-state.json"

    def _read_locked(self) -> InstallState:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict) or raw.get("state") not in _STATES:
            return InstallState("not_installed")
        try:
            progress = float(raw.get("progress") or 0.0)
        except (TypeError, ValueError):
            progress = 0.0
        st = InstallState(raw["state"], raw.get("step"), progress,
                          raw.get("tier"), raw.get("version"), 0, raw.get("error"))
        if st.state in ("installing", "broken") and not self._working:
            return InstallState("failed", None, st.progress, st.tier, st.version, 0,
                                _ERRORS["interrupted"])  # process died mid-install
        return st

    def _write_locked(self, st: InstallState) -> None:
        data = st.to_json()
        data.pop("bytes_on_disk")
        write_json_atomic(self._state_path, data)

    def _update(self, **changes) -> InstallState:
        with self._lock:
            st = self._read_locked()
            for key, value in changes.items():
                setattr(st, key, value)
            self._write_locked(st)
            return st

    def state(self) -> InstallState:
        if self._platform() not in OFFLINE_PLATFORMS:
            return InstallState("unsupported")
        with self._lock:
            st = self._read_locked()
        if st.state not in ("installing", "unsupported"):
            st.bytes_on_disk = self._disk_use()  # leftovers of a failed install are still removable
        return st

    def _disk_use(self) -> int:
        return sum(_tree_size(self._root / d) for d in ("venv", "mineru-home"))

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> InstallState:
        if self._platform() not in OFFLINE_PLATFORMS:
            raise DocParseError("DOCPARSE_OFFLINE_UNSUPPORTED")
        with self._cond:
            self._cond.wait_for(lambda: not self._uninstalling)
            if self._working:
                return self._read_locked()
            self._working = True
            self._cancel.clear()
            self._write_locked(InstallState("installing", STEPS[0], 0.0))
            self._thread = threading.Thread(target=self._work, args=(STEPS,), daemon=True,
                                            name="p2r-offline-install")
            self._thread.start()
        return self.state()

    def cancel(self) -> InstallState:
        """Signal the active worker (background install or inline rebuild) and wait for it to end."""
        self._cancel.set()
        with self._cond:
            self._cond.wait_for(lambda: not self._working)
        return self.state()

    def uninstall(self) -> InstallState:
        """cancel -> wait -> delete -> write, with start()/ensure_healthy() fenced off meanwhile."""
        with self._cond:
            self._cond.wait_for(lambda: not self._uninstalling)
            self._uninstalling = True
            self._cancel.set()
            self._cond.wait_for(lambda: not self._working)
        try:
            for name in ("venv", "mineru-home"):
                shutil.rmtree(self._root / name, ignore_errors=True)
            with self._lock:
                self._write_locked(InstallState("not_installed"))
        finally:
            with self._cond:
                self._uninstalling = False
                self._cond.notify_all()
        return self.state()

    def wait(self, timeout: float) -> InstallState:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.state()

    def ensure_healthy(self) -> bool:
        """True when the venv answers --version; otherwise rebuild venv+packages (models kept)."""
        if self._platform() not in OFFLINE_PLATFORMS:
            return False
        with self._cond:
            # Another check, rebuild or uninstall owns the venv: wait for it, then re-read the state.
            self._cond.wait_for(lambda: not self._working and not self._uninstalling)
            if self._read_locked().state != "ready":
                return False
            self._working = True
            self._cancel.clear()
        try:
            healthy = self._version_ok()
            if healthy is None:
                return False  # unknown (timeout or cancel): leave the install alone
            if healthy:
                return True
            self._update(state="broken", step=None)
            shutil.rmtree(self._root / "venv", ignore_errors=True)
            self._work(("venv", "packages"), rebuild=True)
        finally:
            self._end_work()
        return self.state().state == "ready"

    def _version_ok(self) -> bool | None:
        """True/False when the executable answered or is missing; None when the check was inconclusive."""
        try:
            done = self._run([str(mineru_kit(self._root)), "--version"], env=offline_env(self._root),
                             cwd=self._root, timeout=_TIMEOUTS["version"], cancelled=self._cancel.is_set)
        except FileNotFoundError:
            return False
        except OSError:
            return None
        if done.timed_out or getattr(done, "cancelled", False):
            return None
        return done.returncode == 0

    # ---- worker ----------------------------------------------------------
    def _work(self, names: tuple[str, ...], rebuild: bool = False) -> None:
        try:
            for name in names:
                if self._cancel.is_set():
                    return self._cancelled()
                self._update(step=name, progress=STEPS.index(name) / len(STEPS))
                error = getattr(self, f"_step_{name}")()
                if self._cancel.is_set():
                    return self._cancelled()
                if error:
                    return self._fail(error)
            self._update(state="ready", step=None, progress=1.0, error=None,
                         version=MINERU_VERSION)
        except Exception:  # noqa: BLE001 - never leave the state stuck on "installing"
            self._fail(_ERRORS["unexpected"])
        finally:
            if not rebuild:
                self._end_work()

    def _end_work(self) -> None:
        with self._cond:
            self._working = False
            self._cond.notify_all()

    def _fail(self, message: str) -> None:
        shutil.rmtree(self._root / "venv", ignore_errors=True)
        self._update(state="failed", step=None, error=message)

    def _cancelled(self) -> None:
        shutil.rmtree(self._root / "venv", ignore_errors=True)
        self._update(state="not_installed", step=None, progress=0.0, error=None)

    def _exec(self, argv: list, timeout: float) -> CompletedRun:
        try:
            return self._run([str(a) for a in argv], env=install_env(self._root), cwd=self._root,
                             timeout=timeout, cancelled=self._cancel.is_set)
        except OSError:
            return CompletedRun(127, "", "", False)

    # ---- steps: each returns an error message or None -------------------
    def _step_preflight(self) -> str | None:
        self._root.mkdir(parents=True, exist_ok=True)
        needed = max(0, MIN_FREE_BYTES - _tree_size(self._root / "mineru-home"))  # models already on disk
        return None if self._disk_free(self._root) >= needed else _ERRORS["preflight"]

    def _step_venv(self) -> str | None:
        # A venv left half-deleted by an interrupted install or cancel makes pip fail
        # on it; every install builds the environment from scratch (models are kept).
        shutil.rmtree(self._root / "venv", ignore_errors=True)
        base = str(self._base_python)
        self._pip_standalone = _ok(self._exec([base, "-m", "pip", "--version"], _TIMEOUTS["venv"]))
        argv = [base, "-m", "venv"] + (["--without-pip"] if self._pip_standalone else [])
        done = self._exec(argv + [self._root / "venv"], _TIMEOUTS["venv"])
        return None if _ok(done) else _ERRORS["venv"]

    def _pip_install(self, *args: str) -> CompletedRun:
        py = venv_python(self._root)
        prefix = ([str(self._base_python), "-m", "pip", "--python", str(py)]
                  if self._pip_standalone else [str(py), "-m", "pip"])
        return self._exec(prefix + ["install", "--disable-pip-version-check", "--no-input", *args],
                          _TIMEOUTS["pip"])

    def _step_packages(self) -> str | None:
        spec = f"mineru=={MINERU_VERSION}"
        if _ok(self._pip_install(spec)) or self._cancel.is_set():
            return None
        return None if _ok(self._pip_install("-i", PIP_MIRROR, spec)) else _ERRORS["packages"]

    def _step_tier(self) -> str | None:
        done = self._exec([mineru_kit(self._root), "hardware", "detect", "--json"], _TIMEOUTS["detect"])
        self._update(tier=_pick_tier(done.stdout_tail) if _ok(done) else "basic")
        return None

    def _step_models(self) -> str | None:
        tier = self.state().tier
        if tier == "flash":
            return None
        kit = mineru_kit(self._root)
        if not _ok(self._exec([kit, "models", "download", "--tier", tier], _TIMEOUTS["download"])):
            return _ERRORS["download"]
        if not _ok(self._exec([kit, "models", "verify", "--tier", tier], _TIMEOUTS["verify"])):
            return _ERRORS["verify"]
        return None

    def _step_smoke(self) -> str | None:
        try:
            result = parse_offline(minimal_pdf(["Paper2Run smoke"]), root=self._root,
                                   tier=self.state().tier or "basic", version=MINERU_VERSION,
                                   timeout=_TIMEOUTS["smoke"], run=self._run,
                                   cancelled=self._cancel.is_set)
        except DocParseError:
            return _ERRORS["smoke"]
        return None if "smoke" in result.markdown.lower() else _ERRORS["smoke"]
