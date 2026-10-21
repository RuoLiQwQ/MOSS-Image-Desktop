"""Keep launcher-owned Windows subprocesses in a kill-on-close job.

Only children are assigned; the launcher itself must remain outside this job.
The unnamed job handle is non-inheritable, so it is closed by Windows even if
the launcher exits without reaching its finally block. Non-Windows hosts use
a no-op implementation to allow the rest of the installer to be tested.

Reference: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
from __future__ import annotations

import ctypes
import os
import threading


_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_BOOL = ctypes.c_int32
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _BasicLimitInformation(ctypes.Structure):
    # Windows LARGE_INTEGER is signed 64-bit; SIZE_T and ULONG_PTR follow the
    # pointer size. Native alignment supplies the required x64 padding.
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


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32 = None
if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.CreateJobObjectW.restype = _HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        _HANDLE, ctypes.c_int32, ctypes.c_void_p, _DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = _BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
    _kernel32.AssignProcessToJobObject.restype = _BOOL
    _kernel32.CloseHandle.argtypes = [_HANDLE]
    _kernel32.CloseHandle.restype = _BOOL


def _last_error(operation):
    code = ctypes.get_last_error()
    return ctypes.WinError(code, f"{operation}: {ctypes.FormatError(code).strip()}")


class ChildJob:
    """Own a Windows job containing only explicitly added subprocesses.

    Call add immediately after Popen and close in the launcher's finally block.
    Descendants created after assignment join the job automatically. Assignment
    failures are reported to the caller; the caller still owns that subprocess
    and must terminate it if it cannot safely continue.
    """

    def __init__(self):
        self._handle = None
        self._closed = False
        self._lock = threading.Lock()
        if _kernel32 is None:
            return

        # NULL security attributes make the handle non-inheritable. An unnamed
        # job also prevents accidental sharing with another launcher instance.
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _last_error("CreateJobObjectW")
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = _last_error("SetInformationJobObject")
            _kernel32.CloseHandle(handle)
            raise error
        self._handle = handle

    def add(self, process):
        """Assign a subprocess.Popen process; raise if assignment is unsuccessful."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot add a process to a closed child job.")
            if _kernel32 is None:
                return
            if process.pid == os.getpid():
                raise ValueError("The launcher itself must not enter its child job.")
            # Popen owns this process handle. Borrow it without closing it; a PID
            # is not a handle and reopening by PID would introduce a reuse race.
            handle = int(process._handle)
            if not _kernel32.AssignProcessToJobObject(self._handle, handle):
                raise _last_error("AssignProcessToJobObject")

    def close(self):
        """Close once, terminating any remaining job members and descendants."""
        with self._lock:
            if self._closed:
                return
            if self._handle is not None:
                if not _kernel32.CloseHandle(self._handle):
                    raise _last_error("CloseHandle")
                self._handle = None
            self._closed = True
