"""
The engine's own per-frame GPU time (what `stat unit` and the game's FPS/GPU overlay show), read
from outside the game. Read-only: nothing injected, nothing written.

    python dev/perf/gpu_time.py <pid> [--seconds 20] [--runs 1] [--label x] [--csv out.csv]

Globals (build 26007962, RVAs into Borderlands4.exe; u32 FPlatformTime cycles):
  0xCA84AC0  GGPUFrameTime     (GPU 0; the render thread stores it from the frame's timestamp queries)
  0xCA9A348  GGameThreadTime
  0xCA9A350  GRenderThreadTime
  0xCA9A358  GRHIThreadTime
  0xC83F420  FPlatformTime::SecondsPerCycle (double, 1e-7: QPC at 10 MHz)
Found by snapshotting .data for u32s in the 5-60 ms range that change every frame, then checked in
code: function RVA 0x3421A8C (the stat-unit / "bound by" update) loads exactly these four values,
scales each by SecondsPerCycle*1000 and picks the largest as the bottleneck (2 game, 3 render,
4 RHI, 5 GPU). The GPU value matched the in-game overlay's "GPU: 13.33" (13.34 ms) on first read.

Each game frame (GFrameCounter change, polled at ~2 kHz) records the four values. With frame
generation on, generated frames are not game frames and are not in GGPUFrameTime's span as far as
we can tell; the PDH 3D utilization (gpu_util.py) covers all of the game's GPU work.
"""

from __future__ import annotations

import argparse
import statistics
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402
from gpu_util import Counter, classify  # noqa: E402
from sampler import FRAME_COUNTER_RVA  # noqa: E402

GPU_RVA = 0xCA84AC0
THREADS_RVA = 0xCA9A348  # game @+0, render @+8, RHI @+0x10
SPC_RVA = 0xC83F420


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else 0.0


def run(p: proc.Process, seconds: float, with_pdh: bool = True):
    base = p.main.base
    spc_ms = struct.unpack("<d", p.read(base + SPC_RVA, 8))[0] * 1000
    if not 1e-8 < spc_ms / 1000 < 1e-5:
        raise SystemExit(f"SecondsPerCycle looks wrong ({spc_ms / 1000}); wrong build?")
    fc_addr = base + FRAME_COUNTER_RVA
    pdh = Counter(rf"\GPU Engine(pid_{p.pid}_*)\Utilization Percentage") if with_pdh else None
    gpu, game, rt, rhi, ft = [], [], [], [], []
    last_fc = p.u64(fc_addr)
    t0 = last_t = time.perf_counter()
    fc0 = last_fc
    end = t0 + seconds
    while True:
        now = time.perf_counter()
        if now >= end:
            break
        fc = p.u64(fc_addr)
        if fc != last_fc:
            g = struct.unpack("<I", p.read(base + GPU_RVA, 4))[0]
            th = struct.unpack("<IIIII", p.read(base + THREADS_RVA, 20))
            gpu.append(g * spc_ms)
            game.append(th[0] * spc_ms)
            rt.append(th[2] * spc_ms)
            rhi.append(th[4] * spc_ms)
            if fc == last_fc + 1:
                ft.append((now - last_t) * 1000)
            last_fc, last_t = fc, now
        time.sleep(0.0004)
    elapsed = time.perf_counter() - t0
    frames = last_fc - fc0
    util = classify(pdh.collect()) if pdh else {}
    fps = frames / elapsed
    return {
        "fps": fps, "frames": frames,
        "gpu": statistics.fmean(gpu) if gpu else 0, "gpu_med": statistics.median(gpu) if gpu else 0,
        "gpu_p10": pct(gpu, 0.1), "gpu_p90": pct(gpu, 0.9),
        "game": statistics.fmean(game) if game else 0, "render": statistics.fmean(rt) if rt else 0,
        "rhi": statistics.fmean(rhi) if rhi else 0,
        "ft_p99": pct(ft, 0.99), "ft_max": max(ft) if ft else 0,
        "util3d": util.get("3d", 0.0), "copy": util.get("copy", 0.0), "other": util.get("other", 0.0),
        # GPU 3D-engine busy ms per game frame, from Windows' counters
        "pdh_ms": util.get("3d", 0.0) / 100 * 1000 / fps if fps else 0.0,
    }


def fmt(label, r):
    return (f"{label:16} fps {r['fps']:6.2f}  gpu {r['gpu']:6.2f} (med {r['gpu_med']:5.2f} p10 {r['gpu_p10']:5.2f} "
            f"p90 {r['gpu_p90']:5.2f})  game {r['game']:5.2f} rt {r['render']:5.2f} rhi {r['rhi']:5.2f}  "
            f"3d {r['util3d']:5.1f}% pdh {r['pdh_ms']:5.2f} ms  ft p99 {r['ft_p99']:6.1f} max {r['ft_max']:6.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--csv")
    a = ap.parse_args()
    p = proc.Process(a.pid)
    cols = ["fps", "gpu", "gpu_med", "gpu_p10", "gpu_p90", "game", "render", "rhi", "util3d", "pdh_ms",
            "copy", "other", "ft_p99", "ft_max"]
    for _ in range(a.runs):
        r = run(p, a.seconds)
        print(fmt(a.label, r), flush=True)
        if a.csv:
            new = not Path(a.csv).exists()
            with open(a.csv, "a") as f:
                if new:
                    f.write("time,label," + ",".join(cols) + "\n")
                f.write(f"{time.strftime('%H:%M:%S')},{a.label}," + ",".join(f"{r[c]:.3f}" for c in cols) + "\n")


if __name__ == "__main__":
    main()
