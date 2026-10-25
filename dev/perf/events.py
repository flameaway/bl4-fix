# Logs game-state changes with QueryPerformanceCounter timestamps (time.perf_counter_ns is QPC on
# Windows, the same clock the sampler uses), so analyze.py --events can line hitches up with them.
#   python tools/bridge.py <id> -f dev/perf/events.py              -> starts (10 Hz check, ~20 us)
#   python tools/bridge.py <id> "print(events_dump())"              -> writes .perf/events.json
#   python tools/bridge.py <id> "events_stop()"
# ruff: noqa: F821
import json
import os
import time

from mods_base import ENGINE, get_pc
from unrealsdk import hooks

_EV_HOOK = "/Script/Engine.CameraModifier:BlueprintModifyPostProcess"
_EV_ID = "perf_events"
_GS = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_KSL = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
EVENTS = globals().setdefault("EVENTS", [])
_ev_state = {"last": None, "t": 0.0}
import ctypes as _ct
_u32 = _ct.WinDLL("user32")
_u32.GetForegroundWindow.restype = _ct.c_void_p
_u32.GetWindowThreadProcessId.argtypes = (_ct.c_void_p, _ct.POINTER(_ct.c_ulong))
_MY_PID = _ct.WinDLL("kernel32").GetCurrentProcessId()


def _game_in_front():
    pid = _ct.c_ulong()
    _u32.GetWindowThreadProcessId(_u32.GetForegroundWindow(), _ct.byref(pid))
    return pid.value == _MY_PID


def _ev_snapshot():
    pc = get_pc()
    w = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    return {
        "world": w.Name if w else None,
        "cursor": bool(pc.bShowMouseCursor) if pc else None,
        "cinematic": str(pc.ActiveCinematicModeIdent)[:80] if pc else None,
        "hud": bool(pc.MyHUD.bShowHUD) if pc and pc.MyHUD else None,
        "paused": bool(_GS.IsGamePaused(w)) if w else None,
        "pawn": bool(pc.Pawn) if pc else None,
        "front": _game_in_front(),
    }


def _ev_tick(obj, args, ret, func):
    try:
        now = time.perf_counter()
        if now - _ev_state["t"] < 0.1:
            return
        _ev_state["t"] = now
        snap = _ev_snapshot()
        if snap != _ev_state["last"]:
            _ev_state["last"] = snap
            EVENTS.append((time.perf_counter_ns(), _KSL.GetFrameCount(), snap))
            del EVENTS[:-5000]
    except Exception as e:  # noqa: BLE001
        EVENTS.append((time.perf_counter_ns(), 0, {"error": repr(e)[:100]}))


def events_stop():
    hooks.remove_hook(_EV_HOOK, hooks.Type.POST, _EV_ID)


def events_mark(label):
    EVENTS.append((time.perf_counter_ns(), _KSL.GetFrameCount(), {"mark": label}))


def events_dump():
    path = os.path.join(r"C:\Users\Josh\Documents\projects\bl4-direct\.perf", "events.json")
    with open(path + ".tmp", "w") as f:
        json.dump(EVENTS, f)
    os.replace(path + ".tmp", path)
    return f"{len(EVENTS)} events -> {path}"


hooks.remove_hook(_EV_HOOK, hooks.Type.POST, _EV_ID)
hooks.add_hook(_EV_HOOK, hooks.Type.POST, _EV_ID, _ev_tick)
print("events on", _ev_snapshot())
