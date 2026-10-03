"""Read-only environment checks and execution eligibility decisions."""
from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_NEEDED = "not_needed"


class EnvironmentDecision(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"


class CheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=128)
    status: CheckStatus
    required: bool = True
    observed: Any = None
    required_value: Any = None
    suggestion: str = ""


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[CheckItem] = Field(default_factory=list)


class EnvironmentCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checker_version: str
    decision: EnvironmentDecision
    items: list[CheckItem]
    repair_suggestions: list[str] = Field(default_factory=list)
    # Deliberately empty: the checker explains remediation but never executes it.
    repair_commands: list[str] = Field(default_factory=list)


class EnvironmentProbe(Protocol):
    def check(self, requirements: dict[str, Any]) -> list[CheckItem] | ProbeResult: ...


class DefaultEnvironmentProbe:
    """Conservative, read-only local probe.

    Requirements are intentionally generic and allow only facts the checker can
    inspect without installing packages, invoking shell commands, or contacting
    a remote host.
    """

    def check(self, requirements: dict[str, Any]) -> list[CheckItem]:
        items: list[CheckItem] = []
        self._add_python(items, requirements)
        self._add_platform(items, requirements)
        self._add_cpu(items, requirements)
        self._add_memory(items, requirements)
        self._add_disk(items, requirements)
        self._add_dependencies(items, requirements)
        self._add_dataset(items, requirements)
        self._add_sandbox(items, requirements)
        return items

    @staticmethod
    def _add_python(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        required = requirements.get("python")
        if not required:
            items.append(CheckItem(key="python", status=CheckStatus.PASS, required=False,
                                   observed=platform.python_version()))
            return
        # Do not pretend to implement a package-spec parser; common minimum form
        # is enough for the first checker and unknown is safer than a false pass.
        observed = sys.version_info[:3]
        if isinstance(required, str) and required.startswith(">="):
            try:
                target = tuple(int(x) for x in required[2:].split(".")[:3])
                status = CheckStatus.PASS if observed >= target else CheckStatus.FAIL
            except ValueError:
                status = CheckStatus.UNKNOWN
        else:
            status = CheckStatus.PASS if str(observed[0]) == str(required) else CheckStatus.UNKNOWN
        items.append(CheckItem(key="python", status=status, observed=platform.python_version(),
                               required_value=required,
                               suggestion="使用满足要求的 Python 版本" if status != CheckStatus.PASS else ""))

    @staticmethod
    def _add_platform(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        required = requirements.get("platform") or requirements.get("os")
        observed = f"{platform.system().lower()}/{platform.machine()}"
        if not required:
            items.append(CheckItem(key="platform", status=CheckStatus.PASS, required=False, observed=observed))
        elif isinstance(required, str):
            items.append(CheckItem(key="platform", status=CheckStatus.PASS if required.lower() in observed else CheckStatus.FAIL,
                                   observed=observed, required_value=required,
                                   suggestion="切换到支持的操作系统或架构" if required.lower() not in observed else ""))
        else:
            items.append(CheckItem(key="platform", status=CheckStatus.UNKNOWN, observed=observed, required_value=required))

    @staticmethod
    def _add_cpu(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        required = requirements.get("cpu_count")
        observed = os.cpu_count()
        if required is None:
            items.append(CheckItem(key="cpu", status=CheckStatus.PASS, required=False, observed=observed))
        elif isinstance(required, int) and isinstance(observed, int):
            ok = observed >= required
            items.append(CheckItem(key="cpu", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                                   observed=observed, required_value=required,
                                   suggestion="增加可用 CPU 核心数" if not ok else ""))
        else:
            items.append(CheckItem(key="cpu", status=CheckStatus.UNKNOWN, observed=observed, required_value=required))

    @staticmethod
    def _add_memory(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        required = requirements.get("memory_bytes")
        if required is None:
            items.append(CheckItem(key="memory", status=CheckStatus.UNKNOWN, required=False,
                                   observed=None, suggestion="未读取内存信息；仅用于展示"))
        else:
            items.append(CheckItem(key="memory", status=CheckStatus.UNKNOWN, required=True,
                                   required_value=required, suggestion="提供可读的内存探针结果"))

    @staticmethod
    def _add_disk(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        required = requirements.get("disk_bytes")
        path = requirements.get("disk_path") or "."
        try:
            observed = shutil.disk_usage(Path(path).resolve()).free
        except (OSError, TypeError, ValueError):
            observed = None
        if required is None:
            items.append(CheckItem(key="disk", status=CheckStatus.PASS if observed is not None else CheckStatus.UNKNOWN,
                                   required=False, observed=observed))
        elif observed is None:
            items.append(CheckItem(key="disk", status=CheckStatus.UNKNOWN, required_value=required,
                                   suggestion="确认工作目录所在磁盘可读"))
        else:
            ok = observed >= required
            items.append(CheckItem(key="disk", status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                                   observed=observed, required_value=required,
                                   suggestion="释放工作目录所在磁盘空间" if not ok else ""))

    @staticmethod
    def _add_dependencies(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        packages = requirements.get("dependencies") or {}
        if not isinstance(packages, dict):
            items.append(CheckItem(key="dependencies", status=CheckStatus.UNKNOWN, required_value=packages))
            return
        for name, expected in packages.items():
            try:
                observed = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                observed = None
            status = CheckStatus.PASS if observed is not None and (not expected or str(observed) == str(expected)) else CheckStatus.FAIL
            items.append(CheckItem(key=f"dependency:{name}", status=status, observed=observed,
                                   required_value=expected,
                                   suggestion=f"安装或配置依赖 {name}" if status != CheckStatus.PASS else ""))

    @staticmethod
    def _add_dataset(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        path = requirements.get("dataset_path")
        if not path:
            items.append(CheckItem(key="dataset", status=CheckStatus.NOT_NEEDED, required=False))
            return
        exists = Path(path).is_file() or Path(path).is_dir()
        items.append(CheckItem(key="dataset", status=CheckStatus.PASS if exists else CheckStatus.FAIL,
                               observed=str(path) if exists else None, required_value=str(path),
                               suggestion="提供可读的数据集路径" if not exists else ""))

    @staticmethod
    def _add_sandbox(items: list[CheckItem], requirements: dict[str, Any]) -> None:
        """Probe the host's model-code sandbox capabilities (read-only).

        When "sandbox" is requested, an honest capability split: seatbelt
        (network/write isolation) plus rlimits is a pass; rlimits alone leaves
        network isolation unavailable — reported as unknown, never as pass;
        neither is a fail. When not requested the item stays informational.
        """
        required_raw = requirements.get("sandbox")
        required = required_raw is not None and str(required_raw).lower() not in ("false", "no", "0", "")
        try:
            from p2r_sandbox import sandbox_capabilities
            capabilities = sandbox_capabilities()
        except Exception:
            capabilities = None
        seatbelt = bool(capabilities.get("seatbelt")) if isinstance(capabilities, dict) else False
        rlimits = bool(capabilities.get("rlimits")) if isinstance(capabilities, dict) else False
        platform_walls = bool(capabilities.get("platform_walls")) if isinstance(capabilities, dict) else False
        if seatbelt and rlimits:
            status, suggestion = CheckStatus.PASS, ""
            observed = "seatbelt+rlimits"
        elif rlimits:
            status = CheckStatus.UNKNOWN
            suggestion = "主机未提供网络隔离（seatbelt 不可用）：沙箱可执行但断网不可保证"
            observed = "rlimits-only"
        elif platform_walls:
            status = CheckStatus.UNKNOWN
            suggestion = "Windows 进程墙：墙钟/输出封顶/临时目录隔离可用，断网与内存、进程数限额不可保证"
            observed = "windows-walls"
        else:
            status = CheckStatus.FAIL
            suggestion = "主机不具备沙箱执行能力"
            observed = "unavailable"
        items.append(CheckItem(key="sandbox", status=status, observed=observed,
                               required=required,
                               required_value=required_raw if required else None,
                               suggestion=suggestion))


class EnvironmentChecker:
    VERSION = "environment-checker.v1"

    def __init__(self, probe: EnvironmentProbe | None = None) -> None:
        self._probe = probe or DefaultEnvironmentProbe()

    def check(self, *, recipe_supported: bool, requirements: dict[str, Any] | None = None) -> EnvironmentCheckResult:
        requirements = requirements or {}
        raw = self._probe.check(dict(requirements))
        items = raw.items if isinstance(raw, ProbeResult) else list(raw)
        if not all(isinstance(item, CheckItem) for item in items):
            items = [CheckItem(key="probe", status=CheckStatus.UNKNOWN,
                               suggestion="检查器返回了无法识别的结果")]
        if not recipe_supported:
            decision = EnvironmentDecision.UNSUPPORTED
        elif any(item.required and item.status in {CheckStatus.FAIL, CheckStatus.UNKNOWN} for item in items):
            decision = EnvironmentDecision.BLOCKED
        else:
            decision = EnvironmentDecision.READY
        suggestions = [item.suggestion for item in items if item.suggestion]
        return EnvironmentCheckResult(
            checker_version=self.VERSION,
            decision=decision,
            items=items,
            repair_suggestions=suggestions,
        )
