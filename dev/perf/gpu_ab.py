"""
Interleaved A/B GPU-time comparisons on a live game, toggling through the bridge.

    python dev/perf/gpu_ab.py <pid> --a "<python for state A>" --b "<python for state B>"
        [--label name] [--pairs 2] [--seconds 15] [--settle 4] [--restore "<python>"]
        [--csv .perf/gpu.csv]

Runs A,B,A,B,... (`--pairs` times), each measured with gpu_time.run after `--settle` seconds,
and checks through the bridge before each run that the game is in front, not paused and shows no
cursor (a run that fails the check is marked and left out of the summary). `--restore` runs at
the end, even after an error or Ctrl+C. The A/B snippets run on the game thread via tools/bridge.py.
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402
from gpu_time import fmt, run  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STATE = r"""
import ctypes as _c
_u=_c.WinDLL('user32'); _u.GetForegroundWindow.restype=_c.c_void_p
_u.GetWindowThreadProcessId.argtypes=(_c.c_void_p,_c.POINTER(_c.c_ulong))
_p=_c.c_ulong(); _u.GetWindowThreadProcessId(_u.GetForegroundWindow(),_c.byref(_p))
from mods_base import get_pc, ENGINE
_w=ENGINE.GameViewport.World; _pc=get_pc()
_gs=unrealsdk.find_class('GameplayStatics').ClassDefaultObject
print('STATE', int(_p.value==_c.WinDLL('kernel32').GetCurrentProcessId()), int(bool(_gs.IsGamePaused(_w))), int(bool(_pc.bShowMouseCursor)), _w.Name)
"""


def bridge(pid: int, code: str) -> str:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "bridge.py"), str(pid), code],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    return (r.stdout + r.stderr).strip()


def state(pid: int):
    out = bridge(pid, STATE)
    for ln in out.splitlines():
        if ln.startswith("STATE"):
            _, front, paused, cursor, world = ln.split()[:5]
            return front == "1" and paused == "0" and cursor == "0", ln
    return False, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--label", default="ab")
    ap.add_argument("--pairs", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--settle", type=float, default=4)
    ap.add_argument("--restore")
    ap.add_argument("--csv", default=str(ROOT / ".perf" / "gpu.csv"))
    ap.add_argument("--shot", action="store_true", help="screenshot each side once (.perf/shot_<label>_<side>.png)")
    a = ap.parse_args()
    p = proc.Process(a.pid)
    res = {"A": [], "B": []}
    try:
        for i in range(a.pairs):
            for side, code in (("A", a.a), ("B", a.b)):
                out = bridge(a.pid, code)
                if out:
                    print(f"  [{side}] {out[:300]}")
                time.sleep(a.settle)
                if a.shot and i == 0:
                    subprocess.run([sys.executable, str(ROOT / "tools" / "desktop_agent.py"), "shot", str(a.pid),
                                    str(ROOT / ".perf" / f"shot_{a.label}_{side}.png")], capture_output=True,
                                   timeout=60, cwd=ROOT)
                ok, st = state(a.pid)
                r = run(p, a.seconds)
                ok2, st2 = state(a.pid)
                good = ok and ok2
                tag = f"{a.label}:{side}{i + 1}"
                print(fmt(tag, r) + ("" if good else f"  BAD STATE {st} / {st2}"), flush=True)
                if good:
                    res[side].append(r)
                with open(a.csv, "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')},{tag}{'' if good else '!'},"
                            + ",".join(f"{r[c]:.3f}" for c in ("fps", "gpu", "gpu_med", "gpu_p10", "gpu_p90",
                                                               "game", "render", "rhi", "util3d", "pdh_ms",
                                                               "copy", "other", "ft_p99", "ft_max")) + "\n")
    finally:
        if a.restore:
            print("  [restore]", bridge(a.pid, a.restore)[:300])
    for key in ("gpu", "pdh_ms", "fps", "game", "util3d"):
        av = [r[key] for r in res["A"]]
        bv = [r[key] for r in res["B"]]
        if av and bv:
            ma, mb = statistics.fmean(av), statistics.fmean(bv)
            print(f"  {key:7} A {ma:7.2f} [{min(av):.2f}-{max(av):.2f}]  B {mb:7.2f} [{min(bv):.2f}-{max(bv):.2f}]"
                  f"  B-A {mb - ma:+.2f}")


if __name__ == "__main__":
    main()
