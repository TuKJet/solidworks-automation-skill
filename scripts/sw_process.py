"""@brief SolidWorks 进程探测与同一桌面会话的排他作业锁。"""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import os


class SolidWorksHostBusyError(RuntimeError):
    """@brief 另一个进程已在同一 Windows 会话内执行 SolidWorks 自动化。"""

    code = "SW_HOST_BUSY"


_CAD_JOB_MUTEX_NAME = r"Local\cad-agent-solidworks-automation-job"
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


@contextmanager
def exclusive_cad_job():
    """@brief 非阻塞取得跨进程 SolidWorks 作业锁。

    Windows named mutex follows the owning thread's recursion semantics, so a
    nested use on the same thread is safe.  Different server threads remain
    serialized by the caller's in-process lock.  The operating system releases
    a mutex if an owning process terminates; this function never kills a
    process or touches any SolidWorks document.

    Non-Windows hosts have no SolidWorks COM desktop and deliberately act as a
    no-op so offline package tests remain portable.
    """
    if os.name != "nt":
        yield
        return

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, _CAD_JOB_MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result == _WAIT_TIMEOUT:
            raise SolidWorksHostBusyError(
                "另一个 CAD 自动化进程正在使用 SolidWorks；当前请求未执行。"
            )
        if wait_result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            raise OSError(ctypes.get_last_error(), f"WaitForSingleObject failed: {wait_result}")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def solidworks_processes():
    """@brief 返回 PID→年份；枚举失败返回 None，避免猜测进程所有权。"""
    if os.name != "nt":
        return None

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
            ("name", wintypes.WCHAR * 260)]

    api = ctypes.windll.kernel32
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    result = {}
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        valid = api.Process32FirstW(snapshot, ctypes.byref(entry))
        if not valid:
            return None
        while valid:
            if entry.name.casefold() == "sldworks.exe":
                year = None
                handle = api.OpenProcess(0x1000, False, entry.pid)
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        size = wintypes.DWORD(len(buffer))
                        if api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            import win32api
                            version = win32api.GetFileVersionInfo(buffer.value, "\\")
                            major = version["FileVersionMS"] >> 16
                            year = major if 2000 <= major <= 2035 else major + 1992 if 18 <= major <= 43 else None
                    except Exception:
                        pass
                    finally:
                        api.CloseHandle(handle)
                result[int(entry.pid)] = year
            valid = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    return result
