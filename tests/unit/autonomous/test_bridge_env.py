"""Harness subprocess environment and spawn options on POSIX and (forced) Windows."""
from __future__ import annotations

import subprocess

import pytest

from p2r_orchestration.autonomous import bridge
from p2r_orchestration.autonomous.bridge import HarnessBridge, harness_environment

HOST = {"PATH": "/bin", "SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
        "TEMP": r"C:\Users\me\AppData\Local\Temp", "TMP": r"C:\Users\me\AppData\Local\Temp",
        "USERPROFILE": r"C:\Users\me", "APPDATA": r"C:\Users\me\AppData\Roaming",
        "OPENAI_API_KEY": "sk-leak", "NODE_OPTIONS": "--require evil.js", "SSH_AUTH_SOCK": "/agent", "HOME": "/home/me"}


def test_posix_environment_is_unchanged(tmp_path):
    env = harness_environment(HOST, tmp_path, windows=False)
    assert env == {"PATH": "/bin", "SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "TMPDIR": "/tmp",
                   "LANG": "C.UTF-8", "HOME": str(tmp_path / ".home")}
    assert (tmp_path / ".home").is_dir()


def test_windows_environment_isolates_profile_dirs(tmp_path):
    env = harness_environment(HOST, tmp_path, windows=True)
    home = str(tmp_path / ".home")
    # Node's os.homedir() reads USERPROFILE on Windows; the real profile never leaks.
    assert env["HOME"] == env["USERPROFILE"] == home
    assert env["APPDATA"].startswith(home) and env["LOCALAPPDATA"].startswith(home)
    assert env["TEMP"] == env["TMP"] == HOST["TEMP"]
    assert env["SYSTEMROOT"] == env["WINDIR"] == r"C:\Windows"
    assert not {"OPENAI_API_KEY", "NODE_OPTIONS", "SSH_AUTH_SOCK"} & env.keys()
    assert r"C:\Users\me" not in env.values()


class Spawned(Exception):
    pass


@pytest.mark.parametrize("windows", [False, True])
def test_spawn_uses_configured_node_utf8_and_no_console(tmp_path, monkeypatch, windows):
    node = r"C:\Program Files\Paper2Run\resources\node\node.exe" if windows else "/opt/node/bin/node"
    monkeypatch.setenv("P2R_NODE_BINARY", node)
    monkeypatch.setenv("P2R_HARNESS_DIR", str(tmp_path / "harness"))
    monkeypatch.setattr(bridge, "_windows", lambda: windows)
    captured = {}

    def popen(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        raise Spawned
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(Spawned):
        HarnessBridge().run({"session_dir": str(tmp_path / "session"), "limits": {"wall_time_seconds": 5}}, "", lambda m: None)
    assert captured["argv"] == [node, str(tmp_path / "harness" / "bridge.mjs")]
    assert captured["encoding"] == "utf-8" and "text" not in captured
    if windows:
        assert captured["creationflags"] == 0x08000000  # CREATE_NO_WINDOW
        assert captured["env"]["USERPROFILE"] == str(tmp_path / "session" / ".home")
    else:
        assert "creationflags" not in captured
        assert "USERPROFILE" not in captured["env"]
