"""Windows AppContainer launch for the sandbox guest (ctypes, no deps).

Every run gets a **fresh AppContainer profile** (unique name, deleted
afterwards together with its private storage) holding exactly one custom
capability, :data:`CAPABILITY_NAME`, and **no system capabilities**: no
``internetClient`` / ``privateNetworkClientServer`` means no network at all
(loopback included), and the low-box token can only open objects whose ACL
grants the container SID, the custom capability SID or
``ALL APPLICATION PACKAGES`` (system directories). We grant:

* read+execute on the guest interpreter's trees (venv root, base install)
  to the *capability* SID - a one-time, idempotent, inheritable ACE
  (``icacls``), so per-run profiles need no re-grant;
* full control on each throwaway workdir to that run's *container* SID
  only, so concurrent runs cannot see each other's workdirs.

Everything else - the user profile (``~/.ssh``, ``AppData``, documents),
other processes' memory/environment - is denied by the kernel.

The process is created ``CREATE_SUSPENDED`` with an explicit
``PROC_THREAD_ATTRIBUTE_HANDLE_LIST`` (only the three stdio handles are
inherited), assigned to the caller's Job Object, and only then resumed.

Importable on every platform; Win32 calls are resolved lazily.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from ctypes import wintypes

PROFILE_PREFIX = "p2r.sbx."
PROFILE_DISPLAY = "Paper2Run sandbox guest"
CAPABILITY_NAME = "p2rSandboxInterpreterRead"
SE_GROUP_ENABLED = 0x00000004

E_ALREADY_EXISTS = 0x800700B7
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009

_DWORD = ctypes.c_uint32


class AppContainerError(OSError):
    """AppContainer setup failed; the guest must not run unconfined."""


def environment_block(env: dict[str, str]) -> str:
    """Pure: sorted ``k=v\\0...\\0\\0`` block for CREATE_UNICODE_ENVIRONMENT."""
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in items) + "\0"


def icacls_grant_args(path: str, sid: str, rights: str) -> list[str]:
    """Pure: icacls argv granting an inheritable ACE to ``sid``."""
    return ["icacls", path, "/grant", f"*{sid}:(OI)(CI)({rights})", "/Q", "/C"]


def profile_name(token: str) -> str:
    """Pure: per-run profile name (AppContainer names are <= 64 chars)."""
    return (PROFILE_PREFIX + token)[:64]


def interpreter_trees(python_exe: str) -> list[str]:
    """Directories the guest interpreter must read (venv root + base home)."""
    exe = os.path.abspath(python_exe)
    exe_dir = os.path.dirname(exe)
    trees = [exe_dir]
    root = os.path.dirname(exe_dir) if os.path.basename(exe_dir).lower() == "scripts" else exe_dir
    trees.append(root)
    cfg = os.path.join(root, "pyvenv.cfg")
    if os.path.isfile(cfg):
        with open(cfg, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, _, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    home = value.strip()
                    trees.append(home)
                    if os.path.basename(home).lower() == "scripts":
                        trees.append(os.path.dirname(home))
    out: list[str] = []
    for tree in trees:
        real = os.path.realpath(tree)
        if os.path.isdir(real) and real not in out:
            out.append(real)
    return out


# ---------------------------------------------------------------------------
# Win32 bindings (lazy)
# ---------------------------------------------------------------------------


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", _DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", _DWORD),
        ("Reserved", _DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p), ("dwX", _DWORD), ("dwY", _DWORD),
        ("dwXSize", _DWORD), ("dwYSize", _DWORD), ("dwXCountChars", _DWORD),
        ("dwYCountChars", _DWORD), ("dwFillAttribute", _DWORD), ("dwFlags", _DWORD),
        ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
        ("dwProcessId", _DWORD), ("dwThreadId", _DWORD),
    ]


_libs: dict | None = None


def _win():
    global _libs
    if _libs is None:
        if sys.platform != "win32":
            raise AppContainerError("AppContainers exist only on Windows")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        uenv = ctypes.WinDLL("userenv", use_last_error=True)
        uenv.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, _DWORD, ctypes.POINTER(ctypes.c_void_p)]
        uenv.CreateAppContainerProfile.restype = ctypes.c_long
        uenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        uenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        uenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
        uenv.DeleteAppContainerProfile.restype = ctypes.c_long
        kb = ctypes.WinDLL("kernelbase", use_last_error=True)
        kb.DeriveCapabilitySidsFromName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)), ctypes.POINTER(_DWORD),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)), ctypes.POINTER(_DWORD)]
        kb.DeriveCapabilitySidsFromName.restype = wintypes.BOOL
        adv.FreeSid.argtypes = [ctypes.c_void_p]
        adv.FreeSid.restype = ctypes.c_void_p
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p
        k32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, _DWORD, _DWORD, ctypes.POINTER(ctypes.c_size_t)]
        k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, _DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k32.DeleteProcThreadAttributeList.restype = None
        k32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, _DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)]
        k32.CreateProcessW.restype = wintypes.BOOL
        k32.ResumeThread.argtypes = [ctypes.c_void_p]
        k32.ResumeThread.restype = _DWORD
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = wintypes.BOOL
        _libs = {"k32": k32, "adv": adv, "uenv": uenv, "kb": kb}
    return _libs


_cap_cache: tuple[int, str] | None = None
_granted_trees: set[str] = set()
_lock = threading.Lock()


def _sid_string(psid) -> str:
    libs = _win()
    text = ctypes.c_wchar_p()
    if not libs["adv"].ConvertSidToStringSidW(psid, ctypes.byref(text)):
        raise AppContainerError("ConvertSidToStringSid failed")
    value = text.value
    libs["k32"].LocalFree(text)
    return value


def capability_sid() -> tuple[int, str]:
    """``(PSID, "S-1-15-3-1024-...")`` of the custom read capability."""
    global _cap_cache
    with _lock:
        if _cap_cache is not None:
            return _cap_cache
        libs = _win()
        groups = ctypes.POINTER(ctypes.c_void_p)()
        sids = ctypes.POINTER(ctypes.c_void_p)()
        ngroups, nsids = _DWORD(), _DWORD()
        if not libs["kb"].DeriveCapabilitySidsFromName(
                CAPABILITY_NAME, ctypes.byref(groups), ctypes.byref(ngroups),
                ctypes.byref(sids), ctypes.byref(nsids)) or nsids.value < 1:
            raise AppContainerError(
                f"DeriveCapabilitySidsFromName failed (winerror {ctypes.get_last_error()})")
        for i in range(ngroups.value):
            libs["k32"].LocalFree(groups[i])
        libs["k32"].LocalFree(groups)
        psid = sids[0]  # kept for the process lifetime
        for i in range(1, nsids.value):
            libs["k32"].LocalFree(sids[i])
        libs["k32"].LocalFree(sids)
        _cap_cache = (psid, _sid_string(psid))
        return _cap_cache


class Profile:
    """One per-run AppContainer profile; :meth:`delete` removes it and its
    private storage."""

    def __init__(self, name: str):
        libs = _win()
        self.name = name
        psid = ctypes.c_void_p()
        hr = libs["uenv"].CreateAppContainerProfile(
            name, PROFILE_DISPLAY, PROFILE_DISPLAY, None, 0, ctypes.byref(psid))
        if (hr & 0xFFFFFFFF) == E_ALREADY_EXISTS:
            hr = libs["uenv"].DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(psid))
        if hr != 0 or not psid.value:
            raise AppContainerError(f"AppContainer profile unavailable (HRESULT {hr & 0xFFFFFFFF:#x})")
        self.psid = psid.value
        self.sid = _sid_string(psid)

    def delete(self) -> None:
        if self.psid:
            libs = _win()
            libs["uenv"].DeleteAppContainerProfile(self.name)
            libs["adv"].FreeSid(self.psid)
            self.psid = 0


def _icacls(path: str, sid: str, rights: str) -> None:
    proc = subprocess.run(
        icacls_grant_args(path, sid, rights),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW, timeout=600,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("mbcs", errors="replace").strip()
        raise AppContainerError(f"icacls grant on {path} failed: {detail}")


def _has_ace(path: str, sid: str) -> bool:
    proc = subprocess.run(
        ["icacls", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW, timeout=60,
    )
    return sid.encode("ascii") in proc.stdout


def ensure_interpreter_readable(python_exe: str) -> list[str]:
    """Idempotently grant the capability read+execute on interpreter trees."""
    _psid, sid = capability_sid()
    trees = interpreter_trees(python_exe)
    for tree in trees:
        if tree in _granted_trees:
            continue
        if not _has_ace(tree, sid):
            _icacls(tree, sid, "RX")
        _granted_trees.add(tree)
    return trees


def grant_workdir(workdir: str, profile: Profile) -> None:
    _icacls(workdir, profile.sid, "F")


class AppContainerProcess:
    """Minimal ``subprocess.Popen`` look-alike used by the Windows pump."""

    def __init__(self, process_handle: int, thread_handle: int, pid: int, stdout, stderr):
        self._handle = process_handle
        self._thread = thread_handle
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None

    def resume(self) -> None:
        libs = _win()
        if libs["k32"].ResumeThread(self._thread) == 0xFFFFFFFF:
            raise AppContainerError(f"ResumeThread failed (winerror {ctypes.get_last_error()})")

    def poll(self) -> int | None:
        import _winapi

        if self.returncode is None:
            if _winapi.WaitForSingleObject(self._handle, 0) == _winapi.WAIT_OBJECT_0:
                self.returncode = _winapi.GetExitCodeProcess(self._handle)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import _winapi

        if self.returncode is None:
            ms = _winapi.INFINITE if timeout is None else int(timeout * 1000)
            if _winapi.WaitForSingleObject(self._handle, ms) != _winapi.WAIT_OBJECT_0:
                raise subprocess.TimeoutExpired("guest", timeout)
            self.returncode = _winapi.GetExitCodeProcess(self._handle)
        return self.returncode

    def kill(self) -> None:
        import _winapi

        if self.poll() is None:
            try:
                _winapi.TerminateProcess(self._handle, 1)
            except OSError:
                pass

    def close(self) -> None:
        libs = _win()
        for name in ("_thread", "_handle"):
            handle = getattr(self, name)
            if handle:
                libs["k32"].CloseHandle(handle)
                setattr(self, name, 0)


def spawn(
    argv: list[str], *, cwd: str, env: dict[str, str], job, profile: Profile
) -> AppContainerProcess:
    """Create ``argv`` suspended inside the AppContainer, put it in ``job``,
    resume it. Raises :class:`AppContainerError`; never runs unconfined."""
    import msvcrt

    libs = _win()
    k32 = libs["k32"]
    cap_psid, _cap = capability_sid()

    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    nul = os.open(os.devnull, os.O_RDONLY)
    child_fds = (nul, out_w, err_w)
    handles = [msvcrt.get_osfhandle(fd) for fd in child_fds]
    for handle in handles:
        os.set_handle_inheritable(handle, True)

    attr_list = None
    try:
        cap_entries = (SID_AND_ATTRIBUTES * 1)(SID_AND_ATTRIBUTES(cap_psid, SE_GROUP_ENABLED))
        caps = SECURITY_CAPABILITIES(
            profile.psid, ctypes.cast(cap_entries, ctypes.c_void_p), 1, 0)
        handle_array = (ctypes.c_void_p * 3)(*handles)
        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attr_buf = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attr_buf, 2, 0, ctypes.byref(size)):
            raise AppContainerError("InitializeProcThreadAttributeList failed")
        attr_list = attr_buf
        if not k32.UpdateProcThreadAttribute(
                attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(caps), ctypes.sizeof(caps), None, None):
            raise AppContainerError(f"security capabilities attribute failed ({ctypes.get_last_error()})")
        if not k32.UpdateProcThreadAttribute(
                attr_buf, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                handle_array, ctypes.sizeof(handle_array), None, None):
            raise AppContainerError(f"handle list attribute failed ({ctypes.get_last_error()})")

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = handles
        si.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)

        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        env_buf = ctypes.create_unicode_buffer(environment_block(env))
        pi = PROCESS_INFORMATION()
        flags = (EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW
                 | CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP)
        if not k32.CreateProcessW(None, cmdline, None, None, True, flags,
                                  env_buf, cwd, ctypes.byref(si), ctypes.byref(pi)):
            raise AppContainerError(f"CreateProcess in AppContainer failed (winerror {ctypes.get_last_error()})")
    except BaseException:
        for fd in (out_r, err_r):
            os.close(fd)
        raise
    finally:
        if attr_list is not None:
            k32.DeleteProcThreadAttributeList(attr_list)
        for fd in child_fds:
            os.close(fd)

    proc = AppContainerProcess(
        pi.hProcess, pi.hThread, pi.dwProcessId,
        os.fdopen(out_r, "rb", buffering=0), os.fdopen(err_r, "rb", buffering=0),
    )
    try:
        job.assign(pi.hProcess)
        proc.resume()
    except BaseException:
        proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
        proc.close()
        raise
    return proc
