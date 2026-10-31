"""
Long hitch hunt (bridge): times every mod's hooks and frame tick callbacks for many minutes of real
play and keeps only the frames that hitched, with what ran in them.

It reuses the Mod Menu profiler's wrappers (mods/bl4_mod_menu/perf/profiler.py, imported under its
own name so the Mod Menu's copy and its benchmark are untouched), but instead of every frame it keeps
a frame-time histogram per world and, for each frame over HH_HITCH_MS (or with HH_OWN_MS of mod
Python), its time, QPC timestamp (the clock dev/perf/sampler.py uses), world, whether a menu was up,
the mods' Python in it (top 4), every call over 1 ms in it and any GC pause.

    python tools/bridge.py <id> -f dev/hitch_hunt.py                 # starts (or restarts)
    python tools/bridge.py <id> "print(hh_report())"                 # summary so far, keeps going
    python tools/bridge.py <id> "print(hh_stop())"                   # puts every hook back, writes .perf/hitch_hunt.json

Cost: the profiler's (~0.05 ms a frame); a hitch frame adds a world/cursor read (~30 us).
"""

# ruff: noqa: F821

import importlib.util
import json
import os
import sys
import time

import mods_base
from mods_base import ENGINE, get_pc

_HH_REPO = r"C:\Users\Josh\Documents\projects\bl4-direct"
HH_HITCH_MS = 28.0  # about twice a 75 fps frame
HH_OWN_MS = 3.0  # mod Python in one frame worth keeping even when the frame wasn't slow
HH_MAX_HITCHES = 20000
_HH_BUCKETS = (8.3, 11.1, 16.7, 25.0, 33.3, 50.0, 100.0, 250.0, 1e9)


def _hh_load_profiler():
    name = "_hh_profiler"
    old = sys.modules.get(name)
    if old is not None and getattr(old, "R", None) is not None and old.R.run is not None:
        old.abort()
    path = os.path.join(_HH_REPO, "mods", "bl4_mod_menu", "perf", "profiler.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.FRAME_ID = "hitch_hunt.frame"  # not the Mod Menu benchmark's id
    module.MAX_SLOW = 200000
    return module


class _HHFrames(list):
    """Stands in for Run.frames: the profiler appends every finished frame; we keep the hitches."""

    def __init__(self, run):
        super().__init__()
        self.run = run
        self.hist = {}  # world -> [count per bucket]
        self.hitches = []
        self.world = ""
        self.world_at = 0.0
        self.menu = False

    def __len__(self):
        return 0  # the profiler's MAX_FRAMES check always passes

    def _state(self, now):
        if now - self.world_at > 0.5:  # at most twice a second (only on hitch frames)
            self.world_at = now
            try:
                self.world = str(ENGINE.GameViewport.World.Name)
            except Exception:  # noqa: BLE001
                self.world = "?"
            try:
                pc = get_pc(possibly_loading=True)
                self.menu = pc is None or bool(pc.bShowMouseCursor) or pc.Pawn is None
            except Exception:  # noqa: BLE001
                self.menu = True

    def append(self, item):
        frame_s, py, top, top_s = item
        ms = frame_s * 1000.0
        world = self.world
        row = self.hist.get(world)
        if row is None:
            row = self.hist[world] = [0] * len(_HH_BUCKETS)
        for i, edge in enumerate(_HH_BUCKETS):
            if ms < edge:
                row[i] += 1
                break
        if (ms >= HH_HITCH_MS or py * 1000.0 >= HH_OWN_MS) and len(self.hitches) < HH_MAX_HITCHES:
            now = time.perf_counter()
            self._state(now)
            run = self.run
            mods = sorted(run.frame_by_mod.items(), key=lambda kv: -kv[1])[:4]
            self.hitches.append({
                "t": round(run.frame_start - run.started, 3),  # frame start, seconds into the hunt
                "qpc_ns": time.perf_counter_ns() - int((now - run.frame_start) * 1e9),
                "wall": time.time(),
                "frame": run.frame_no,
                "ms": round(ms, 2),
                "py_ms": round(py * 1000.0, 3),
                "mods": [(m, round(s * 1000.0, 3)) for m, s in mods],
                "world": self.world,
                "menu": self.menu,
            })


class HH:
    prof = None
    run = None
    frames = None


def hh_start():
    if HH.prof is not None and HH.prof.R.run is not None:
        HH.prof.abort()
    real = sys.modules.get("bl4_mod_menu.perf.profiler")
    if real is not None and real.R.run is not None:
        return "the Mod Menu's own benchmark is running: try again when it's done"
    HH.prof = prof = _hh_load_profiler()
    msg = prof.start(36000.0, "", "")
    HH.run = prof.R.run
    HH.frames = HH.run.frames = _HHFrames(HH.run)
    try:
        HH.frames.world = str(ENGINE.GameViewport.World.Name)
    except Exception:  # noqa: BLE001
        pass
    return "hitch hunt: " + msg


def _hh_collect():
    run, fr = HH.run, HH.frames
    if run is None:
        return None
    slow_by_frame = {}
    for frame_no, t, mod, label, took in run.slow:
        slow_by_frame.setdefault(frame_no, []).append((mod, label, round(took * 1000.0, 2)))
    hitches = []
    for h in fr.hitches:
        h = dict(h)
        h["slow"] = slow_by_frame.get(h["frame"], [])
        h["gc"] = [(g, round(s * 1000.0, 2)) for g, t0, s in run.gcs if h["t"] - 0.001 <= t0 <= h["t"] + h["ms"] / 1000.0]
        hitches.append(h)
    stats = []
    for st in run.stats.values():
        if st.calls:
            stats.append({"mod": st.mod, "hook": st.label, "calls": st.calls, "total_ms": round(st.total * 1000.0, 1),
                          "worst_ms": round(st.worst * 1000.0, 2), "over_5ms": st.over_5ms})
    stats.sort(key=lambda s: -s["total_ms"])
    gcs = {}
    for g, _t0, s in run.gcs:
        e = gcs.setdefault(g, [0, 0.0, 0.0])
        e[0] += 1
        e[1] += s
        e[2] = max(e[2], s)
    return {
        "span_s": round(time.perf_counter() - run.started, 1),
        "started_wall": time.time() - (time.perf_counter() - run.started),
        "hist_edges_ms": list(_HH_BUCKETS),
        "hist": fr.hist,
        "hitches": hitches,
        "stats": stats,
        "gc": {str(g): {"n": e[0], "avg_ms": round(e[1] / e[0] * 1000.0, 2), "worst_ms": round(e[2] * 1000.0, 2)} for g, e in gcs.items()},
        "slow_calls": len(run.slow),
    }


def hh_report(top=25):
    data = _hh_collect()
    if data is None:
        return "not running"
    lines = [f"{data['span_s']:.0f} s, {len(data['hitches'])} kept frames, {data['slow_calls']} calls over 1 ms, GC {data['gc']}"]
    for world, row in data["hist"].items():
        n = sum(row) or 1
        lines.append(f"  {world or '?':24} {sum(row):7} frames  " + "  ".join(
            f"<{e:g}:{c}" for e, c in zip(data["hist_edges_ms"], row) if c))
    worst = sorted(data["hitches"], key=lambda h: -h["ms"])[:top]
    for h in worst:
        lines.append(f"  {h['t']:8.1f}s {h['ms']:7.1f} ms py {h['py_ms']:6.2f} {h['world']}{' menu' if h['menu'] else ''} "
                     f"{h['mods'][:2]} slow={h['slow'][:3]} gc={h['gc']}")
    own = [h for h in data["hitches"] if h["py_ms"] >= HH_OWN_MS]
    lines.append(f"frames with >= {HH_OWN_MS} ms of mod Python: {len(own)}")
    for h in sorted(own, key=lambda h: -h["py_ms"])[:top]:
        lines.append(f"  {h['t']:8.1f}s {h['ms']:7.1f} ms py {h['py_ms']:6.2f} {h['mods'][:3]} slow={h['slow'][:4]} gc={h['gc']}")
    lines.append("hooks by total:")
    for s in data["stats"][:15]:
        lines.append(f"  {s['total_ms']:9.1f} ms  worst {s['worst_ms']:6.2f}  >5ms {s['over_5ms']:3}  {s['calls']:8}  {s['mod']}: {s['hook']}")
    return "\n".join(lines)


def hh_save(path=None):
    data = _hh_collect()
    if data is None:
        return "not running"
    path = path or os.path.join(_HH_REPO, ".perf", "hitch_hunt.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return path


def hh_stop():
    if HH.prof is None or HH.prof.R.run is None:
        return "not running"
    path = hh_save()
    HH.prof.abort()
    HH.run = HH.frames = None
    return f"stopped, hooks restored, saved {path}"


print(hh_start())
