"""
Keep the game's memory in RAM: a soft minimum working set for the game process.

When the PC runs short of free memory, Windows trims the game's working set (seen: 3.2 -> 2.0 GB
in one step while playing), and the game then stalls on hard page faults as it touches those pages
again: the sampler caught the game thread parked for 274 ms on a two-instruction function whose
code page wasn't resident. A minimum working set tells the memory manager to take memory from other
processes first. It's a soft limit (QUOTA_LIMITS_HARDWS_MIN_DISABLE): under real memory pressure
Windows can still go below it, nothing is locked, and commit doesn't change.

The game process may raise its own working set limits (SeIncreaseWorkingSetPrivilege, which normal
users hold). The previous minimum is read first and put back on disable.

Never pass a maximum below the current working set: SetProcessWorkingSetSizeEx trims down to it at
once, soft limits or not. Writing back the default limits (min 200 KB, max 1.3 MB) took the game
from 6.3 to 0.9 GB resident in one call (2026-10-24), and every page then had to fault back in.
So the maximum we set (and leave on restore) is at least the current working set; it's a soft
maximum, which Windows doesn't enforce.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetCurrentProcess = _k32.GetCurrentProcess
_GetCurrentProcess.restype = w.HANDLE
_GetCurrentProcess.argtypes = ()
_Get = _k32.GetProcessWorkingSetSizeEx
_Get.argtypes = (w.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
                 ctypes.POINTER(w.DWORD))
_Get.restype = w.BOOL
_Set = _k32.SetProcessWorkingSetSizeEx
_Set.argtypes = (w.HANDLE, ctypes.c_size_t, ctypes.c_size_t, w.DWORD)
_Set.restype = w.BOOL


class _PMC(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


_GetPMI = _k32.K32GetProcessMemoryInfo
_GetPMI.argtypes = (w.HANDLE, ctypes.POINTER(_PMC), w.DWORD)
_GetPMI.restype = w.BOOL

HARDWS_MIN_DISABLE = 0x2
HARDWS_MAX_DISABLE = 0x8
GB = 1 << 30


class _State:
    original: tuple[int, int, int] | None = None  # (min, max, flags) before we changed them
    applied_gb: float = 0.0


def current() -> tuple[int, int, int]:
    mn, mx, fl = ctypes.c_size_t(), ctypes.c_size_t(), w.DWORD()
    if not _Get(_GetCurrentProcess(), ctypes.byref(mn), ctypes.byref(mx), ctypes.byref(fl)):
        raise OSError(ctypes.get_last_error(), "GetProcessWorkingSetSizeEx")
    return mn.value, mx.value, fl.value


def working_set() -> int:
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)
    if not _GetPMI(_GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo")
    return pmc.WorkingSetSize


def _safe_max(*floors: int) -> int:
    """A maximum that can't trim: above the current working set (plus room to grow)."""
    return max(working_set() + GB, *floors)


def apply(min_gb: float) -> str:
    """Sets a soft minimum working set of min_gb (0 = put the original limits back)."""
    if min_gb <= 0:
        return restore()
    if _State.original is None:
        _State.original = current()
    mn = int(min_gb * GB)
    mx = _safe_max(_State.original[1], mn + GB)
    if not _Set(_GetCurrentProcess(), mn, mx, HARDWS_MIN_DISABLE | HARDWS_MAX_DISABLE):
        return f"SetProcessWorkingSetSizeEx failed ({ctypes.get_last_error()})"
    _State.applied_gb = min_gb
    return f"minimum working set {min_gb:g} GB"


def restore() -> str:
    if _State.original is None:
        return "unchanged"
    mn, mx, fl = _State.original
    # the old minimum and flags, but a maximum that doesn't trim (see the module docstring)
    ok = _Set(_GetCurrentProcess(), mn, _safe_max(mx), fl or (HARDWS_MIN_DISABLE | HARDWS_MAX_DISABLE))
    _State.original = None
    _State.applied_gb = 0.0
    return "restored" if ok else f"restore failed ({ctypes.get_last_error()})"
