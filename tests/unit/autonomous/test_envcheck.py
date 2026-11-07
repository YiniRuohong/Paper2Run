"""Environment checks and the bundled-image import, with every external command faked."""
from __future__ import annotations

import subprocess

import pytest

from p2r_sandbox.autonomous import envcheck


def fake_run(table):
    def run(argv, timeout=15):
        for needle, result in table.items():
            if needle in " ".join(argv):
                return result
        return None
    return run


def test_decode_handles_wsl_utf16_and_gbk():
    assert "默认版本: 2" in envcheck.decode("默认版本: 2\r\n".encode("utf-16-le"))
    assert envcheck.decode("中文".encode("gbk")) == "中文"
    assert envcheck.decode("abc".encode()) == "abc"


@pytest.mark.parametrize("output, status", [
    ("True False", "ok"),    # a running hypervisor hides the firmware flag: still fine
    ("False True", "warn"),
    ("False False", "bad"),
])
def test_virtualization_trusts_the_hypervisor_first(monkeypatch, output, status):
    monkeypatch.setattr(envcheck, "run", fake_run({"powershell": (0, output)}))
    assert envcheck.check_virtualization()["status"] == status


def test_virtualization_unreadable_is_only_a_warning(monkeypatch):
    monkeypatch.setattr(envcheck, "run", fake_run({}))
    assert envcheck.check_virtualization()["status"] == "warn"


@pytest.mark.parametrize("got, status", [
    (None, "bad"), ((1, ""), "bad"), ((0, "默认版本: 2"), "ok"), ((0, "Default Version: 1"), "warn"),
])
def test_wsl_rows(monkeypatch, got, status):
    monkeypatch.setattr(envcheck, "run", fake_run({"wsl.exe": got}))
    row = envcheck.check_wsl()
    assert row["status"] == status
    assert (status == "ok") == (row["fix"] == "")


@pytest.mark.parametrize("got, status", [(None, "bad"), ((1, "error"), "bad"), ((0, "linux"), "ok"), ((0, "windows"), "bad")])
def test_docker_rows(monkeypatch, got, status):
    monkeypatch.setattr(envcheck, "run", fake_run({"docker info": got}))
    assert envcheck.check_docker()["status"] == status


def test_image_row_distinguishes_present_bundled_and_missing(monkeypatch, tmp_path):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"x")
    monkeypatch.setattr(envcheck, "IMAGE_LOADER", envcheck.ImageLoader())
    monkeypatch.setattr(envcheck, "run", fake_run({"image inspect": (0, "sha256:x")}))
    assert envcheck.check_image(archive)["status"] == "ok"
    monkeypatch.setattr(envcheck, "run", fake_run({"image inspect": (1, "")}))
    assert envcheck.check_image(archive)["status"] == "warn"
    assert envcheck.check_image(tmp_path / "nope.tar.gz")["status"] == "bad"


def test_disk_row(monkeypatch, tmp_path):
    usage = lambda free: type("U", (), {"free": free})()
    monkeypatch.setattr(envcheck.shutil, "disk_usage", lambda p: usage(50 * 1024 ** 3))
    assert envcheck.check_disk(tmp_path / "not" / "yet")["status"] == "ok"
    monkeypatch.setattr(envcheck.shutil, "disk_usage", lambda p: usage(1024 ** 3))
    assert envcheck.check_disk(tmp_path)["status"] == "bad"


def test_network_row(monkeypatch):
    monkeypatch.setattr(envcheck, "_reach", lambda url: True)
    assert envcheck.check_network()["status"] == "ok"
    monkeypatch.setattr(envcheck, "_reach", lambda url: "pypi" in url)
    assert envcheck.check_network()["status"] == "warn"
    monkeypatch.setattr(envcheck, "_reach", lambda url: False)
    assert envcheck.check_network()["status"] == "bad"


def test_run_all_skips_windows_only_rows_elsewhere_and_fails_on_a_bad_row(monkeypatch, tmp_path):
    monkeypatch.setattr(envcheck, "run", fake_run({}))  # no docker at all
    monkeypatch.setattr(envcheck, "_reach", lambda url: True)
    monkeypatch.setattr(envcheck, "IMAGE_LOADER", envcheck.ImageLoader())
    result = envcheck.run_all(None, tmp_path, platform="darwin")
    keys = [row["key"] for row in result["checks"]]
    assert keys == ["docker", "image", "disk", "network"] and result["ok"] is False
    windows = envcheck.run_all(None, tmp_path, platform="win32")
    assert [row["key"] for row in windows["checks"]][:2] == ["virtualization", "wsl"]


# ---- the background import ----------------------------------------------------

@pytest.fixture
def loader(monkeypatch):
    monkeypatch.setattr(envcheck, "DOCKER_WAIT_SECONDS", 0)
    return envcheck.ImageLoader()


def test_loader_imports_the_archive_through_stdin(loader, monkeypatch, tmp_path):
    archive = tmp_path / "中文目录" / "image.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"tar-bytes")
    seen = {"inspect": 0, "loaded": None}

    def run(argv, timeout=15):
        if "inspect" in argv:
            seen["inspect"] += 1
            return (0, "sha256:x") if seen["loaded"] else (1, "")
        return (0, "linux")

    def load(argv, **kwargs):
        seen["loaded"] = kwargs["stdin"].read()
        assert argv == ["docker", "load"]  # the path never reaches Docker
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(envcheck, "run", run)
    monkeypatch.setattr(envcheck.subprocess, "run", load)
    assert loader.start(archive, wait=True)["state"] == "ready"
    assert seen["loaded"] == b"tar-bytes"


def test_loader_does_nothing_when_the_image_is_already_there(loader, monkeypatch, tmp_path):
    monkeypatch.setattr(envcheck, "run", fake_run({"inspect": (0, "sha256:x"), "docker info": (0, "linux")}))
    monkeypatch.setattr(envcheck.subprocess, "run", lambda *a, **k: pytest.fail("must not reload"))
    assert loader.start(tmp_path / "absent.tar.gz", wait=True)["state"] == "ready"


def test_loader_reports_missing_archive_docker_down_and_a_failed_load(loader, monkeypatch, tmp_path):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"x")
    monkeypatch.setattr(envcheck, "run", fake_run({"inspect": (1, ""), "docker info": (0, "linux")}))
    assert loader.start(None, wait=True)["state"] == "absent"
    monkeypatch.setattr(envcheck.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 1, b"", "磁盘已满".encode("utf-8")))
    state = loader.start(archive, wait=True)
    assert state["state"] == "failed" and "磁盘已满" in state["error"]
    monkeypatch.setattr(envcheck, "run", fake_run({}))
    assert loader.start(archive, wait=True) == {"state": "failed", "error": "Docker 未运行"}
