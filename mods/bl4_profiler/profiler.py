"""
The measurement: what each loaded mod costs the game, over one run of a few seconds.

For the run, every active hook of every enabled mod (anything registered through mods_base, so
other modders' mods too) is swapped for a timing wrapper under the same identifier, and put back
afterwards. A wrapper returns what the hook returned (Blocks keep working) and lets its exceptions
through to the SDK as before. A few hooks of our own mods are added with unrealsdk.hooks directly
(DIRECT below); they're timed too. Hooks other mods add that way can't be listed (the SDK has no
way to enumerate hooks), so they're not seen.

Besides the hooks: each Python worker thread's CPU time, put on the mod whose module started it;
frame times (exact engine frames, KismetSystemLibrary.GetFrameCount); Python garbage collections;
and for the slowest frames, how much of them was mod Python and whose.

Game thread only (start, frame hook, finish); the wrappers run wherever the hooks run.
"""

from __future__ import annotations

import ctypes
import gc
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

import unrealsdk
from unrealsdk import hooks

FRAME_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyPostProcess"
FRAME_ID = "bl4_profiler.frame"
# Calls of the frame hook closer than this belong to one camera update: only the first asks the
# engine for its frame number (~10 us a call)
SAME_UPDATE_S = 0.0005
# Kept per run: enough for 60 s at 300 fps
MAX_FRAMES = 20000
# A single hook call over this is a visible hitch (root CLAUDE.md, "Performance")
SPIKE_S = 0.003
HITCH_S = 0.005
# Calls over this are kept one by one, to tell a mod's own slow calls from shared ones: a GC pause
# that ran inside the call, or a frame where SHARED_MODS or more mods were slow at once (the game
# thread waiting for the GIL a worker thread held, or the engine stalling under every hook)
SLOW_S = 0.001
SHARED_MODS = 3
MAX_SLOW = 5000
# Two wrapped hooks of the same function this close together ran back to back in one call: the gap
# is the SDK's own cost of calling the next hook (finding it, wrapping the arguments, entering
# Python), which no wrapper can time from inside
DISPATCH_GAP_S = 0.0002

# Hooks our own mods add with unrealsdk.hooks directly: (module, attribute, function or the
# module attribute naming it, type, identifier)
DIRECT = (
    ("bl4_direct.ui", "_on_tick", "TICK_FUNC", "POST", "bl4_direct.ui"),
    ("bl4_mod_menu.menu", "_on_tick", "TICK_FUNC", "POST", "bl4_mod_menu.menu"),
    ("bl4_direct.coop", "_chat_hook", "CHAT_FUNC", "PRE", "bl4_coop_chat"),
    ("bl4_direct.coop", "_message_hook", "MESSAGE_FUNC", "PRE", "bl4_coop_message"),
    ("bl4_direct.coop", "_tick_hook", "TICK_FUNC", "POST", "bl4_coop_tick"),
)


def mod_list() -> list[Any]:
    import mods_base

    ml = mods_base.mod_list
    return list(getattr(ml, "mod_list", ml))


@dataclass
class HookStat:
    mod: str
    label: str
    calls: int = 0
    total: float = 0.0
    worst: float = 0.0
    over_5ms: int = 0
    small_worst: float = 0.0  # the slowest call under SLOW_S


@dataclass
class Installed:
    func: str
    kind: Any
    ident: str
    original: Callable[..., Any]
    wrapper: Callable[..., Any]
    module: str
    attr: str  # set for DIRECT hooks (the module attribute to compare after a reload)


@dataclass
class Run:
    started: float = 0.0
    length: float = 20.0
    frame0: int = 0
    frame_no: int = -1
    frame_start: float = 0.0
    last_call: float = 0.0
    # This frame's mod Python so far, total and per mod
    frame_py: float = 0.0
    frame_by_mod: dict[str, float] = field(default_factory=dict)
    # (frame seconds, mod Python seconds, top mod, its seconds) per finished frame
    frames: list[tuple[float, float, str, float]] = field(default_factory=list)
    # (frame number, seconds into the run, mod, hook, seconds) of each call over SLOW_S
    slow: list[tuple[int, float, str, str, float]] = field(default_factory=list)
    gc_start: float = 0.0
    # (generation, seconds into the run, seconds)
    gcs: list[tuple[int, float, float]] = field(default_factory=list)
    stats: dict[str, HookStat] = field(default_factory=dict)
    installed: list[Installed] = field(default_factory=list)
    threads0: dict[tuple[int, str], float] = field(default_factory=dict)
    # thread -> mod name, decided at start (and for threads that appear later, at the end)
    packages: dict[str, str] = field(default_factory=dict)
    mods_seen: list[str] = field(default_factory=list)
    mods_off: int = 0
    overhead_s: float = 0.0  # one wrapper's own cost per call, measured at start
    frame_hook_s: float = 0.0  # time spent in our own frame hook
    # Back-to-back hooks: when the last wrapper ended, on which function; the gaps measured
    last_end: float = 0.0
    last_func: str = ""
    dispatch_s: float = 0.0
    dispatch_n: int = 0
    notes: list[str] = field(default_factory=list)
    world: str = ""


class R:
    run: Run | None = None
    ks: Any = None


# region Threads

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_OpenThread = _k32.OpenThread
_OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_OpenThread.restype = wintypes.HANDLE
_GetThreadTimes = _k32.GetThreadTimes
_GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_GetThreadTimes.restype = wintypes.BOOL
_CloseHandle = _k32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL
THREAD_QUERY_LIMITED_INFORMATION = 0x0800


def _filetime_s(ft: wintypes.FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def _thread_cpu(game_thread: int) -> dict[tuple[int, str], float]:
    """CPU seconds of every live Python thread but the game thread (its time is the game's too)."""
    out = {}
    for t in threading.enumerate():
        tid = getattr(t, "native_id", None)
        if not tid or tid == game_thread:
            continue
        h = _OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
        if not h:
            continue
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if _GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                out[(tid, t.name)] = _filetime_s(k) + _filetime_s(u)
        finally:
            _CloseHandle(h)
    return out


def _thread_module(t: threading.Thread) -> str:
    """The module that started a thread: its target's, or its class's for a Thread subclass."""
    target = getattr(t, "_target", None)
    module = getattr(target, "__module__", None) if target is not None else None
    if not module and type(t) is not threading.Thread:
        module = type(t).__module__
    return module or ""


# endregion
# region Which mod is which package


def _packages(mods: list[Any]) -> dict[str, str]:
    """Top-level package name -> mod name, from each mod's hooks, settings file and module globals."""
    out: dict[str, str] = {}
    for mod in mods:
        for h in getattr(mod, "hooks", ()) or ():
            module = getattr(getattr(h, "__wrapped__", None), "__module__", None)
            if module:
                out.setdefault(module.partition(".")[0], mod.name)
        settings = getattr(mod, "settings_file", None)
        if settings is not None and settings.stem in sys.modules:
            out.setdefault(settings.stem, mod.name)
    # A mod built with build_mod is a global of its package (usually `mod`)
    wanted = {id(m): m.name for m in mods}
    for name, module in list(sys.modules.items()):
        if "." in name or name in out or module is None:
            continue
        try:
            for value in vars(module).values():
                if id(value) in wanted:
                    out[name] = wanted[id(value)]
                    break
        except TypeError:
            continue
    return out


# endregion
# region Wrappers


def _timed(stat: HookStat, fn: Callable[..., Any], hooked: str = "") -> Callable[..., Any]:
    perf = time.perf_counter
    mod = stat.mod

    def wrapper(obj: Any, args: Any, ret: Any, func: Any) -> Any:
        start = perf()
        result = fn(obj, args, ret, func)  # its exceptions reach the SDK as before
        end = perf()
        took = end - start
        try:
            run = R.run
            if run is not None:
                gap = start - run.last_end
                if run.last_func is hooked and 0.0 <= gap < DISPATCH_GAP_S:
                    run.dispatch_s += gap
                    run.dispatch_n += 1
                run.last_end, run.last_func = end, hooked
            stat.calls += 1
            stat.total += took
            if took > stat.worst:
                stat.worst = took
            if took > HITCH_S:
                stat.over_5ms += 1
            if run is not None:
                run.frame_py += took
                run.frame_by_mod[mod] = run.frame_by_mod.get(mod, 0.0) + took
                if took > SLOW_S:
                    if len(run.slow) < MAX_SLOW:
                        run.slow.append((run.frame_no, start - run.started, mod, stat.label, took))
                elif took > stat.small_worst:
                    stat.small_worst = took
        except Exception:  # noqa: BLE001, S110 - the timing must never break the hook (or its Block)
            pass
        return result

    return wrapper


def _calibrate() -> float:
    """What a wrapper adds per call, so the report can say how much the run itself cost."""
    stat = HookStat("", "")

    def noop(*_: Any) -> None:
        return None

    wrapped = _timed(stat, noop)
    saved, R.run = R.run, None
    try:
        n = 2000
        t0 = time.perf_counter()
        for _ in range(n):
            noop(None, None, None, None)
        bare = time.perf_counter() - t0
        t0 = time.perf_counter()
        for _ in range(n):
            wrapped(None, None, None, None)
        return max(0.0, (time.perf_counter() - t0 - bare) / n)
    finally:
        R.run = saved


def _install(run: Run, func: str, kind: Any, ident: str, fn: Callable[..., Any], mod: str, label: str, module: str = "", attr: str = "") -> None:
    stat = run.stats.setdefault(f"{mod}\x00{label}", HookStat(mod, label))
    wrapper = _timed(stat, fn, sys.intern(func))
    hooks.remove_hook(func, kind, ident)
    hooks.add_hook(func, kind, ident, wrapper)
    run.installed.append(Installed(func, kind, ident, fn, wrapper, module, attr))


def _short(func: str) -> str:
    return func.rsplit(":", 1)[-1]


# endregion
# region Frames and GC


def _on_frame(*_: Any) -> None:
    run = R.run
    if run is None:
        return
    now = time.perf_counter()
    gap = now - run.last_call
    run.last_call = now
    if gap < SAME_UPDATE_S:
        return
    try:
        frame = int(R.ks.GetFrameCount())
    except Exception:  # noqa: BLE001
        return
    finally:
        run.frame_hook_s += time.perf_counter() - now
    if frame == run.frame_no:
        return
    if run.frame_no >= 0 and len(run.frames) < MAX_FRAMES:
        top, top_s = "", 0.0
        for name, took in run.frame_by_mod.items():
            if took > top_s:
                top, top_s = name, took
        run.frames.append((now - run.frame_start, run.frame_py, top, top_s))
    run.frame_no = frame
    run.frame_start = now
    run.frame_py = 0.0
    run.frame_by_mod.clear()


def _on_gc(phase: str, info: dict[str, Any]) -> None:
    run = R.run
    if run is None:
        return
    if phase == "start":
        run.gc_start = time.perf_counter()
    elif run.gc_start and len(run.gcs) < 5000:
        run.gcs.append((int(info.get("generation", -1)), run.gc_start - run.started, time.perf_counter() - run.gc_start))
        run.gc_start = 0.0


def _drop_gc_callback() -> None:
    """Removes ours, also a copy from before a hot reload of this module."""
    gc.callbacks[:] = [c for c in gc.callbacks if not (getattr(c, "__name__", "") == "_on_gc" and getattr(c, "__module__", "") == __name__)]


# endregion
# region Start / finish


def running() -> bool:
    return R.run is not None


def elapsed() -> float:
    run = R.run
    return 0.0 if run is None else time.perf_counter() - run.started


def start(length: float, skip: str, world: str = "") -> str:
    """Starts a run (game thread). skip: our own mod's name (its tick isn't what's measured)."""
    if R.run is not None:
        stop()
    R.ks = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    run = Run(length=length, world=world)
    run.overhead_s = _calibrate()
    mods = mod_list()
    enabled = [m for m in mods if getattr(m, "is_enabled", False) and m.name != skip]
    run.mods_off = sum(1 for m in mods if not getattr(m, "is_enabled", False))
    run.mods_seen = [m.name for m in enabled]
    run.packages = _packages(mods)
    for mod in enabled:
        for h in getattr(mod, "hooks", ()) or ():
            for func, kind in h.hook_funcs:
                if hooks.has_hook(func, kind, h.hook_identifier):
                    name = getattr(h.__wrapped__, "__name__", h.hook_identifier.rsplit(":", 1)[-1])
                    _install(run, func, kind, h.hook_identifier, h.__wrapped__, mod.name, f"{name} [{_short(func)}]")
    for module_name, attr, func_attr, typ, ident in DIRECT:
        module = sys.modules.get(module_name)
        fn = getattr(module, attr, None) if module is not None else None
        func = getattr(module, func_attr, None) if module is not None else None
        kind = getattr(hooks.Type, typ)
        if fn is None or not func or not hooks.has_hook(func, kind, ident):
            continue
        owner = run.packages.get(module_name.partition(".")[0], module_name.partition(".")[0])
        _install(run, func, kind, ident, fn, owner, f"{attr} [{_short(func)}]", module_name, attr)
    run.notes.append("Hooks other mods add outside mods_base, and their page scripts, can't be seen.")
    _drop_gc_callback()
    gc.callbacks.append(_on_gc)
    run.threads0 = _thread_cpu(threading.get_native_id())
    run.frame0 = int(R.ks.GetFrameCount())
    run.started = run.last_call = time.perf_counter()
    R.run = run
    hooks.add_hook(FRAME_FUNC, hooks.Type.POST, FRAME_ID, _on_frame)
    return f"timing {len(run.installed)} hooks of {len(enabled)} mods for {length:.0f} s"


def _restore(run: Run) -> list[str]:
    """Puts each original hook back, unless a reload or a disable replaced our wrapper meanwhile."""
    current: dict[str, Callable[..., Any]] = {}
    for mod in mod_list():
        for h in getattr(mod, "hooks", ()) or ():
            current[h.hook_identifier] = h.__wrapped__
    left = []
    while run.installed:
        item = run.installed.pop()
        newer: Any = current.get(item.ident)
        if item.attr and item.module in sys.modules:
            newer = getattr(sys.modules[item.module], item.attr, None)
        if newer is not None and newer is not item.original and getattr(newer, "__func__", None) is not getattr(item.original, "__func__", 0):
            left.append(item.ident)  # a hot reload re-registered its hook over our wrapper
            continue
        if not hooks.has_hook(item.func, item.kind, item.ident):
            continue  # its mod was disabled meanwhile: nothing to put back
        hooks.remove_hook(item.func, item.kind, item.ident)
        hooks.add_hook(item.func, item.kind, item.ident, item.original)
    return left


def stop() -> dict[str, Any] | None:
    """Ends the run (game thread), restores every hook, returns the results (None if none ran)."""
    run = R.run
    if run is None:
        return None
    span = max(time.perf_counter() - run.started, 1e-6)
    try:
        engine_frames = int(R.ks.GetFrameCount()) - run.frame0
    except Exception:  # noqa: BLE001
        engine_frames = len(run.frames)
    R.run = None
    hooks.remove_hook(FRAME_FUNC, hooks.Type.POST, FRAME_ID)
    _drop_gc_callback()
    _restore(run)
    threads = _thread_cpu(threading.get_native_id())
    return _results(run, span, max(engine_frames, 1), threads)


def abort() -> None:
    """Ends a run without results and puts the hooks back. Any thread: no engine calls."""
    run = R.run
    if run is None:
        return
    R.run = None
    hooks.remove_hook(FRAME_FUNC, hooks.Type.POST, FRAME_ID)
    _drop_gc_callback()
    _restore(run)


def _results(run: Run, span: float, frames_n: int, threads: dict[tuple[int, str], float]) -> dict[str, Any]:
    by_mod: dict[str, dict[str, Any]] = {
        name: {"name": name, "total": 0.0, "worst": 0.0, "over_5ms": 0, "calls": 0, "threads_s": 0.0, "hooks": [], "threads": []}
        for name in run.mods_seen
    }
    calls_all = 0
    for stat in run.stats.values():
        entry = by_mod.setdefault(stat.mod, {"name": stat.mod, "total": 0.0, "worst": 0.0, "over_5ms": 0, "calls": 0, "threads_s": 0.0, "hooks": [], "threads": []})
        entry["total"] += stat.total
        entry["worst"] = max(entry["worst"], stat.worst)
        entry["over_5ms"] += stat.over_5ms
        entry["calls"] += stat.calls
        calls_all += stat.calls
        entry["hooks"].append({
            "label": stat.label,
            "ms_frame": stat.total * 1000 / frames_n,
            "avg_ms": stat.total * 1000 / stat.calls if stat.calls else 0.0,
            "worst_ms": stat.worst * 1000,
            "calls_s": stat.calls / span,
            "over_5ms": stat.over_5ms,
        })
    # Worker threads: CPU used during the run, on the mod whose module started the thread
    live = {t.native_id: t for t in threading.enumerate() if getattr(t, "native_id", None)}
    other_threads = 0.0
    for key, cpu in threads.items():
        used = cpu - run.threads0.get(key, 0.0)
        if used <= 0.0005:
            continue
        t = live.get(key[0])
        package = _thread_module(t).partition(".")[0] if t is not None else ""
        owner = run.packages.get(package)
        if owner is None or owner not in by_mod:
            other_threads += used
            continue
        by_mod[owner]["threads_s"] += used
        by_mod[owner]["threads"].append({"name": key[1], "cpu_ms_s": used * 1000 / span})
    # Each slow call: the mod's own, or a GC pause / a stall shared by several mods that frame
    gcs = run.gcs
    mods_in_frame: dict[int, set[str]] = {}
    for frame, _t, mod, _label, _took in run.slow:
        mods_in_frame.setdefault(frame, set()).add(mod)

    def cause(frame: int, t: float, took: float) -> str:
        if any(t - 0.0005 <= g_at <= t + took for _g, g_at, _gt in gcs):
            return "gc"
        if len(mods_in_frame.get(frame, ())) >= SHARED_MODS:
            return "shared"
        return "own"

    slow = [(frame, t, mod, label, took, cause(frame, t, took)) for frame, t, mod, label, took in run.slow]
    own_slow: dict[str, list[float]] = {}
    outside: dict[str, int] = {}
    for _f, _t, mod, _label, took, why in slow:
        if why == "own":
            own_slow.setdefault(mod, []).append(took)
        else:
            outside[mod] = outside.get(mod, 0) + 1
    small_worst: dict[str, float] = {}
    for stat in run.stats.values():
        small_worst[stat.mod] = max(small_worst.get(stat.mod, 0.0), stat.small_worst)
    shared_frames = sum(1 for names in mods_in_frame.values() if len(names) >= SHARED_MODS)

    mods = []
    for entry in by_mod.values():
        ms_frame = entry["total"] * 1000 / frames_n
        worst_ms = entry["worst"] * 1000
        own = own_slow.get(entry["name"], [])
        own_worst_ms = max(own) * 1000 if own else small_worst.get(entry["name"], 0.0) * 1000
        own_over_5ms = sum(1 for took in own if took > HITCH_S)
        entry["hooks"].sort(key=lambda h: -h["ms_frame"])
        mods.append({
            "name": entry["name"],
            "ms_frame": ms_frame,
            "worst_ms": worst_ms,
            # Without the slow calls a GC pause or a shared stall caused
            "own_worst_ms": own_worst_ms,
            "own_over_5ms": own_over_5ms,
            "outside_slow": outside.get(entry["name"], 0),
            "over_5ms": entry["over_5ms"],
            "calls_s": entry["calls"] / span,
            "threads_ms_s": entry["threads_s"] * 1000 / span,
            "hooks": entry["hooks"],
            "threads": entry["threads"],
            "verdict": verdict(ms_frame, own_worst_ms, entry["threads_s"] * 1000 / span),
        })
    mods.sort(key=lambda m: (-m["ms_frame"], -m["own_worst_ms"], m["name"]))

    times = sorted(f[0] for f in run.frames)
    n = len(times)
    median = times[n // 2] if n else 0.0
    p99 = times[min(n - 1, int(n * 0.99))] if n else 0.0
    hitch_at = max(2 * median, 1 / 30)
    hitches = [f for f in run.frames if f[0] > hitch_at]
    slowest = sorted(run.frames, key=lambda f: -f[0])[:5]
    gc_by_gen: dict[int, list[float]] = {}
    for gen, _at, took in run.gcs:
        e = gc_by_gen.setdefault(gen, [0, 0.0, 0.0])
        e[0] += 1
        e[1] += took
        e[2] = max(e[2], took)
    total_ms_frame = sum(m["ms_frame"] for m in mods)
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "world": run.world,
        "seconds": span,
        "frames": frames_n,
        "fps": frames_n / span,
        "median_ms": median * 1000,
        "p99_ms": p99 * 1000,
        "worst_ms": times[-1] * 1000 if n else 0.0,
        "hitches": len(hitches),
        "hitch_ms": hitch_at * 1000,
        "slowest": [{"ms": f[0] * 1000, "mods_ms": f[1] * 1000, "top": f[2], "top_ms": f[3] * 1000} for f in slowest],
        "mods_ms_frame": total_ms_frame,
        "share": total_ms_frame / (median * 1000) if median else 0.0,
        "overhead_ms_frame": (run.overhead_s * calls_all + run.frame_hook_s) * 1000 / frames_n,
        # The SDK's cost of calling each hook, outside every wrapper: the average back-to-back gap
        # (it includes our wrapper's entry, so it's an upper bound) times every hook call
        "dispatch_us": run.dispatch_s * 1e6 / run.dispatch_n if run.dispatch_n else 0.0,
        "dispatch_ms_frame": (run.dispatch_s / run.dispatch_n * calls_all * 1000 / frames_n) if run.dispatch_n else 0.0,
        "calls_frame": calls_all / frames_n,
        "gc": [{"gen": g, "count": e[0], "avg_ms": e[1] * 1000 / e[0], "worst_ms": e[2] * 1000} for g, e in sorted(gc_by_gen.items())],
        "spikes": [{"t": t, "label": f"{m}: {label}", "ms": took * 1000, "cause": why} for _f, t, m, label, took, why in slow if took > SPIKE_S][-30:],
        "shared_frames": shared_frames,
        "gc_slow_calls": sum(1 for x in slow if x[5] == "gc"),
        "other_threads_ms_s": other_threads * 1000 / span,
        "mods": mods,
        "mods_off": run.mods_off,
        "notes": run.notes,
    }


def verdict(ms_frame: float, worst_ms: float, threads_ms_s: float) -> str:
    """Against the budget in the root CLAUDE.md: well under 0.25 ms a frame, no tick over 5 ms."""
    if worst_ms > HITCH_S * 1000:
        return "hitches"
    if ms_frame >= 0.25 or threads_ms_s >= 50:
        return "heavy"
    if ms_frame >= 0.05 or threads_ms_s >= 10:
        return "light"
    return "free"


# endregion
# region Report text


def report_text(res: dict[str, Any]) -> str:
    lines = [
        f"BL4 Mod Profiler, {res['at']}" + (f", {res['world']}" if res["world"] else ""),
        f"{res['seconds']:.1f} s, {res['frames']} frames ({res['fps']:.0f} fps): median {res['median_ms']:.2f} ms,"
        f" p99 {res['p99_ms']:.2f} ms, worst {res['worst_ms']:.1f} ms, {res['hitches']} frames over {res['hitch_ms']:.0f} ms",
        f"All mods' hooks: {res['mods_ms_frame']:.3f} ms per frame ({res['share'] * 100:.1f}% of a median frame);"
        f" the profiler itself added about {res['overhead_ms_frame']:.3f} ms per frame (not included)",
    ]
    if res.get("dispatch_us"):
        lines.append(
            f"Calling the hooks themselves (the SDK's side, outside every number above): about {res['dispatch_us']:.1f} us per call,"
            f" {res['calls_frame']:.0f} calls per frame = ~{res['dispatch_ms_frame']:.2f} ms per frame",
        )
    for g in res["gc"]:
        lines.append(f"Python GC gen{g['gen']}: {g['count']}x, avg {g['avg_ms']:.2f} ms, worst {g['worst_ms']:.2f} ms")
    if res["other_threads_ms_s"] > 0.05:
        lines.append(f"Other Python threads: {res['other_threads_ms_s']:.1f} ms CPU per second")
    lines.append(
        f"Slow calls not caused by their mod: {res['gc_slow_calls']} during a GC pause, and {res['shared_frames']} frame(s) where"
        f" {SHARED_MODS}+ mods were slow at once (a worker thread holding the GIL, or the engine stalling)",
    )
    lines.append("")
    lines.append("own worst = the slowest call that wasn't a GC pause or a shared stall; raw = the slowest of all")
    lines.append(f"{'mod':40} {'ms/frame':>9} {'own worst':>9} {'raw':>7} {'>5ms':>5} {'calls/s':>8} {'threads':>8}  verdict")
    for m in res["mods"]:
        lines.append(
            f"{m['name'][:40]:40} {m['ms_frame']:9.3f} {m['own_worst_ms']:9.2f} {m['worst_ms']:7.2f} {m['own_over_5ms']:5d} {m['calls_s']:8.0f}"
            f" {m['threads_ms_s']:8.1f}  {m['verdict']}",
        )
        for h in m["hooks"]:
            lines.append(f"    {h['label'][:56]:56} {h['ms_frame']:9.3f} {'':9} {h['worst_ms']:7.2f} {h['over_5ms']:5d} {h['calls_s']:8.0f}")
        for t in m["threads"]:
            lines.append(f"    thread {t['name'][:49]:49} {t['cpu_ms_s']:8.1f} ms CPU/s")
    if res["slowest"]:
        lines.append("")
        lines.append("Slowest frames (frame ms, mod Python in it, biggest mod):")
        for f in res["slowest"]:
            lines.append(f"  {f['ms']:7.1f}  {f['mods_ms']:6.2f}  {f['top'] or '-'} {f['top_ms']:.2f}")
    if res["spikes"]:
        lines.append("")
        lines.append(f"Hook calls over {SPIKE_S * 1000:.0f} ms (s into the run, hook, ms, cause):")
        for s in res["spikes"]:
            lines.append(f"  {s['t']:6.2f}  {s['label'][:70]:70} {s['ms']:7.2f}  {s['cause']}")
    lines.append("")
    lines.append(f"{res['mods_off']} mod(s) switched off weren't measured.")
    lines.extend(res["notes"])
    return "\n".join(lines) + "\n"


# endregion
