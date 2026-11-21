"""Small file helpers shared by the collector: whole-file writes that a reader sees as old or new, never half of one."""
import os, time
from pathlib import Path


def replace_with_retry(tmp, final, attempts=8, pause=0.4):
    """os.replace with retries: on Windows a file another program (or an antivirus scan) has open cannot be replaced right now."""
    for i in range(attempts):
        try: os.replace(tmp, final); return
        except PermissionError:
            if i == attempts - 1: raise
            time.sleep(pause)


def atomic_write_bytes(path, data, attempts=8):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp{os.getpid()}')
    with open(tmp, 'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    replace_with_retry(tmp, path, attempts)


def atomic_write_text(path, text, attempts=8):
    atomic_write_bytes(path, text.encode('utf-8'), attempts)


def pid_alive(pid):
    """Is a process with this id running? (Windows: os.kill(pid, 0) would TERMINATE it there, so the process table is asked instead.)"""
    if not isinstance(pid, int) or pid <= 0: return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)       # own handle: the error code is read thread-safely with ctypes.get_last_error()
        k.OpenProcess.restype = wintypes.HANDLE; k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.restype = wintypes.BOOL; k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.CloseHandle.restype = wintypes.BOOL; k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: return ctypes.get_last_error() == 5    # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally: k.CloseHandle(h)
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    return True
