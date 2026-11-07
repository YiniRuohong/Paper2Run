"""Windows host side of the Docker execution target, forced on POSIX.

Everything inside the container stays Linux. These tests cover only what runs
on the host: workspace validation, the interpreter used for host scripts, the
bind-mount argv, subprocess text encoding and Windows interface enumeration.
"""
from __future__ import annotations

import ctypes
import json
import socket
import subprocess
import sys

import pytest

from p2r_sandbox.autonomous import ExecutionError, ExecutionTarget, execution
from p2r_sandbox.autonomous import _interfaces as interfaces

from .test_execution import config


# ---- workspace validation ---------------------------------------------------

@pytest.mark.parametrize("workspace", [
    r"C:\Users\me\AppData\Roaming\Paper2Run\data\autonomous-workspaces\abc\ws\task",
    "C:/Users/me/p2r/task",
    r"D:\p2r work\task",
])
def test_windows_local_workspace_accepted(workspace):
    execution.validate_workspace(workspace, "local", windows=True)


@pytest.mark.parametrize("workspace", [
    "/home/me/task",              # no drive: relative to the current drive
    r"\Users\me\task",
    r"C:relative\task",
    "C:\\",
    r"\\server\share\task",       # UNC shares are not a dedicated local directory
    r"C:\Users\..\Windows",
    r"C:\work,dst=/etc",          # would inject into the --mount spec
    "C:\\work\nx",
    "C:\\work\x00",
    "",
])
def test_windows_local_workspace_refused(workspace):
    with pytest.raises(ExecutionError) as error:
        execution.validate_workspace(workspace, "local", windows=True)
    assert error.value.code == "invalid_config"


@pytest.mark.parametrize("kind,windows", [("ssh", True), ("ssh", False), ("local", False)])
def test_posix_workspace_rules_unchanged(kind, windows):
    execution.validate_workspace("/srv/p2r/task", kind, windows=windows)
    for bad in (r"C:\work\task", "relative", "/", "/srv/../etc", "/srv/a,b", "/srv/a\nb"):
        with pytest.raises(ExecutionError):
            execution.validate_workspace(bad, kind, windows=windows)


# ---- interpreter and argv for host commands --------------------------------

class Recorder:
    def __init__(self, stop_at=None):
        self.calls = []
        self.stop_at = stop_at

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        joined = " ".join(argv)
        if self.stop_at and self.stop_at in joined:
            raise RuntimeError("stop")
        if "image inspect" in joined:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[-2:] == ["inspect", argv[-1]] and "p2r-" in argv[-1]:
            return subprocess.CompletedProcess(argv, 1, "", "No such object")
        if "network inspect" in joined:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if "-c" in argv or " -c " in joined:
            return subprocess.CompletedProcess(argv, 0, '["127.0.0.1"]', "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_local_host_scripts_use_this_interpreter(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    target = ExecutionTarget(config(tmp_path), tmp_path / "state")
    target._host_addresses()
    argv, kwargs = recorder.calls[-1]
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert kwargs["encoding"] == "utf-8"


def test_ssh_host_scripts_keep_python3(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    target = ExecutionTarget(config(tmp_path, kind="ssh", ssh_alias="gpu-box", workspace="/srv/p2r/task"), tmp_path / "state")
    target._host_addresses()
    argv, _ = recorder.calls[-1]
    assert argv[0] == "ssh"
    assert argv[-1].startswith("python3 -I -c ")


def test_host_interpreter_falls_back_when_executable_unknown(tmp_path, monkeypatch):
    target = ExecutionTarget(config(tmp_path), tmp_path / "state")
    monkeypatch.setattr(execution.sys, "executable", "")
    assert target._python() == "python3"


def windows_target(tmp_path, monkeypatch, workspace=r"C:\Users\me\p2r\task"):
    target = ExecutionTarget(config(tmp_path), tmp_path / "state")
    monkeypatch.setattr(execution, "_host_is_windows", lambda: True)
    target.workspace = workspace  # constructed on POSIX; the Windows path is what prepare sees
    return target


def test_windows_prepare_argv(tmp_path, monkeypatch):
    recorder = Recorder(stop_at=" start ")
    monkeypatch.setattr(subprocess, "run", recorder)
    target = windows_target(tmp_path, monkeypatch, r"C:\Users\Jane Doe\p2r\task")
    with pytest.raises(RuntimeError, match="stop"):
        target.prepare()
    scripts = [argv for argv, _ in recorder.calls if argv[0] == sys.executable]
    assert scripts == [[sys.executable, "-c", execution.WINDOWS_WORKSPACE_SCRIPT, r"C:\Users\Jane Doe\p2r\task"]]
    create = next(argv for argv, _ in recorder.calls if argv[:2] == ["docker", "create"])
    # One argv element, unquoted, never through a shell; Docker Desktop accepts drive paths.
    assert r"type=bind,src=C:\Users\Jane Doe\p2r\task,dst=/work" in create
    assert all(kwargs.get("shell") in (None, False) for _, kwargs in recorder.calls)
    assert all(kwargs.get("creationflags") == execution.CREATE_NO_WINDOW for _, kwargs in recorder.calls)


def test_posix_prepare_keeps_strict_realpath_script(tmp_path, monkeypatch):
    recorder = Recorder(stop_at=" start ")
    monkeypatch.setattr(subprocess, "run", recorder)
    target = ExecutionTarget(config(tmp_path), tmp_path / "state")
    with pytest.raises(RuntimeError):
        target.prepare()
    scripts = [argv for argv, _ in recorder.calls if argv[0] == sys.executable]
    assert scripts == [[sys.executable, "-c", execution.POSIX_WORKSPACE_SCRIPT, target.workspace]]
    assert all("creationflags" not in kwargs for _, kwargs in recorder.calls)


def test_windows_workspace_script_creates_and_rejects_links(tmp_path):
    # normcase is the identity on POSIX, so the Windows script runs here unchanged.
    created = tmp_path / "new" / "task"
    subprocess.run([sys.executable, "-c", execution.WINDOWS_WORKSPACE_SCRIPT, str(created)], check=True)
    assert created.is_dir()
    (tmp_path / "link").symlink_to(tmp_path / "new", target_is_directory=True)
    result = subprocess.run([sys.executable, "-c", execution.WINDOWS_WORKSPACE_SCRIPT, str(tmp_path / "link" / "task")],
                            capture_output=True)
    assert result.returncode != 0


# ---- GetAdaptersAddresses ---------------------------------------------------

AF_INET6_WINDOWS = 23


def sockaddr(family, payload):
    return family.to_bytes(2, "little") + b"\x00\x00" + payload + b"\x00" * 8


def lay_out(buffer, adapters):
    """Write a linked IP_ADAPTER_ADDRESSES list into ``buffer``; return bytes used."""
    base = ctypes.addressof(buffer)
    offset = 0

    def place(structure):
        nonlocal offset
        address = base + offset
        offset += ctypes.sizeof(structure)
        offset += -offset % 8
        return structure.from_address(address), address

    nodes = [place(interfaces.AdapterAddresses) for _ in adapters]
    for index, ((node, _), records) in enumerate(zip(nodes, adapters)):
        node.length = ctypes.sizeof(interfaces.AdapterAddresses)
        node.next = ctypes.cast(nodes[index + 1][1], ctypes.POINTER(interfaces.AdapterAddresses)) if index + 1 < len(nodes) else None
        unicasts = [place(interfaces.UnicastAddress) for _ in records]
        node.first_unicast = ctypes.cast(unicasts[0][1], ctypes.POINTER(interfaces.UnicastAddress)) if unicasts else None
        for position, ((unicast, _), raw) in enumerate(zip(unicasts, records)):
            data = base + offset
            ctypes.memmove(data, raw, len(raw))
            offset += len(raw) + (-len(raw) % 8)
            unicast.address.sockaddr = data
            unicast.address.length = len(raw)
            unicast.next = (ctypes.cast(unicasts[position + 1][1], ctypes.POINTER(interfaces.UnicastAddress))
                            if position + 1 < len(unicasts) else None)
    return offset


def fake_api(adapters, needed=4096):
    calls = []

    def api(family, flags, reserved, buffer, size):
        calls.append((family, flags, size._obj.value))
        assert family == interfaces.AF_INET_WINDOWS and reserved is None
        if size._obj.value < needed:
            size._obj.value = needed
            return interfaces.ERROR_BUFFER_OVERFLOW
        lay_out(buffer, adapters)
        return interfaces.ERROR_SUCCESS
    return api, calls


def test_get_adapters_addresses_retries_and_decodes_ipv4_unicast():
    adapters = [
        [sockaddr(socket.AF_INET, socket.inet_aton("10.1.0.2")), sockaddr(AF_INET6_WINDOWS, b"\x00" * 4)],
        [],
        [sockaddr(socket.AF_INET, socket.inet_aton("127.0.0.1")), sockaddr(socket.AF_INET, socket.inet_aton("192.168.5.9"))],
    ]
    api, calls = fake_api(adapters, needed=64 * 1024)
    records = interfaces.windows_records(api)
    assert len(calls) == 2 and calls[1][2] == 64 * 1024
    assert interfaces.ipv4_addresses(records, "win32") == ["10.1.0.2", "127.0.0.1", "192.168.5.9"]


def test_get_adapters_addresses_errors_fail_closed():
    with pytest.raises(OSError):
        interfaces.windows_records(lambda *args: 87)  # ERROR_INVALID_PARAMETER
    assert interfaces.windows_records(lambda *args: interfaces.ERROR_NO_DATA) == []

    def always_grows(family, flags, reserved, buffer, size):
        size._obj.value += 1024
        return interfaces.ERROR_BUFFER_OVERFLOW
    with pytest.raises(OSError):
        interfaces.windows_records(always_grows)


def test_windows_interface_addresses_refuse_empty(monkeypatch):
    monkeypatch.setattr(interfaces, "windows_records", lambda api=None: [sockaddr(socket.AF_INET, socket.inet_aton("10.0.0.7"))])
    assert interfaces.interface_addresses("win32") == ["10.0.0.7"]
    monkeypatch.setattr(interfaces, "windows_records", lambda api=None: [])
    with pytest.raises(RuntimeError, match="no IPv4"):
        interfaces.interface_addresses("win32")
    with pytest.raises(RuntimeError, match="requires"):
        interfaces.interface_addresses("sunos5")


def test_interfaces_script_stays_standalone():
    source = (interfaces.__file__ and open(interfaces.__file__, encoding="utf-8").read())
    assert "p2r_" not in source.replace("p2r_sandbox", "")
    assert json.loads(subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True, check=True).stdout)
