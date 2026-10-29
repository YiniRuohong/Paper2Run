"""Backend construction, probe gating and capability reporting tests.

The argv builders are pure functions so their exact output is asserted
byte-for-byte without any OS backend present. Probe/selection logic is
exercised with monkeypatched verdicts; nothing here claims a Linux backend
was ever *executed* - that happens only on a Linux host with the binaries
installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from p2r_sandbox import sandbox_capabilities
from p2r_sandbox.sandbox import (
    SANDBOX_EXEC,
    SENSITIVE_READ_DENY_PATHS,
    _bwrap_argv,
    _probe_bwrap,
    _probe_unshare,
    _select_backend,
    _seatbelt_profile,
    _unshare_argv,
    _wrapped_argv,
)

BASE = ["/usr/bin/python3", "-I", "script.py"]


# ---------------------------------------------------------------------------
# pure argv builders: exact content
# ---------------------------------------------------------------------------


def test_bwrap_argv_exact():
    # Mount order matters: the workdir bind must land ON TOP of the private
    # /tmp tmpfs (a workdir under /tmp would otherwise be masked).
    assert _bwrap_argv(BASE, "/tmp/wd") == [
        "bwrap",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/tmp",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        "/tmp/wd",
        "/tmp/wd",
        "--die-with-parent",
        "--",
        *BASE,
    ]


def test_unshare_argv_exact():
    assert _unshare_argv(BASE) == ["unshare", "-rn", *BASE]


def test_wrapped_argv_dispatch():
    seatbelt = _wrapped_argv("seatbelt", "/usr/bin/python3", "/tmp/wd")
    assert seatbelt[:2] == [SANDBOX_EXEC, "-p"]
    assert seatbelt[-3:] == BASE
    assert "(deny network*)" in seatbelt[2]

    assert _wrapped_argv("bwrap", "/usr/bin/python3", "/tmp/wd") == _bwrap_argv(
        BASE, "/tmp/wd"
    )
    assert _wrapped_argv("unshare", "/usr/bin/python3", "/tmp/wd") == _unshare_argv(
        BASE
    )
    assert _wrapped_argv("none", "/usr/bin/python3", "/tmp/wd") == BASE


# ---------------------------------------------------------------------------
# seatbelt profile: sensitive-read deny list
# ---------------------------------------------------------------------------


def test_seatbelt_profile_denies_sensitive_paths():
    profile = _seatbelt_profile("/tmp/allowed")
    for raw_path in (
        "~/.ssh",
        "~/.aws",
        "~/.config",
        "~/.gnupg",
        "~/.minimax",
        "~/Library/Keychains",
        "~/Library/Application Support",
        "/etc/ssh",
        "/private/etc/ssh",
    ):
        assert raw_path in SENSITIVE_READ_DENY_PATHS
        expected = f'(deny file-read* (subpath "{os.path.expanduser(raw_path)}"))'
        assert expected in profile, f"missing deny for {raw_path}"
    home = os.path.expanduser("~")
    assert f'(deny file-read* (regex #"^{re.escape(home)}/\\.env"))' in profile
    # legacy semantics intact
    assert "(deny network*)" in profile
    assert "(deny file-write*)" in profile
    assert '(allow file-write* (subpath "/tmp/allowed"))' in profile
    assert "(allow default)" in profile


# ---------------------------------------------------------------------------
# backend selection: preference order and honest degradation (monkeypatched)
# ---------------------------------------------------------------------------


def test_select_backend_preference_order(monkeypatch):
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(mod, "_seatbelt_available", lambda: True)
    monkeypatch.setattr(mod, "_bwrap_available", lambda: True)
    monkeypatch.setattr(mod, "_unshare_net_available", lambda: True)
    assert mod._select_backend() == ("seatbelt", True)

    monkeypatch.setattr(mod, "_seatbelt_available", lambda: False)
    assert mod._select_backend() == ("bwrap", True)

    monkeypatch.setattr(mod, "_bwrap_available", lambda: False)
    assert mod._select_backend() == ("unshare", True)

    monkeypatch.setattr(mod, "_unshare_net_available", lambda: False)
    assert mod._select_backend() == ("none", False)


def test_probe_bwrap_requires_binary(monkeypatch):
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert _probe_bwrap() is False
    assert _probe_unshare() is False


def test_probe_bwrap_requires_net_and_write_denial(monkeypatch):
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name else None
    )
    monkeypatch.setattr(
        mod,
        "_run_ns_probe",
        lambda argv, workdir, outside: {"net": True, "write": False},
    )
    assert _probe_bwrap() is False
    monkeypatch.setattr(
        mod,
        "_run_ns_probe",
        lambda argv, workdir, outside: {"net": True, "write": True},
    )
    assert _probe_bwrap() is True


def test_probe_unshare_requires_net_only(monkeypatch):
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(
        mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name else None
    )
    monkeypatch.setattr(
        mod, "_run_ns_probe", lambda argv, workdir, outside: {"net": False}
    )
    assert _probe_unshare() is False
    monkeypatch.setattr(
        mod, "_run_ns_probe", lambda argv, workdir, outside: {"net": True}
    )
    assert _probe_unshare() is True


def test_probe_launch_failure_disables_backend(monkeypatch):
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    def boom(*args, **kwargs):
        raise OSError("no such process")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert _probe_bwrap() is False
    assert _probe_unshare() is False


def test_ns_probe_script_rejects_loopback_success():
    # ECONNREFUSED means the loopback route worked: the probe script must
    # not count it as network isolation.
    from p2r_sandbox.sandbox import _NS_PROBE_SCRIPT

    assert "ENETUNREACH" in _NS_PROBE_SCRIPT
    # The errno whitelist cannot contain ECONNREFUSED (its absence is the point).
    assert "ECONNREFUSED" not in _NS_PROBE_SCRIPT


# ---------------------------------------------------------------------------
# capabilities: legacy keys unchanged, new keys additive
# ---------------------------------------------------------------------------


def test_capabilities_keys_are_additive_and_bool():
    caps = sandbox_capabilities()
    assert {"seatbelt", "rlimits", "platform_walls"} <= set(caps)
    for key in ("sensitive_read_denied", "bubblewrap", "unshare_net"):
        assert key in caps
        assert isinstance(caps[key], bool)
    # On a host without the Linux binaries these must be honestly False.
    if sys.platform == "darwin":
        assert caps["bubblewrap"] is False
        assert caps["unshare_net"] is False


def test_capabilities_json_serializable():
    caps = sandbox_capabilities()
    assert json.loads(json.dumps(caps)) == caps


def test_sensitive_probe_path_is_an_existing_directory():
    from p2r_sandbox.sandbox import _sensitive_probe_path

    path = _sensitive_probe_path()
    assert os.path.isdir(path)
    assert any(os.path.expanduser(p) == path for p in SENSITIVE_READ_DENY_PATHS)
