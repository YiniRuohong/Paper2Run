"""Windows Job Object containment for the sandbox guest (ctypes, no deps).

The guest is launched ``CREATE_SUSPENDED``, assigned to a fresh Job Object
and only then resumed, so every limit applies from its first instruction and
no grandchild can be spawned outside the job. The job enforces:

* ``JobMemoryLimit`` - total *committed* memory of every process in the job
  (stricter than an RSS monitor: allocations fail with ``MemoryError``);
* ``ActiveProcessLimit`` - process-count cap (``CreateProcess`` fails with
  ``ERROR_NOT_ENOUGH_QUOTA`` beyond it);
* ``PerJobUserTimeLimit`` - CPU-time cap for the whole tree;
* ``KILL_ON_JOB_CLOSE`` - closing the handle (also on supervisor death)
  terminates the whole tree; ``DIE_ON_UNHANDLED_EXCEPTION`` suppresses WER
  dialogs;
* UI restrictions - no clipboard, global atoms, desktop switching, display
  or system-parameter changes, no ``ExitWindows``, no foreign USER handles.

Not provided here (documented gaps): network denial and filesystem write
confinement - those need an AppContainer / restricted token.

Importable on every platform; all Win32 calls are resolved lazily.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

JobObjectBasicAccountingInformation = 1
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9

JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: HANDLES|READCLIPBOARD|WRITECLIPBOARD|SYSTEMPARAMETERS|DISPLAYSETTINGS|
#: GLOBALATOMS|DESKTOP|EXITWINDOWS
JOB_OBJECT_UILIMIT_ALL = 0x000000FF

CREATE_SUSPENDED = 0x00000004
#: Explicit 32-bit DWORD: wintypes.DWORD is c_ulong, 8 bytes on LP64 hosts,
#: which would break the pure layout tests off Windows.
_DWORD = ctypes.c_uint32


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", _DWORD)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


def extended_limits(
    *, memory_bytes: int, max_processes: int, cpu_seconds: int
) -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    """Pure: the limit struct for one guest (unit-tested on every OS)."""
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    basic = info.BasicLimitInformation
    basic.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_JOB_TIME
    )
    basic.ActiveProcessLimit = max_processes
    basic.PerJobUserTimeLimit = cpu_seconds * 10_000_000  # 100 ns ticks
    info.JobMemoryLimit = memory_bytes
    return info


class JobError(OSError):
    """A Job Object call failed; the guest must not run uncontained."""


_k32 = None
_ntdll = None


def _kernel32():
    global _k32, _ntdll
    if _k32 is None:
        if sys.platform != "win32":
            raise JobError("Job Objects exist only on Windows")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        nt = ctypes.WinDLL("ntdll")
        nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
        nt.NtResumeProcess.restype = ctypes.c_long
        _k32, _ntdll = k32, nt
    return _k32


def _check(ok, what: str) -> None:
    if not ok:
        raise JobError(f"{what} failed (winerror {ctypes.get_last_error()})")


class GuestJob:
    """One Job Object owning one guest process tree."""

    def __init__(self, *, memory_bytes: int, max_processes: int, cpu_seconds: int):
        k32 = _kernel32()
        handle = k32.CreateJobObjectW(None, None)
        _check(handle, "CreateJobObject")
        self.handle = handle
        self.memory_bytes = memory_bytes
        try:
            info = extended_limits(
                memory_bytes=memory_bytes,
                max_processes=max_processes,
                cpu_seconds=cpu_seconds,
            )
            _check(k32.SetInformationJobObject(
                handle, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)),
                "SetInformationJobObject(limits)")
            ui = JOBOBJECT_BASIC_UI_RESTRICTIONS(JOB_OBJECT_UILIMIT_ALL)
            _check(k32.SetInformationJobObject(
                handle, JobObjectBasicUIRestrictions,
                ctypes.byref(ui), ctypes.sizeof(ui)),
                "SetInformationJobObject(ui)")
        except BaseException:
            self.close()
            raise

    def adopt_suspended(self, process_handle: int) -> None:
        """Assign a ``CREATE_SUSPENDED`` process, then resume it."""
        k32 = _kernel32()
        _check(k32.AssignProcessToJobObject(self.handle, process_handle),
               "AssignProcessToJobObject")
        status = _ntdll.NtResumeProcess(process_handle)
        if status < 0:
            raise JobError(f"NtResumeProcess failed (NTSTATUS {status & 0xFFFFFFFF:#x})")

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle:
            _kernel32().TerminateJobObject(self.handle, exit_code)

    def active_processes(self) -> int | None:
        if not self.handle:
            return None
        acct = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ok = _kernel32().QueryInformationJobObject(
            self.handle, JobObjectBasicAccountingInformation,
            ctypes.byref(acct), ctypes.sizeof(acct), None)
        return int(acct.ActiveProcesses) if ok else None

    def peak_memory(self) -> int | None:
        if not self.handle:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ok = _kernel32().QueryInformationJobObject(
            self.handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info), None)
        return int(info.PeakJobMemoryUsed) if ok else None

    def close(self) -> None:
        """Close the job; KILL_ON_JOB_CLOSE reaps any survivor."""
        if self.handle:
            _kernel32().CloseHandle(self.handle)
            self.handle = None
