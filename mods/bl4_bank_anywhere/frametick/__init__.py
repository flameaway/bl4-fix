"""
BL4 Frame Tick: one per-frame hook for all our mods.

Source of truth: shared/frametick/ in the repo. tools/sync_shared.py copies it, byte for byte, into
every mod that ticks per frame (`<mod>/frametick/`), so each mod works on its own. Never edit a
mod's copy; edit shared/frametick/, bump VERSION, run the sync.

Why
---
The SDK costs ~4 us to call a hook, before any of the mod's code runs, and our per-frame hook
(CameraModifier:BlueprintModifyCamera POST) fires 3-6 times a frame (camera modifiers x camera
updates). ~25 mods each hooking it on their own cost ~0.6 ms a frame in calls that only decide
"not a new frame yet" (Mod Profiler, 2026-10-29). Here one hook decides once, confirms the new
frame with the engine's frame counter, and calls each mod's callback once that frame.

Many copies, one hook
---------------------
All our mods run in one Python. The copies share one state object, `sys.modules[HUB]` (a plain
module object): the callbacks and the frame state. The newest copy (VERSION) owns the hook; a
newer copy (or a hot reload of the owner) takes it over, callbacks kept. Shared fields are only
ever added, never renamed or given a new meaning, so any copy can read what an older one left.
The hook is only there while at least one callback is: nothing runs when no mod wants a tick.

From a mod:

    from . import frametick

    @frametick.tick("bl4_builds")            # instead of @hook(TICK_FUNC, Type.POST)
    def _tick() -> None: ...                 # no arguments; once per engine frame

    @frametick.tick("bl4_x_slow", every=0.25)  # at most every 0.25 s (skipped frames cost nothing)
    def _slow() -> None: ...

    mod = build_mod(hooks=[_tick, _slow, ...])   # enabled / disabled with the mod, like a hook

    frametick.add(key, fn, every=0.0) / frametick.remove(key)   # without mods_base
    frametick.soon(key)  # an every= callback runs at the next frame (a setting changed)
    frametick.frame()   # the engine frame number this tick (GetFrameCount), for dedupe
    frametick.now()     # time.perf_counter() at this frame's tick

Callbacks run on the game thread, in the order they were added, each inside its own try/except
(an error is logged once per distinct message and doesn't stop the others). A mod that needs the
hook's arguments, or every camera update rather than every frame, keeps its own hook.
"""

from __future__ import annotations

import ctypes
import sys
import time
import traceback
import types
from collections.abc import Callable
from typing import Any

from mods_base import HookType
from unrealsdk import find_class, find_object, logging
from unrealsdk.hooks import Type, add_hook, has_hook, remove_hook

VERSION = 2
HUB = "_bl4_frametick"
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
HOOK_ID = "bl4_frametick"
# A new frame: the first call after this gap (the calls of one frame come close together: measured
# 0.3 ms median, 0.9 ms worst inside a frame), or this long since the last tick; then the engine's
# frame counter confirms it, so a slow frame's second camera update never ticks twice
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
MAX_ERRORS = 64

# v2: the frame counter read straight from memory. GetFrameCount is a UFunction call, and pyunrealsdk
# lets go of the GIL for those: a worker thread waiting for it takes it, and the game thread waits
# to get it back (1.2 us in a loop, 28.5 us a frame in the dispatcher, measured 2026-10-29). Its
# native body reads one global, GFrameCounter; that address comes from the instruction itself and
# is used only if the code matches byte for byte, the address is inside the game's image, and the
# value equals GetFrameCount's answer. Otherwise (a game patch) it stays on GetFrameCount.
COUNTER_FUNC = "/Script/Engine.KismetSystemLibrary:GetFrameCount"
FUNC_NATIVE = 0xD8  # UFunction::Func
# execGetFrameCount (UE 5.5): P_FINISH; *(int64*)RESULT_PARAM = GFrameCounter; return
_EXEC_HEAD = bytes.fromhex("488b42204883f8014883d8ff48894220" "488b05")  # ... mov rax, [rip+disp32]
_EXEC_TAIL = bytes.fromhex("498900c3")  # mov [r8], rax; ret

_STAMP = object()


# region Shared state


def _hub() -> types.ModuleType:
    """The state every copy shares (created by the first one)."""
    hub = sys.modules.get(HUB)
    if hub is None:
        hub = types.ModuleType(HUB)
        hub.__doc__ = "BL4 Frame Tick state shared by every mod's copy of frametick (see its docstring)."
        hub.entries = {}  # key -> SimpleNamespace(key, fn, every, next_at, mod)
        hub.order = ()  # the entries, as a tuple the dispatcher can walk while one is added
        hub.owner = None  # the copy whose dispatcher is hooked (a module object)
        hub.owner_stamp = None
        hub.owner_version = 0
        hub.hooked = False
        hub.last_call = 0.0  # perf_counter of the hook's last call
        hub.ticked_at = 0.0  # ... of the last call that checked for a new frame
        hub.now = 0.0  # ... of this frame's tick
        hub.frame = -1  # engine frame number of the last tick
        hub.frame_count = None  # bound KismetSystemLibrary.GetFrameCount
        hub.errors = set()
        hub.ticks = 0
        sys.modules[HUB] = hub
    if not hasattr(hub, "frame_ptr"):  # v2 (a v1 copy may have made the hub)
        hub.frame_ptr = None  # ctypes.c_uint64 over GFrameCounter, once found and checked
        hub.frame_ptr_tried = False
    return hub


def _me() -> types.ModuleType:
    return sys.modules[__name__]


def _elect(hub: types.ModuleType) -> None:
    """Makes this copy the owner if it's newer (or a hot reload of the owner), and re-hooks."""
    owner = hub.owner
    if owner is _me() and hub.owner_stamp is _STAMP:
        return
    if owner is not None and owner is not _me() and getattr(hub, "owner_version", 0) >= VERSION:
        return
    hub.owner, hub.owner_stamp, hub.owner_version = _me(), _STAMP, VERSION
    if hub.hooked:
        _hook(hub)  # the newer dispatcher replaces the old one under the same identifier


def _hook(hub: types.ModuleType) -> None:
    remove_hook(TICK_FUNC, Type.POST, HOOK_ID)
    add_hook(TICK_FUNC, Type.POST, HOOK_ID, hub.owner._dispatch)  # noqa: SLF001
    hub.hooked = True


def _sync_hook(hub: types.ModuleType) -> None:
    """Hooked while there are callbacks, unhooked when there are none."""
    if hub.entries:
        owner = hub.owner
        if owner is None:
            _elect(hub)
        if not hub.hooked or not has_hook(TICK_FUNC, Type.POST, HOOK_ID):
            _hook(hub)
    elif hub.hooked:
        remove_hook(TICK_FUNC, Type.POST, HOOK_ID)
        hub.hooked = False


# endregion
# region Registering (any copy, any thread)


def add(key: str, fn: Callable[[], Any], every: float = 0.0, mod: str = "") -> None:
    """Calls fn() once a frame (or at most every `every` seconds) until remove(key). Replaces a key."""
    hub = _hub()
    old = hub.entries.get(key)
    hub.entries[key] = types.SimpleNamespace(
        key=key, fn=fn, every=float(every), next_at=0.0 if old is None else old.next_at, mod=mod or _package()
    )
    hub.order = tuple(hub.entries.values())
    _elect(hub)
    _sync_hook(hub)


def remove(key: str) -> None:
    hub = _hub()
    if hub.entries.pop(key, None) is not None:
        hub.order = tuple(hub.entries.values())
    _sync_hook(hub)


def soon(key: str) -> None:
    """Runs an `every=` callback at the next frame (a setting changed), then at its pace again."""
    entry = _hub().entries.get(key)
    if entry is not None:
        entry.next_at = 0.0


def frame() -> int:
    """The engine frame number of this tick (-1 before the first)."""
    return _hub().frame


def now() -> float:
    """time.perf_counter() at this frame's tick."""
    return _hub().now


def _package() -> str:
    return __name__.rpartition(".")[0] or __name__


class FrameTick(HookType):
    """A per-frame callback that mods_base enables and disables with the mod, like a hook."""

    every: float = 0.0

    def enable(self) -> None:
        add(self.hook_identifier, self.__wrapped__, self.every)

    def disable(self) -> None:
        remove(self.hook_identifier)

    def get_active_count(self) -> int:
        return int(self.hook_identifier in _hub().entries)


def tick(key: str, every: float = 0.0) -> Callable[[Callable[[], Any]], FrameTick]:
    """Decorator: a FrameTick for build_mod's hooks list. `key` must be unique among our mods."""

    def decorator(fn: Callable[[], Any]) -> FrameTick:
        ft = FrameTick(fn, key, [])  # no hook_funcs: mods_base and the profiler hook nothing for it
        ft.every = float(every)
        return ft

    return decorator


# endregion
# region The hook (owner's copy only)


def _find_counter(expected: int) -> Any:
    """GFrameCounter as a ctypes.c_uint64 (see COUNTER_FUNC), or None if anything doesn't check out."""
    try:
        k32 = ctypes.WinDLL("kernel32")  # our own object: shared windll prototypes stay untouched
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        base = k32.GetModuleHandleW(None)
        if not base or ctypes.string_at(base, 2) != b"MZ":
            return None
        pe = base + ctypes.c_uint32.from_address(base + 0x3C).value
        if ctypes.string_at(pe, 4) != b"PE\0\0":
            return None
        end = base + ctypes.c_uint32.from_address(pe + 0x50).value  # SizeOfImage

        def inside(address: int, size: int) -> bool:
            return base <= address and address + size <= end

        code = ctypes.c_uint64.from_address(find_object("Function", COUNTER_FUNC)._get_address() + FUNC_NATIVE).value
        size = len(_EXEC_HEAD) + 4 + len(_EXEC_TAIL)
        if not inside(code, size):
            return None
        raw = ctypes.string_at(code, size)
        if not raw.startswith(_EXEC_HEAD) or not raw.endswith(_EXEC_TAIL):
            return None
        disp = int.from_bytes(raw[len(_EXEC_HEAD) : len(_EXEC_HEAD) + 4], "little", signed=True)
        address = code + len(_EXEC_HEAD) + 4 + disp
        if not inside(address, 8):
            return None
        ptr = ctypes.c_uint64.from_address(address)
        return ptr if ptr.value == expected else None
    except Exception:  # noqa: BLE001 - stay on GetFrameCount
        return None


def _dispatch(_obj: Any, _args: Any, _ret: Any, _func: Any, hub: types.ModuleType = _hub()) -> None:
    t = time.perf_counter()
    gap = t - hub.last_call
    hub.last_call = t
    if gap < FRAME_GAP_S and t - hub.ticked_at < MAX_WAIT_S:
        return
    hub.ticked_at = t
    ptr = hub.frame_ptr
    if ptr is not None:
        n = ptr.value
    else:
        fc = hub.frame_count
        try:
            if fc is None:
                fc = hub.frame_count = find_class("KismetSystemLibrary").ClassDefaultObject.GetFrameCount
            n = int(fc())
        except Exception:  # noqa: BLE001 - no counter: the time gate alone decides
            n = hub.frame + 1
        else:
            if not hub.frame_ptr_tried:
                hub.frame_ptr_tried = True
                hub.frame_ptr = _find_counter(n)
    if n == hub.frame:
        return
    hub.frame = n
    hub.now = t
    hub.ticks += 1
    for e in hub.order:
        if e.every:
            if t < e.next_at:
                continue
            e.next_at = t + e.every
        try:
            e.fn()
        except Exception as ex:  # noqa: BLE001 - one mod's error mustn't stop the others
            sig = f"{e.key}: {ex!r}"
            if sig not in hub.errors and len(hub.errors) < MAX_ERRORS:
                hub.errors.add(sig)
                logging.error(f"[Frame Tick] {e.key} ({e.mod}) failed:")
                logging.error("".join(traceback.format_exception(ex)))


# endregion
