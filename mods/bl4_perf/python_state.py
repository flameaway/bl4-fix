"""
Cheaper Python hooks: keeps one Python thread state alive on the game thread.

pyunrealsdk enters Python through pybind11's gil_scoped_acquire. The game thread never had a Python
thread state of its own, so every hook call created one (PyThreadState_New, a VirtualAlloc'd
data-stack chunk, a fresh threading._DummyThread) and destroyed it again on the way out
(PyThreadState_Clear, VirtualFree). Measured with dev/perf/sampler.py: ~1-2 ms of every frame at
~65 fps with our mods loaded; ~113,000 thread states in a 12-hour session. One extra
PyGILState_Ensure on the game thread raises that state's gilstate_counter, so the outermost release
never reaches zero and the state lives on; pybind11 still releases the GIL after each call.

Also lowers CPython's GIL switch interval from 5 ms to 0.5 ms: when a worker thread holds the GIL
and the game thread wants it back (after any ctypes call, or at hook entry), the game thread waits
at most that long. Measured: game-thread GIL waits 1.4 -> 1.0 ms/frame with a busy page open.

Both are undone on disable: PyGILState_Release must run on the thread that pinned, so it happens on
the next game-thread tick when disable comes from another thread.
"""

from __future__ import annotations

import ctypes
import sys
import threading

_api = ctypes.pythonapi
_api.PyGILState_Ensure.restype = ctypes.c_int
_api.PyGILState_Ensure.argtypes = ()
_api.PyGILState_Release.restype = None
_api.PyGILState_Release.argtypes = (ctypes.c_int,)

SWITCH_INTERVAL = 0.0005


class _State:
    pinned: int | None = None  # PyGILState_STATE from our Ensure
    thread: int | None = None  # ident of the thread that pinned (the game thread)
    old_interval: float | None = None


def pin_here() -> bool:
    """Call on the game thread (inside a hook). Pins once; returns True if it pinned now."""
    if _State.pinned is not None:
        return False
    _State.pinned = _api.PyGILState_Ensure()
    _State.thread = threading.get_ident()
    if _State.old_interval is None:
        _State.old_interval = sys.getswitchinterval()
        sys.setswitchinterval(SWITCH_INTERVAL)
    return True


def is_pinned() -> bool:
    return _State.pinned is not None


def on_pinned_thread() -> bool:
    return _State.thread == threading.get_ident()


def unpin_here() -> bool:
    """Call on the thread that pinned. Returns True if it released our extra reference."""
    if _State.pinned is None or not on_pinned_thread():
        return False
    _api.PyGILState_Release(_State.pinned)
    _State.pinned = None
    _State.thread = None
    if _State.old_interval is not None:
        sys.setswitchinterval(_State.old_interval)
        _State.old_interval = None
    return True
