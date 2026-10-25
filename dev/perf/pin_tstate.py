# Through the bridge (game thread):
#   python tools/bridge.py <id> -f dev/perf/pin_tstate.py          -> pins
#   python tools/bridge.py <id> "unpin_tstate()"                    -> undoes it
#
# pyunrealsdk enters Python through pybind11's gil_scoped_acquire. The game thread has no Python
# thread state of its own, so every hook call creates one (PyThreadState_New, a VirtualAlloc'd
# data-stack chunk) and destroys it on the way out (PyThreadState_Clear, VirtualFree): measured
# ~1.3-2 ms of every frame at ~60 fps with our mods loaded. One extra PyGILState_Ensure on the
# game thread raises the state's gilstate_counter, so the outermost release never reaches 0 and the
# state lives on; pybind11 still releases the GIL after each call. PyGILState_Release undoes it.
# ruff: noqa: F821
import ctypes

_api = ctypes.pythonapi
_api.PyGILState_Ensure.restype = ctypes.c_int
_api.PyGILState_Release.argtypes = (ctypes.c_int,)
_api.PyGILState_GetThisThreadState.restype = ctypes.c_void_p


def pin_tstate():
    g = globals()
    if g.get("_pinned_state") is not None:
        return "already pinned"
    g["_pinned_state"] = _api.PyGILState_Ensure()
    return f"pinned {_api.PyGILState_GetThisThreadState():#x}"


def unpin_tstate():
    g = globals()
    st = g.get("_pinned_state")
    if st is None:
        return "not pinned"
    _api.PyGILState_Release(st)
    g["_pinned_state"] = None
    return "unpinned"


print(pin_tstate())
