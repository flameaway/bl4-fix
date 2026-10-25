"""
Times every hook body of every loaded mod in a running game, through the bridge. Unlike
perf_probe.py (which wraps named module functions), this re-registers each active hook under its
own identifier with a timing wrapper, so it sees hooks that were registered directly too. It also
samples the CPU time of every Python worker thread (they share the GIL with the game thread).

    python tools/bridge.py <id> "exec(open(r'<repo>/dev/perf_hooks.py').read()); print(hooks_start())"

Its helpers are all `_ph_*`: perf_probe.py, exec'd into the same bridge namespace, defines its own
`_timed`, and once replaced ours (the wrappers then raised after every hook, losing Blocks).
    ... 30 s or more of play / menu use ...
    python tools/bridge.py <id> "print(hooks_report())"      # keeps measuring
    python tools/bridge.py <id> "print(hooks_stop())"        # puts the original hooks back

A hot reload of a mod during a run replaces our wrapper with its new hook (same identifier);
hooks_stop() then leaves that one alone instead of putting the stale function back.
"""

# ruff: noqa: F821

import ctypes
import threading
import time
from ctypes import wintypes

import sys

import mods_base
from unrealsdk import hooks

def _ph_mods():
    ml = mods_base.mod_list
    return getattr(ml, "mod_list", ml)


_ph_FRAME_HOOK = "/Script/Engine.CameraModifier:BlueprintModifyPostProcess"
_ph_FRAME_ID = "perf_hooks.frame"
_ph_SAME_FRAME_S = 0.002

# Hooks added with add_hook directly (not through mods_base): (module, attr, func, type, identifier)
_ph_DIRECT = [
    ("bl4_direct.ui", "_on_tick", "/Script/Engine.CameraModifier:BlueprintModifyCamera", "POST", "bl4_direct.ui"),
    ("bl4_mod_menu.menu", "_on_tick", "/Script/Engine.CameraModifier:BlueprintModifyCamera", "POST", "bl4_mod_menu.menu"),
    ("bl4_direct.coop", "_chat_hook", None, "PRE", "bl4_coop_chat"),
    ("bl4_direct.coop", "_message_hook", None, "PRE", "bl4_coop_message"),
    ("bl4_direct.coop", "_tick_hook", "/Script/Engine.CameraModifier:BlueprintModifyCamera", "POST", "bl4_coop_tick"),
]


class _ph_P:
    stats: dict = {}  # label -> [calls, total, worst, >1ms, >5ms, frames-with-call]
    installed: list = []  # (func, type, ident, original, wrapper, label)
    frames: list = []
    last_call = 0.0
    frame_start = 0.0
    frame_no = 0
    started = 0.0
    threads0: dict = {}
    spikes: list = []  # (t, label, took, frame) for ticks over 3 ms
    gc: list = []  # (t, generation, took)
    gc_start = 0.0


def _ph_on_gc(phase, info):
    if phase == "start":
        _ph_P.gc_start = time.perf_counter()
    elif _ph_P.gc_start and len(_ph_P.gc) < 5000:
        _ph_P.gc.append((_ph_P.gc_start - _ph_P.started, info.get("generation"), time.perf_counter() - _ph_P.gc_start))


def _ph_timed(label, fn):
    entry = _ph_P.stats.setdefault(label, [0, 0.0, 0.0, 0, 0, 0, -1, 0.0, 0])

    def wrapper(obj, args, ret, func):
        try:
            frame = int(_ph_P.ks.GetFrameCount())  # outside the timed part (~10 us)
        except Exception:  # noqa: BLE001
            frame = -1
        start = time.perf_counter()
        result = fn(obj, args, ret, func)  # its exceptions go to the SDK as before
        try:
            _ph_book(entry, label, start, time.perf_counter() - start, frame)
        except Exception:  # noqa: BLE001, S110 - never let the timing break the hook (or its Block)
            pass
        return result

    return wrapper


def _ph_book(entry, label, start, took, frame):
    entry[0] += 1
    entry[1] += took
    if took > entry[2]:
        entry[2] = took
    entry[3] += took > 0.001
    entry[4] += took > 0.005
    if took > 0.003 and len(_ph_P.spikes) < 2000:
        _ph_P.spikes.append((start - _ph_P.started, label, took, _ph_P.frame_no))
    if entry[6] != frame:
        entry[6] = frame
        entry[5] += 1
    else:  # a repeat call in the same frame (the camera hooks fire once per modifier)
        entry[7] += took
        entry[8] += 1


def _ph_on_frame(*_a):
    now = time.perf_counter()
    if now - _ph_P.last_call > _ph_SAME_FRAME_S:
        if _ph_P.frame_start:
            _ph_P.frames.append(now - _ph_P.frame_start)
        _ph_P.frame_start = now
        _ph_P.frame_no += 1
    _ph_P.last_call = now


_ph_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ph_OpenThread = _ph_k32.OpenThread
_ph_OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_ph_OpenThread.restype = wintypes.HANDLE
_ph_GetThreadTimes = _ph_k32.GetThreadTimes
_ph_GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_ph_GetThreadTimes.restype = wintypes.BOOL
_ph_CloseHandle = _ph_k32.CloseHandle
_ph_CloseHandle.argtypes = [wintypes.HANDLE]


def _ph_thread_cpu() -> dict:
    out = {}
    for t in threading.enumerate():
        tid = getattr(t, "native_id", None)
        if not tid:
            continue
        h = _ph_OpenThread(0x0800, False, tid)  # THREAD_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if _ph_GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                def v(ft):
                    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

                out[(tid, t.name)] = v(k) + v(u)
        finally:
            _ph_CloseHandle(h)
    return out


def _ph_install(func, typ, ident, fn, label):
    hooks.remove_hook(func, typ, ident)
    wrapper = _ph_timed(label, fn)
    hooks.add_hook(func, typ, ident, wrapper)
    _ph_P.installed.append((func, typ, ident, fn, wrapper, label))


def hooks_start() -> str:
    hooks_stop()
    _ph_P.stats.clear()
    _ph_P.frames.clear()
    _ph_P.spikes.clear()
    _ph_P.gc.clear()
    import gc as _gc
    if _ph_on_gc not in _gc.callbacks:
        _gc.callbacks[:] = [c for c in _gc.callbacks if getattr(c, '__name__', '') != '_ph_on_gc']
        _gc.callbacks.append(_ph_on_gc)
    _ph_P.last_call = _ph_P.frame_start = 0.0
    _ph_P.started = time.perf_counter()
    import unrealsdk
    _ph_P.ks = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    _ph_P.frame0 = int(_ph_P.ks.GetFrameCount())
    notes = []
    for mod in _ph_mods():
        for h in mod.hooks:
            for func, typ in h.hook_funcs:
                if hooks.has_hook(func, typ, h.hook_identifier):
                    short = h.hook_identifier.replace("mods_base.hook:", "")
                    _ph_install(func, typ, h.hook_identifier, h.__wrapped__, f"{short} [{func.rsplit(':', 1)[-1]}]")
    import importlib
    import sys

    for module, attr, func, typ, ident in _ph_DIRECT:
        m = sys.modules.get(module)
        if m is None:
            continue
        fn = getattr(m, attr, None)
        if fn is None:
            notes.append(f"no {module}.{attr}")
            continue
        if func is None:
            func = getattr(m, "CHAT_FUNC" if "chat" in attr else "MESSAGE_FUNC", None)
        t = getattr(hooks.Type, typ)
        if func and hooks.has_hook(func, t, ident):
            _ph_install(func, t, ident, fn, f"{module}.{attr} [{func.rsplit(':', 1)[-1]}]")
        else:
            notes.append(f"not active: {ident}")
    hooks.add_hook(_ph_FRAME_HOOK, hooks.Type.POST, _ph_FRAME_ID, _ph_on_frame)
    _ph_P.threads0 = _ph_thread_cpu()
    return f"timing {len(_ph_P.installed)} hooks" + "".join(f"\n  {n}" for n in notes)


def hooks_stop() -> str:
    """Puts the originals back, unless a reload replaced our wrapper meanwhile."""
    restored = 0
    skipped = []
    current = {}
    for mod in _ph_mods():
        for h in mod.hooks:
            current[h.hook_identifier] = h.__wrapped__
    while _ph_P.installed:
        func, typ, ident, fn, wrapper, label = _ph_P.installed.pop()
        newer = current.get(ident)
        for module, attr, _f, _t, direct_ident in _ph_DIRECT:
            if direct_ident == ident and module in sys.modules:
                newer = getattr(sys.modules[module], attr, None)
        if newer is not None and newer is not fn and getattr(newer, "__func__", None) is not getattr(fn, "__func__", 0):
            skipped.append(f"reloaded: {label}")  # its enable() already replaced our wrapper
            continue
        if not hooks.has_hook(func, typ, ident):
            skipped.append(f"removed meanwhile: {label}")
            continue
        hooks.remove_hook(func, typ, ident)
        hooks.add_hook(func, typ, ident, fn)
        restored += 1
    hooks.remove_hook(_ph_FRAME_HOOK, hooks.Type.POST, _ph_FRAME_ID)
    import gc as _gc
    _gc.callbacks[:] = [c for c in _gc.callbacks if getattr(c, '__name__', '') != '_ph_on_gc']
    return f"restored {restored}, left {len(skipped)}" + "".join(f"\n  {x}" for x in skipped)


def _ph_ms(s):
    return f"{s * 1000:.3f}"


def hooks_report(top: int = 60) -> str:
    span = time.perf_counter() - _ph_P.started
    lines = [f"over {span:.1f} s"]
    frames = sorted(_ph_P.frames)
    nframes = len(frames)
    if frames:
        med = frames[nframes // 2]
        p99 = frames[min(nframes - 1, int(nframes * 0.99))]
        lines.append(
            f"frames {nframes} ({nframes / span:.0f}/s): median {_ph_ms(med)} ms, p99 {_ph_ms(p99)}, worst {_ph_ms(frames[-1])},"
            f" >2x median {sum(f > 2 * med for f in frames)}",
        )
    real = int(_ph_P.ks.GetFrameCount()) - _ph_P.frame0
    lines.append(f"engine frames {real} ({real / span:.0f}/s)")
    nframes = real
    total_all = sum(v[1] for v in _ph_P.stats.values())
    lines.append(f"all hooks: {_ph_ms(total_all / span)} ms/s, {_ph_ms(total_all / max(nframes, 1))} ms/frame")
    lines.append(f"{'hook':70} {'calls/s':>8} {'avg ms':>7} {'worst':>7} {'>1ms':>5} {'>5ms':>5} {'ms/frame':>8} {'repeat avg':>10}")
    for label, (calls, total, worst, o1, o5, fr, _, rep_t, rep_n) in sorted(_ph_P.stats.items(), key=lambda kv: -kv[1][1])[:top]:
        if not calls:
            continue
        lines.append(
            f"{label[:70]:70} {calls / span:8.1f} {_ph_ms(total / calls):>7} {_ph_ms(worst):>7} {o1:5d} {o5:5d}"
            f" {_ph_ms(total / max(nframes, 1)):>8} {_ph_ms(rep_t / rep_n) if rep_n else '-':>10}",
        )
    gcs = _ph_P.gc
    if gcs:
        by_gen = {}
        for _t, g, took in gcs:
            e = by_gen.setdefault(g, [0, 0.0, 0.0])
            e[0] += 1
            e[1] += took
            e[2] = max(e[2], took)
        lines.append("gc: " + ", ".join(f"gen{g} {n}x avg {_ph_ms(tot / n)} worst {_ph_ms(w)}" for g, (n, tot, w) in sorted(by_gen.items())))
    if _ph_P.spikes:
        lines.append("ticks over 3 ms (t s, frame, hook, ms, gc during it):")
        for t, label, took, fr in _ph_P.spikes[-40:]:
            g = [f"gen{gg} {_ph_ms(gt)}" for gt0, gg, gt in gcs if t - 0.001 <= gt0 <= t + took]
            lines.append(f"  {t:7.2f} f{fr:<6} {label[:50]:50} {_ph_ms(took):>8} {' '.join(g)}")
    now = _ph_thread_cpu()
    lines.append("python threads (CPU ms per s; the game thread includes the game itself):")
    for key, cpu in sorted(now.items(), key=lambda kv: -(kv[1] - _ph_P.threads0.get(kv[0], kv[1]))):
        if key in _ph_P.threads0:
            lines.append(f"  {key[1][:50]:50} {(cpu - _ph_P.threads0[key]) * 1000 / span:8.2f}")
    return "\n".join(lines)
