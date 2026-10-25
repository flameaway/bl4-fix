"""
GPU busy time per rendered frame of a running game, from Windows' own GPU performance counters
(PDH "GPU Engine" utilization, the same numbers Task Manager shows) plus the game's frame counter.
Read-only: nothing is injected or written to the game.

    python dev/perf/gpu_util.py <pid> [--seconds 20] [--runs 1] [--label x] [--csv out.csv]

Per run it prints:
  fps          game frames per second (GFrameCounter; frame-gen frames are not counted)
  3d%          utilization of the game's 3D engine(s) (WDDM sums every queue mapped onto it)
  copy%        all copy engines of the game, summed
  other%       every other engine of the game (OFA = optical flow, used by DLSS-G)
  gpu_ms       3d% * frame time = GPU 3D-engine busy ms per game frame
  all3d%       3D utilization of all processes (to spot another program using the GPU)
The 1 s samples inside a run give the spread (sd) of fps and 3d%.
Whether the game is in front can't be read from the agent shell (session 0): check it through the
bridge (dev/perf/events.py).
"""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402
from sampler import FRAME_COUNTER_RVA  # noqa: E402

pdh = ctypes.WinDLL("pdh")
PDH_FMT_DOUBLE = 0x200
PDH_MORE_DATA = 0x800007D2


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", w.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", w.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


pdh.PdhOpenQueryW.argtypes = (w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
pdh.PdhAddEnglishCounterW.argtypes = (ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_void_p))
pdh.PdhCollectQueryData.argtypes = (ctypes.c_void_p,)
pdh.PdhGetFormattedCounterArrayW.argtypes = (ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
                                             ctypes.POINTER(w.DWORD), ctypes.c_void_p)
pdh.PdhGetFormattedCounterArrayW.restype = w.DWORD
pdh.PdhCloseQuery.argtypes = (ctypes.c_void_p,)


class Counter:
    def __init__(self, path: str):
        self.q = ctypes.c_void_p()
        self.c = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(self.q)):
            raise OSError("PdhOpenQuery")
        st = pdh.PdhAddEnglishCounterW(self.q, path, None, ctypes.byref(self.c))
        if st:
            raise OSError(f"PdhAddCounter {path}: {st:#x}")
        pdh.PdhCollectQueryData(self.q)

    def collect(self) -> dict[str, float]:
        """Values since the previous collect, by instance name."""
        pdh.PdhCollectQueryData(self.q)
        size, count = w.DWORD(0), w.DWORD(0)
        st = pdh.PdhGetFormattedCounterArrayW(self.c, PDH_FMT_DOUBLE, ctypes.byref(size),
                                              ctypes.byref(count), None)
        if st != PDH_MORE_DATA:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        st = pdh.PdhGetFormattedCounterArrayW(self.c, PDH_FMT_DOUBLE, ctypes.byref(size),
                                              ctypes.byref(count), buf)
        if st:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        out: dict[str, float] = {}
        for i in range(count.value):
            it = items[i]
            # several instances can share a name (per-process engines); sum them
            out[it.szName] = out.get(it.szName, 0.0) + it.FmtValue.doubleValue
        return out


def classify(vals: dict[str, float]) -> dict[str, float]:
    out = {"3d": 0.0, "copy": 0.0, "other": 0.0}
    for name, v in vals.items():
        kind = name.rsplit("engtype_", 1)[-1]
        if kind == "3D":
            out["3d"] += v
        elif kind == "Copy":
            out["copy"] += v
        else:
            out["other"] += v
            out[kind] = out.get(kind, 0.0) + v
    return out


def run(pid: int, seconds: float, p: proc.Process, fc_addr: int):
    game = Counter(rf"\GPU Engine(pid_{pid}_*)\Utilization Percentage")
    allp = Counter(r"\GPU Engine(*engtype_3D)\Utilization Percentage")
    rows = []
    fc0 = p.u64(fc_addr)
    t0 = time.perf_counter()
    last_fc, last_t = fc0, t0
    not_front = 0
    end = t0 + seconds
    while True:
        nxt = last_t + 1.0
        time.sleep(max(0.0, nxt - time.perf_counter()))
        fc, t = p.u64(fc_addr), time.perf_counter()
        g = classify(game.collect())
        a = sum(allp.collect().values())
        fps = (fc - last_fc) / (t - last_t)
        rows.append({"fps": fps, "all3d": a, **g})
        last_fc, last_t = fc, t
        if t >= end:
            break
    elapsed = last_t - t0
    frames = last_fc - fc0
    fps = frames / elapsed
    m = {k: statistics.fmean(r.get(k, 0.0) for r in rows) for k in rows[0]}
    sd_fps = statistics.pstdev(r["fps"] for r in rows)
    sd_3d = statistics.pstdev(r["3d"] for r in rows)
    gpu_ms = m["3d"] / 100 * 1000 / fps if fps else 0.0
    return {"fps": fps, "sd_fps": sd_fps, "3d": m["3d"], "sd_3d": sd_3d, "copy": m["copy"],
            "other": m["other"], "all3d": m["all3d"], "gpu_ms": gpu_ms, "frame_ms": 1000 / fps if fps else 0,
            "not_front": not_front, "n": len(rows),
            "extra": {k: v for k, v in m.items() if k not in ("fps", "3d", "copy", "other", "all3d")}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--csv")
    a = ap.parse_args()
    p = proc.Process(a.pid)
    fc_addr = p.main.base + FRAME_COUNTER_RVA
    for i in range(a.runs):
        r = run(a.pid, a.seconds, p, fc_addr)
        line = (f"{a.label:14} fps {r['fps']:6.2f} (sd {r['sd_fps']:4.1f}) frame {r['frame_ms']:6.2f} ms  "
                f"3d {r['3d']:5.1f}% (sd {r['sd_3d']:4.1f})  gpu {r['gpu_ms']:6.2f} ms/frame  "
                f"copy {r['copy']:4.1f}% other {r['other']:4.1f}% all3d {r['all3d']:5.1f}%"
                + ("  " + " ".join(f"{k}={v:.1f}" for k, v in r["extra"].items() if v > 0.05)
                   if r["extra"] else ""))
        print(line, flush=True)
        if a.csv:
            new = not Path(a.csv).exists()
            with open(a.csv, "a") as f:
                if new:
                    f.write("time,label,fps,sd_fps,frame_ms,3d,sd_3d,gpu_ms,copy,other,all3d,not_front\n")
                f.write(f"{time.strftime('%H:%M:%S')},{a.label},{r['fps']:.3f},{r['sd_fps']:.3f},"
                        f"{r['frame_ms']:.3f},{r['3d']:.2f},{r['sd_3d']:.2f},{r['gpu_ms']:.3f},"
                        f"{r['copy']:.2f},{r['other']:.2f},{r['all3d']:.2f},{r['not_front']}\n")


if __name__ == "__main__":
    main()
