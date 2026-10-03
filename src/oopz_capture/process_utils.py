from __future__ import annotations

import ctypes
import os
import errno


def valid_lock_pid(value: object) -> bool:
    """Only positive native PID integers are safe to use for stale-lock recovery."""
    return type(value) is int and 0 < value <= 2_147_483_647


def pid_is_running(pid: int) -> bool:
    """Check process existence without signaling or modifying the process."""
    if not valid_lock_pid(pid):
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied is not evidence that the owner exited.
            return ctypes.get_last_error() != 87
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError as error:
        # Only ESRCH proves absence; EPERM and unknown errors must block recovery.
        return error.errno != errno.ESRCH
