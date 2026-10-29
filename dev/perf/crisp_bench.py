"""
Crisp damage numbers bench (overnight, unattended). Uses the game's own NS_Damage_Numbers spawned
in front of the idle camera, scores the sharpness of each screenshot, and measures GPU time.

    python bench.py <pid> <outdir> [--n 10] [--skip-sharp] [--skip-gpu]
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Josh\Documents\projects\bl4-direct")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "dev" / "perf"))

BASIS = "r.Translucency.ScreenPercentage.Basis"
PERCENT = "r.SeparateTranslucencyScreenPercentage"
FXAA = "Gbx.FxaaAfterMotionBlur.Enabled"
FX_BEHIND = "/Game/Common/Effects/Systems/Explosions/Fire/NS_Explosion_Fire"


def bridge(pid, code, timeout=60):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "bridge.py"), str(pid), code],
                       capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    out = (r.stdout + r.stderr).strip()
    if "Traceback" in out:
        raise RuntimeError(out)
    return out


def shot(pid, path):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "desktop_agent.py"), "shot", str(pid), str(path)],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    if not Path(path).exists():
        raise RuntimeError(f"no screenshot: {r.stdout} {r.stderr}")


def load(path):
    return np.asarray(Image.open(path).convert("RGB")).astype(np.float32)


def sharpness(img, bg):
    """Normalized edge steepness of whatever appeared vs the background in the centre region.
    Returns (score, bbox) or (None, None). score = p95 gradient / intensity range in the bbox:
    a blurred edge spreads the same contrast over more pixels, so it scores lower."""
    h, w, _ = img.shape
    y0, y1, x0, x1 = int(h * 0.05), int(h * 0.6), int(w * 0.2), int(w * 0.55)
    a, b = img[y0:y1, x0:x1], bg[y0:y1, x0:x1]
    # the digits: near-white pixels that weren't white before (firelight is orange, not white)
    white = (a.min(axis=2) > 200) & (a.max(axis=2) - a.min(axis=2) < 40)
    was_white = (b.min(axis=2) > 200) & (b.max(axis=2) - b.min(axis=2) < 40)
    mask = white & ~was_white
    if mask.sum() < 80:
        return None, None
    ys, xs = np.nonzero(mask)
    # the biggest cluster: the number is one compact blob; take the dense box around the median
    cy, cx = np.median(ys), np.median(xs)
    near = (np.abs(ys - cy) < 80) & (np.abs(xs - cx) < 260)
    ys, xs = ys[near], xs[near]
    if len(ys) < 80:
        return None, None
    by0, by1 = max(0, ys.min() - 6), ys.max() + 7
    bx0, bx1 = max(0, xs.min() - 6), xs.max() + 7
    gray = (a[by0:by1, bx0:bx1] @ np.array([0.299, 0.587, 0.114], dtype=np.float32))
    if gray.shape[0] < 8 or gray.shape[1] < 8:
        return None, None
    gy, gx = np.gradient(gray)
    g = np.hypot(gx, gy)
    # the digit edges: dilate the white mask by 2 px and score gradients there
    m = mask[by0:by1, bx0:bx1]
    md = m.copy()
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            md |= np.roll(np.roll(m, dy, 0), dx, 1)
    rng = np.percentile(gray, 99) - np.percentile(gray, 1)
    if rng < 60:
        return None, None
    return float(np.percentile(g[md], 95) / rng), (y0 + by0, y0 + by1, x0 + bx0, x0 + bx1)


def crop(img, bbox, pad=12):
    y0, y1, x0, x1 = bbox
    return Image.fromarray(img[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad].astype(np.uint8))


def set_cond(pid, basis, fxaa, percent=100):
    return bridge(pid, f"b=crisp_bench; print(b.cvar('{BASIS}', {basis}), b.cvar('{FXAA}', {fxaa}), b.cvar('{PERCENT}', {percent}))")


def load_raw(path):
    b = Path(path).read_bytes()
    w, h = int.from_bytes(b[:4], "little"), int.from_bytes(b[4:8], "little")
    return np.frombuffer(b[8:], np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0]].astype(np.float32)


def wait_file(path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if Path(path).exists():
            return True
        if Path(str(path) + ".err").exists():
            raise RuntimeError(Path(str(path) + ".err").read_text())
        time.sleep(0.05)
    return False


GRAB_DELAYS = (0.05, 0.10)


def sharp_run(pid, out, n, log):
    conds = [("vanilla", 0, 1), ("sharp", 1, 1), ("sharp_nofxaa", 1, 0), ("vanilla_nofxaa", 0, 0)]
    # effects at fight-like distances: none, a shock burst behind the number, a fire explosion off to
    # the side, heavy smoke behind the number (path, dist, right, up)
    fx_modes = {"": None,
                "_shockbehind": ("/Game/Common/Effects/Systems/Explosions/Shock/NS_Explosion_Shock", 1500.0, -450.0, 225.0),
                "_fireside": ("/Game/Common/Effects/Systems/Explosions/Fire/NS_Explosion_Fire", 1500.0, 650.0, 0.0),
                "_smokebehind": ("/Game/Common/Effects/Systems/Smoke/NS_Smoke_Heavy_02", 1200.0, -360.0, 180.0)}
    results = {}
    raw = out / "raw"
    raw.mkdir(exist_ok=True)
    for fx_key, fx in fx_modes.items():
        for name, basis, fxaa in conds:
            key = f"{name}{fx_key}"
            set_cond(pid, basis, fxaa)
            time.sleep(1.5)
            scores = []
            for i in range(n):
                bgp = raw / f"{key}_{i}_bg.raw"
                bridge(pid, f"crisp_grab.later(r'{bgp}', 0.0)")
                wait_file(bgp)
                shots = [raw / f"{key}_{i}_{int(d * 1000)}.raw" for d in GRAB_DELAYS]
                grabs = ", ".join(f"crisp_grab.later(r'{p}', {d})" for p, d in zip(shots, GRAB_DELAYS))
                num = f"b.number({10000 + i * 1111}, 400.0, -120.0, 60.0, crit={i % 3 == 0})"
                # all timing on the game thread: the effect, then 0.3 s later the number and its grabs
                fx_call = f"b.effect('{fx[0]}', {fx[1]}, {fx[2]}, {fx[3]}); " if fx else ""
                delay = 0.3 if fx else 0.0
                bridge(pid, f"b=crisp_bench; {fx_call}b.after({delay}, lambda: ({num}, {grabs}))")
                bg = load_raw(bgp)
                for p, d in zip(shots, GRAB_DELAYS):
                    if not wait_file(p):
                        continue
                    img = load_raw(p)
                    s, bbox = sharpness(img, bg)
                    if s is not None:
                        scores.append(s)
                    if i < 4:
                        h, w, _ = img.shape  # the number's neighbourhood, for looking at
                        Image.fromarray(img[int(h * .15):int(h * .42), int(w * .30):int(w * .55)].astype(np.uint8)).save(out / f"view_{key}_{i}_{int(d * 1000)}.png")
                        if bbox is not None:
                            crop(img, bbox).save(out / f"crop_{key}_{i}_{int(d * 1000)}.png")
                    p.unlink(missing_ok=True)
                bgp.unlink(missing_ok=True)
                time.sleep(2.0)  # let the number and the effect finish
            results[key] = {"n": len(scores), "median": statistics.median(scores) if scores else None,
                            "mean": statistics.fmean(scores) if scores else None, "scores": scores}
            log(f"{key:24} n={len(scores):2}  median={results[key]['median']}")
    return results


def gpu_run(pid, out, log, seconds=12, rounds=3):
    import gpu_time
    import proc
    p = proc.Process(pid)
    conds = [("vanilla", 0, 1, 100), ("sharp100", 1, 1, 100), ("sharp75", 1, 1, 75), ("sharp50", 1, 1, 50)]
    res = {c[0]: [] for c in conds}
    for load_on in (False, True):
        if load_on:
            bridge(pid, "crisp_bench.load_on()")
        try:
            for r in range(rounds):
                for name, basis, fxaa, pct in conds:
                    set_cond(pid, basis, fxaa, pct)
                    time.sleep(3)
                    m = gpu_time.run(p, seconds)
                    key = f"{name}{'_load' if load_on else '_idle'}"
                    res.setdefault(key, []).append(m)
                    log(gpu_time.fmt(key, m))
        finally:
            if load_on:
                bridge(pid, "crisp_bench.load_off()")
    summary = {}
    for key, runs in res.items():
        if runs:
            summary[key] = {k: statistics.fmean(x[k] for x in runs) for k in ("fps", "gpu", "gpu_med", "gpu_p90", "ft_p99", "ft_max")}
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("out")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--skip-sharp", action="store_true")
    ap.add_argument("--skip-gpu", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / "log.txt", "a", encoding="utf-8")

    def log(s):
        line = f"{time.strftime('%H:%M:%S')} {s}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    bridge(a.pid, (HERE / "crisp_bench_game.py").read_text())
    bridge(a.pid, "crisp_bench.stops_on()")
    # the mod would fight our settings: off for the bench (it restores everything it changed)
    log(bridge(a.pid, "m=sys.modules['bl4_crisp_numbers']; m.mod.disable(); print('mod off', m.mod.is_enabled)"))
    fx_before = bridge(a.pid, f"import sys; cv=sys.modules['bl4_crisp_numbers'].cvars; crisp_fx_cv=cv.Console().cvar('{FXAA}'); crisp_fx_lvl=crisp_fx_cv.set_by; print(crisp_bench.get('{FXAA}'), hex(crisp_fx_lvl))")
    log(f"fxaa before (value, set-by level): {fx_before}")
    results = {}
    try:
        if not a.skip_sharp:
            results["sharp"] = sharp_run(a.pid, out, a.n, log)
            (out / "results.json").write_text(json.dumps(results, indent=1))
        if not a.skip_gpu:
            results["gpu"] = gpu_run(a.pid, out, log)
            (out / "results.json").write_text(json.dumps(results, indent=1))
    finally:
        try:
            bridge(a.pid, "crisp_bench.load_off(); crisp_bench.stops_off()")
        except Exception as ex:  # noqa: BLE001
            log(f"load_off: {ex}")
        log(set_cond(a.pid, 0, 1, 100))
        log(bridge(a.pid, "crisp_fx_cv.set_by = crisp_fx_lvl; print('fxaa level back', hex(crisp_fx_cv.set_by))"))
        log(bridge(a.pid, "m=sys.modules['bl4_crisp_numbers']; m.mod.enable(); print('mod on', m.mod.is_enabled)"))
        log("done")


if __name__ == "__main__":
    main()
