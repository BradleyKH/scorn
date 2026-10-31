"""External programs (Audiveris and the like) that the server must be able to stop when it shuts down."""

import subprocess
import sys
import threading

_lock = threading.Lock()
_running: set[subprocess.Popen] = set()
_stopping = False


class Stopped(RuntimeError):
    pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _make_job():
        """A job object whose members, and everything they start, die when this process does, however it ends."""
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        job = k32.CreateJobObjectW(None, None)
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None, k32
        return job, k32

    _job, _k32 = _make_job()
else:
    _job = _k32 = None


def run(cmd: list[str], *, check: bool = False, timeout: float | None = None,
        capture_output: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for a program that stop_all() can end; raises Stopped if it was ended that way."""
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with _lock:
        if _stopping:
            raise Stopped("Scorn is shutting down")
        proc = subprocess.Popen(cmd, **kwargs)
        _running.add(proc)
        if _job:
            _k32.AssignProcessToJobObject(_job, int(proc._handle))
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    finally:
        with _lock:
            _running.discard(proc)
    if _stopping:
        raise Stopped("Scorn is shutting down")
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def stop_all() -> None:
    """End every running program, with anything it started, and refuse to start more."""
    global _stopping
    with _lock:
        _stopping = True
        procs = list(_running)
    if _job:
        _k32.TerminateJobObject(_job, 1)
    for proc in procs:
        proc.kill()
