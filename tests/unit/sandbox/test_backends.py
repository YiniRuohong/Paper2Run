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
    # No private /tmp tmpfs (guest could exhaust host RAM through it); /dev
    # is remounted read-only so /dev/shm is not writable either. The
    # workdir bind comes last so it lands on top of the read-only root.
    assert _bwrap_argv(BASE, "/tmp/wd") == [
        "bwrap",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--remount-ro",
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


def _pretend_posix(monkeypatch, mod) -> None:
    """The Linux backends short-circuit on win32 by design; these
    monkeypatched selection/probe tests exercise the POSIX logic, so pin a
    POSIX platform there (no-op on macOS/Linux)."""
    if sys.platform == "win32":
        monkeypatch.setattr(mod.sys, "platform", "linux")


def test_select_backend_preference_order(monkeypatch):
    from p2r_sandbox import sandbox as mod

    _pretend_posix(monkeypatch, mod)

    monkeypatch.setattr(mod, "_seatbelt_available", lambda: True)
    monkeypatch.setattr(mod, "_bwrap_available", lambda: True)
    monkeypatch.setattr(mod, "_landlock_available", lambda: True)
    monkeypatch.setattr(mod, "_unshare_net_available", lambda: True)
    # seatbelt is only ever chosen on darwin, by design
    expected_first = "seatbelt" if sys.platform == "darwin" else "landlock"
    assert mod._select_backend() == (expected_first, True)

    monkeypatch.setattr(mod, "_seatbelt_available", lambda: False)
    assert mod._select_backend() == ("landlock", True)

    monkeypatch.setattr(mod, "_landlock_available", lambda: False)
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

    _pretend_posix(monkeypatch, mod)

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

    _pretend_posix(monkeypatch, mod)

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
    # A refused connection proves a working route: the errno whitelist must
    # not contain ECONNREFUSED, and the probe must also require "lo only".
    from p2r_sandbox.sandbox import _NS_PROBE_SCRIPT

    errno_line = next(l for l in _NS_PROBE_SCRIPT.splitlines() if l.startswith("net_errnos"))
    assert "ENETUNREACH" in errno_line and "ECONNREFUSED" not in errno_line
    assert "if_nameindex" in _NS_PROBE_SCRIPT


def test_ns_probe_reports_unisolated_host_honestly(tmp_path):
    # Run the probe with NO namespace wrapper: a normal host has non-loopback
    # interfaces, so the verdict must be net=False (no false positive).
    from p2r_sandbox.sandbox import _run_ns_probe

    checks = _run_ns_probe([sys.executable, "-I"], str(tmp_path), None)
    assert checks.get("net") is False


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


@pytest.mark.skipif(sys.platform != "darwin", reason="sensitive-path denies are seatbelt-only")
def test_sensitive_probe_path_is_an_existing_directory():
    from p2r_sandbox.sandbox import _sensitive_probe_path

    path = _sensitive_probe_path()
    assert os.path.isdir(path)
    assert any(os.path.expanduser(p) == path for p in SENSITIVE_READ_DENY_PATHS)


# ---------------------------------------------------------------------------
# fail-closed: require_isolation refuses before anything runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["none", "unshare"])
def test_require_isolation_refuses_weak_backends(monkeypatch, tmp_path, backend):
    from p2r_sandbox import SandboxUnavailable, run_python_sandbox
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(mod, "_select_backend", lambda: (backend, backend != "none"))
    marker = tmp_path / "ran"
    with pytest.raises(SandboxUnavailable, match="refusing to run untrusted code"):
        run_python_sandbox(f"open({str(marker)!r}, 'w').write('x')", require_isolation=True)
    assert not marker.exists()


def test_require_isolation_default_off_keeps_compatibility(monkeypatch):
    from p2r_sandbox import run_python_sandbox
    from p2r_sandbox import sandbox as mod

    monkeypatch.setattr(mod, "_select_backend", lambda: ("none", False))
    assert run_python_sandbox("print('ok')").exit_code == 0


def test_full_isolation_backends_are_exactly_net_and_write_walls():
    from p2r_sandbox.sandbox import FULL_ISOLATION_BACKENDS

    assert FULL_ISOLATION_BACKENDS == {"seatbelt", "bwrap", "landlock", "appcontainer"}
