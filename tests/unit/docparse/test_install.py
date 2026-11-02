from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from p2r_docparse import install
from p2r_docparse.errors import DocParseError
from p2r_docparse.install import (
    MINERU_VERSION, OFFLINE_PLATFORMS, PIP_MIRROR, OfflineInstaller, _pick_tier,
)
from p2r_docparse.proc import CompletedRun, mineru_kit, venv_python

GB = 1024 ** 3
OK = CompletedRun(0, "", "", False)
FAIL = CompletedRun(1, "", "boom", False)


class FakeRun:
    """Records argv, executes a few side effects, and answers from a scripted table."""

    def __init__(self, root: Path, *, tier: str = "standard", pip_available: bool = True) -> None:
        self.root = root
        self.tier = tier
        self.pip_available = pip_available
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.rules: list = []  # (predicate(argv) -> bool, handler(argv) -> CompletedRun)

    def on(self, needle: str, handler) -> None:
        self.rules.append((lambda argv, n=needle: n in " ".join(argv), handler))

    def __call__(self, argv, *, env, cwd, timeout, cancelled=lambda: False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.envs.append(env)
        for pred, handler in self.rules:
            if pred(argv):
                return handler(argv)
        text = " ".join(argv)
        if "-m pip --version" in text:
            return OK if self.pip_available else FAIL
        if " -m venv " in text:
            venv_python(self.root).parent.mkdir(parents=True, exist_ok=True)
            return OK
        if "hardware detect" in text:
            return CompletedRun(0, json.dumps({"recommended_tier": self.tier}), "", False)
        if " parse " in text:
            out = Path(argv[argv.index("-o") + 1])
            (out / "doc.md").write_text("Paper2Run smoke\n", encoding="utf-8")
            return OK
        return OK

    def joined(self) -> list[str]:
        return [" ".join(a) for a in self.calls]

    def index(self, needle: str) -> int:
        return next(i for i, line in enumerate(self.joined()) if needle in line)

    def count(self, needle: str) -> int:
        return sum(1 for line in self.joined() if needle in line)


def make(tmp_path, run=None, *, free=10 * GB, platform="darwin-arm64", **kw):
    root = tmp_path / "docparse"
    run = run or FakeRun(root, **kw)
    inst = OfflineInstaller(root, base_python=Path("/usr/bin/basepy"), run=run,
                            disk_free=lambda p: free, platform=lambda: platform)
    return inst, run, root


def test_offline_platforms_constant():
    assert OFFLINE_PLATFORMS == frozenset({"darwin-arm64", "win32-x64"})
    assert MINERU_VERSION == "4.1.3"


def test_unsupported_platform(tmp_path):
    inst, run, _ = make(tmp_path, platform="darwin-x64")
    assert inst.state().state == "unsupported"
    with pytest.raises(DocParseError) as exc:
        inst.start()
    assert exc.value.code == "DOCPARSE_OFFLINE_UNSUPPORTED"
    assert run.calls == []


def test_initial_state_not_installed(tmp_path):
    inst, _, _ = make(tmp_path)
    s = inst.state()
    assert (s.state, s.step, s.progress, s.tier, s.version, s.bytes_on_disk, s.error) == (
        "not_installed", None, 0.0, None, None, 0, None)


def test_happy_install_sequence(tmp_path):
    inst, run, root = make(tmp_path)
    inst.start()
    final = inst.wait(10)
    assert final.state == "ready", final
    assert final.tier == "standard" and final.version == "4.1.3"
    assert final.progress == 1.0 and final.error is None
    order = [run.index(n) for n in (
        "-m venv", f"install --disable-pip-version-check --no-input mineru=={MINERU_VERSION}",
        "hardware detect --json", "models download --tier standard",
        "models verify --tier standard", " parse ")]
    assert order == sorted(order)
    assert any("--without-pip" in line for line in run.joined())
    assert f"--python {venv_python(root)}" in run.joined()[run.index("install ")]


def test_install_uses_network_env_but_smoke_is_offline(tmp_path):
    inst, run, _ = make(tmp_path)
    inst.start()
    assert inst.wait(10).state == "ready"
    for i, line in enumerate(run.joined()):
        offline = run.envs[i].get("HF_HUB_OFFLINE") == "1"
        if " parse " in line:
            assert offline
        else:
            assert not offline, line


def test_base_without_pip_uses_venv_pip(tmp_path):
    inst, run, root = make(tmp_path, pip_available=False)
    inst.start()
    assert inst.wait(10).state == "ready"
    venv_cmd = run.joined()[run.index("-m venv")]
    assert "--without-pip" not in venv_cmd
    pip_cmd = run.calls[run.index("install ")]
    assert pip_cmd[:3] == [str(venv_python(root)), "-m", "pip"]
    assert "--python" not in pip_cmd


def test_mirror_fallback(tmp_path):
    inst, run, _ = make(tmp_path)
    attempts: list[list[str]] = []

    def pip(argv):
        attempts.append(argv)
        return FAIL if len(attempts) == 1 else OK

    run.on("install --disable-pip-version-check", pip)
    inst.start()
    assert inst.wait(10).state == "ready"
    assert len(attempts) == 2
    assert "-i" not in attempts[0]
    assert attempts[1][attempts[1].index("-i") + 1] == PIP_MIRROR


def test_packages_failure_reports_fixed_message_and_removes_venv(tmp_path):
    inst, run, root = make(tmp_path)
    run.on("install --disable-pip-version-check",
           lambda argv: CompletedRun(1, "SECRET stdout", "SECRET stderr", False))
    (root / "mineru-home").mkdir(parents=True)
    inst.start()
    s = inst.wait(10)
    assert s.state == "failed"
    assert s.error == "安装 MinerU 失败（已尝试镜像源）"
    assert "SECRET" not in json.dumps(s.to_json(), ensure_ascii=False)
    assert not (root / "venv").exists()
    assert (root / "mineru-home").exists()
    assert run.count("install --disable-pip-version-check") == 2


def test_flash_skips_models(tmp_path):
    inst, run, _ = make(tmp_path, tier="flash")
    inst.start()
    s = inst.wait(10)
    assert s.state == "ready" and s.tier == "flash"
    assert run.count("models download") == 0 and run.count("models verify") == 0


def test_advanced_maps_to_standard_and_missing_falls_back_to_basic(tmp_path):
    inst, run, _ = make(tmp_path, tier="advanced")
    inst.start()
    assert inst.wait(10).tier == "standard"
    inst2, run2, _ = make(tmp_path / "b")
    run2.on("hardware detect", lambda argv: CompletedRun(0, "not json", "", False))
    inst2.start()
    s = inst2.wait(10)
    assert s.tier == "basic"
    assert run2.count("models download --tier basic") == 1


@pytest.mark.parametrize("stdout,expected", [
    ('{"recommended_tier": "flash"}', "flash"),
    ('{"tier": "Basic"}', "basic"),
    ('{"recommendation": {"tier": "standard"}}', "standard"),
    ('{"recommended": {"tier": "advanced"}}', "standard"),
    ('{"recommended_tier": "bogus", "tier": "flash"}', "flash"),
    ('{"recommended_tier": "bogus"}', "basic"),
    ('[1, 2]', "basic"),
    ('', "basic"),
])
def test_pick_tier(stdout, expected):
    assert _pick_tier(stdout) == expected


def test_models_download_and_verify_failures(tmp_path):
    inst, run, _ = make(tmp_path)
    run.on("models download", lambda argv: FAIL)
    inst.start()
    s = inst.wait(10)
    assert (s.state, s.error) == ("failed", "下载模型失败")
    inst2, run2, root2 = make(tmp_path / "b")
    run2.on("models verify", lambda argv: FAIL)
    inst2.start()
    s2 = inst2.wait(10)
    assert (s2.state, s2.error) == ("failed", "模型校验失败")
    assert not (root2 / "venv").exists()


def test_smoke_failure(tmp_path):
    inst, run, _ = make(tmp_path)
    run.on(" parse ", lambda argv: FAIL)
    inst.start()
    s = inst.wait(10)
    assert (s.state, s.error) == ("failed", "冒烟测试失败")


def test_smoke_requires_marker_text(tmp_path):
    inst, run, _ = make(tmp_path)

    def parse(argv):
        out = Path(argv[argv.index("-o") + 1])
        (out / "doc.md").write_text("totally unrelated\n", encoding="utf-8")
        return OK

    run.on(" parse ", parse)
    inst.start()
    assert inst.wait(10).error == "冒烟测试失败"


def test_venv_failure(tmp_path):
    inst, run, _ = make(tmp_path)
    run.on("-m venv", lambda argv: FAIL)
    inst.start()
    assert inst.wait(10).error == "创建 Python 环境失败"


def test_low_disk_fails(tmp_path):
    inst, run, _ = make(tmp_path, free=1 * GB)
    inst.start()
    s = inst.wait(10)
    assert s.state == "failed" and "磁盘空间不足" in s.error
    assert run.calls == []


def test_cancel_cleans_venv(tmp_path):
    inst, run, root = make(tmp_path)
    reached, release = threading.Event(), threading.Event()

    def pip(argv):
        reached.set()
        release.wait(5)
        return CompletedRun(-9, "", "", False, cancelled=True)

    run.on("install --disable-pip-version-check", pip)
    inst.start()
    assert reached.wait(5)
    assert (root / "venv").exists()
    threading.Timer(0.05, release.set).start()
    s = inst.cancel()
    assert s.state == "not_installed"
    assert not (root / "venv").exists()
    assert inst.state().state == "not_installed"


def test_start_while_installing_returns_current_state(tmp_path):
    inst, run, _ = make(tmp_path)
    reached, release = threading.Event(), threading.Event()

    def pip(argv):
        reached.set()
        release.wait(5)
        return OK

    run.on("install --disable-pip-version-check", pip)
    inst.start()
    assert reached.wait(5)
    again = inst.start()
    assert again.state == "installing" and again.step == "packages"
    assert again.progress == pytest.approx(2 / len(install.STEPS))
    release.set()
    assert inst.wait(10).state == "ready"
    assert run.count("-m venv") == 1


def test_state_persists(tmp_path):
    inst, run, root = make(tmp_path)
    inst.start()
    inst.wait(10)
    fresh, _, _ = make(tmp_path)
    s = fresh.state()
    assert (s.state, s.tier, s.version) == ("ready", "standard", "4.1.3")
    assert json.loads((root / "install-state.json").read_text(encoding="utf-8"))["state"] == "ready"


def test_stale_installing_state_is_not_stuck(tmp_path):
    root = tmp_path / "docparse"
    root.mkdir()
    (root / "install-state.json").write_text(
        json.dumps({"state": "installing", "step": "models", "progress": 0.6}), encoding="utf-8")
    inst, _, _ = make(tmp_path)
    s = inst.state()
    assert s.state == "failed" and s.error


def test_bytes_on_disk_counts_venv_and_models(tmp_path):
    inst, run, root = make(tmp_path)
    inst.start()
    inst.wait(10)
    (root / "venv" / "blob").write_bytes(b"x" * 100)
    (root / "mineru-home").mkdir(exist_ok=True)
    (root / "mineru-home" / "m.bin").write_bytes(b"y" * 50)
    assert inst.state().bytes_on_disk == 150


def test_ensure_healthy_true_when_version_ok(tmp_path):
    inst, run, root = make(tmp_path)
    inst.start()
    inst.wait(10)
    before = len(run.calls)
    assert inst.ensure_healthy() is True
    assert run.calls[before] == [str(mineru_kit(root)), "--version"]
    assert run.envs[before].get("HF_HUB_OFFLINE") == "1"
    assert len(run.calls) == before + 1
    assert inst.state().state == "ready"


def test_ensure_healthy_rebuilds_without_models(tmp_path):
    inst, run, root = make(tmp_path)
    inst.start()
    inst.wait(10)
    n = len(run.calls)
    run.on("--version", lambda argv: FAIL)
    seen_states: list[str] = []
    run.on("install --disable-pip-version-check",
           lambda argv: (seen_states.append(inst.state().state), OK)[1])
    assert inst.ensure_healthy() is True
    later = [" ".join(a) for a in run.calls[n:]]
    assert any("-m venv" in line for line in later)
    assert any("install --disable-pip-version-check" in line for line in later)
    assert not any("models" in line or "hardware" in line or " parse " in line for line in later)
    assert seen_states == ["broken"]
    s = inst.state()
    assert (s.state, s.tier, s.version) == ("ready", "standard", "4.1.3")


def test_ensure_healthy_false_when_rebuild_fails(tmp_path):
    inst, run, _ = make(tmp_path)
    inst.start()
    inst.wait(10)
    run.on("--version", lambda argv: FAIL)
    run.on("install --disable-pip-version-check", lambda argv: FAIL)
    assert inst.ensure_healthy() is False
    assert inst.state().state in {"broken", "failed"}


def test_ensure_healthy_false_when_not_installed(tmp_path):
    inst, run, _ = make(tmp_path)
    assert inst.ensure_healthy() is False
    assert run.calls == []


def test_uninstall_removes_env_and_models(tmp_path):
    inst, run, root = make(tmp_path)
    inst.start()
    inst.wait(10)
    (root / "mineru-home").mkdir(exist_ok=True)
    s = inst.uninstall()
    assert s.state == "not_installed"
    assert not (root / "venv").exists() and not (root / "mineru-home").exists()
    assert inst.state().state == "not_installed"


def test_uninstall_while_installing_cancels_first(tmp_path):
    inst, run, root = make(tmp_path)
    reached, release = threading.Event(), threading.Event()

    def pip(argv):
        reached.set()
        release.wait(5)
        return CompletedRun(-9, "", "", False, cancelled=True)

    run.on("install --disable-pip-version-check", pip)
    inst.start()
    assert reached.wait(5)
    threading.Timer(0.05, release.set).start()
    s = inst.uninstall()
    assert s.state == "not_installed"
    assert not (root / "venv").exists() and not (root / "mineru-home").exists()


def test_to_json_shape(tmp_path):
    inst, _, _ = make(tmp_path)
    assert set(inst.state().to_json()) == {
        "state", "step", "progress", "tier", "version", "bytes_on_disk", "error"}
