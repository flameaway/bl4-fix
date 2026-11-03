"""
Checks Kairosdex's picture.compose (mods/bl4_weapondex/picture.py) offline: it must make byte for
byte the picture the first version made (kept below as `reference`), and let go of the GIL while
it works (it runs on a worker thread while the game plays).

    python dev/kairosdex_compose_test.py [--raw DIR] [--quick]

--raw DIR: real captures saved by a bake with bake.KEEP_RAW (`<boss>.hdr.raw`, `.base.raw`,
`.normal.raw`, `.depth.raw`, 512x512): each boss is checked too.
--quick: skip the timings.
"""

from __future__ import annotations

import argparse
import importlib
import math
import random
import statistics
import struct
import sys
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_weapondex"
pkg = types.ModuleType("kdx_compose_test")
pkg.__path__ = [str(MOD)]
sys.modules["kdx_compose_test"] = pkg
picture = importlib.import_module("kdx_compose_test.picture")
P = picture

results: list[tuple[bool, str]] = []


def check(ok: object, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


# region The first version (2026-11-02), unchanged but for reading the module's constants


def reference(width: int, height: int, hdr: bytes, base: bytes, normal: bytes, depth: bytes) -> bytes:
    n = width * height
    a = struct.unpack(f"<{n * 4}f", hdr)
    c = struct.unpack(f"<{n * 4}f", base)
    nm = struct.unpack(f"<{n * 4}f", normal)
    dp = struct.unpack(f"<{n * 4}f", depth)
    inside = [a[i * 4 + 3] < 1 and dp[i * 4] < P.NO_DEPTH for i in range(n)]
    unpainted = [inside[i] and min(c[i * 4], c[i * 4 + 1], c[i * 4 + 2]) >= P.UNPAINTED for i in range(n)]

    def lum(f: tuple[float, ...], i: int) -> float:
        return 0.2126 * f[i * 4] + 0.7152 * f[i * 4 + 1] + 0.0722 * f[i * 4 + 2]

    paint = sorted(lum(c, i) for i in range(n) if inside[i])
    if paint and paint[int(len(paint) * P.BLACK_SHARE)] < P.BLACK_PAINT:
        unpainted = inside[:]
    if not any(inside):
        raise ValueError("the capture is empty")
    light = [lum(a, i) / max(lum(c, i), 0.02) if inside[i] and not unpainted[i] else 0.0 for i in range(n)]
    lit = sorted(light[i] for i in range(n) if inside[i] and not unpainted[i])
    median = (lit[len(lit) // 2] or 1.0) if lit else 1.0
    raw = sorted(max(lum(a, i), 0.0) for i in range(n) if unpainted[i])
    raw_median = (raw[len(raw) // 2] if raw else 0.0) or 1e-4
    hue_sum = [0.0, 0.0, 0.0]
    hued = 0
    for i in range(n):
        if unpainted[i] and (v := lum(a, i)) > P.HUE_FLOOR * raw_median:
            for k in range(3):
                hue_sum[k] += max(a[i * 4 + k], 0.0) / v
            hued += 1
    avg_hue = [h / hued for h in hue_sum] if hued else [1.0, 1.0, 1.0]
    out = bytearray(n * 4)
    for y in range(height):
        for x in range(width):
            i = y * width + x
            if not inside[i]:
                continue
            if unpainted[i]:
                v = max(lum(a, i), 0.0)
                hue = [max(a[i * 4 + k], 0.0) / v for k in range(3)] if v > P.HUE_FLOOR * raw_median else avg_hue
                level = P.MID_PAINT * min(max((v / raw_median) ** P.UNPAINTED_CURVE, P.UNPAINTED_LOW), P.UNPAINTED_HIGH)
                r, g, b = hue[0] * level, hue[1] * level, hue[2] * level
            else:
                shade = P.AMBIENT + P.LIGHT_GAIN * min(light[i] / median, P.LIGHT_MAX) * (1 - P.AMBIENT)
                r, g, b = c[i * 4] * shade, c[i * 4 + 1] * shade, c[i * 4 + 2] * shade
            if _ref_inked(i, x, y, width, height, inside, nm, dp):
                r, g, b = r * P.INK, g * P.INK, b * P.INK
            out[i * 4 : i * 4 + 4] = bytes((P._srgb(r), P._srgb(g), P._srgb(b), 255))  # noqa: SLF001
    return bytes(out)


def _ref_inked(i: int, x: int, y: int, width: int, height: int, inside: list[bool], nm: tuple[float, ...], dp: tuple[float, ...]) -> bool:
    if x == 0 or y == 0 or x == width - 1 or y == height - 1:
        return True
    d = dp[i * 4]
    for j in (i - 1, i + 1, i - width, i + width):
        if not inside[j]:
            return True
        if abs(dp[j * 4] - d) > P.INK_DEPTH * max(d, 1.0):
            return True
        if nm[i * 4] * nm[j * 4] + nm[i * 4 + 1] * nm[j * 4 + 1] + nm[i * 4 + 2] * nm[j * 4 + 2] < P.INK_NORMAL:
            return True
    return False


# endregion
# region Made-up captures


def pack(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def scene(width: int, height: int, seed: int, *, black: bool = False, white: bool = False, empty: bool = False) -> tuple[bytes, ...]:
    """
    A blob of a boss: painted parts, a white-paint (unpainted) patch of fur, dark and negative
    light, an effect cloud with no depth, and an arm reaching the frame's edge.
    """
    rnd = random.Random(seed)
    hdr, base, normal, depth = [], [], [], []
    cx, cy = width * rnd.uniform(0.4, 0.6), height * rnd.uniform(0.4, 0.6)
    rx, ry = width * rnd.uniform(0.25, 0.4), height * rnd.uniform(0.3, 0.45)
    for y in range(height):
        for x in range(width):
            u, v = (x - cx) / rx, (y - cy) / ry
            r2 = u * u + v * v
            arm = abs(y - cy) < height * 0.05 and x > cx  # reaches the right edge
            cloud = not empty and 0.9 < r2 < 1.3 and x < cx  # drawn, see-through-ish, no depth
            body = not empty and (r2 < 1 or arm)
            if body:
                nz = math.sqrt(max(0.0, 1 - min(r2, 1)))
                nx, ny = u * 0.9, v * 0.9
                if rnd.random() < 0.05:  # surface noise: ink from normals
                    nx, ny, nz = rnd.uniform(-1, 1), rnd.uniform(-1, 1), rnd.uniform(-1, 1)
                dist = 1500 + 200 * (1 - nz) + (300 if (x // 37 + y // 41) % 5 == 0 else 0)  # depth steps
                fur = white or (u > 0.2 and v < -0.1)
                if black:
                    paint = [rnd.uniform(0, 0.002)] * 3
                elif fur:
                    paint = [rnd.uniform(0.95, 1.0) for _ in range(3)]
                else:
                    band = (x // 23 + y // 29) % 3
                    paint = [[0.6, 0.1, 0.05], [0.05, 0.3, 0.5], [0.002, 0.002, 0.002]][band]
                    paint = [p * rnd.uniform(0.8, 1.2) for p in paint]
                lit = max(0.0, 0.3 + 0.7 * nx * 0.6 + 0.4 * nz) * rnd.uniform(0.5, 3.0)
                if rnd.random() < 0.02:
                    lit = -rnd.uniform(0, 0.01)  # HDR can dip below 0
                if rnd.random() < 0.01:
                    lit = 0.0
                tint = [rnd.uniform(0.7, 1.3) for _ in range(3)]
                hdr += [paint[k] * lit * tint[k] if not fur else lit * tint[k] * 0.4 for k in range(3)] + [0.0]
                base += [*paint, 1.0]
                normal += [nx, ny, nz, 0.0]
                depth += [dist, 0.0, 0.0, 0.0]
            elif cloud:
                hdr += [0.3, 0.3, 0.3, 0.5]
                base += [0.0, 0.0, 0.0, 0.0]
                normal += [0.0, 0.0, 0.0, 0.0]
                depth += [65504.0, 0.0, 0.0, 0.0]
            else:
                hdr += [0.0, 0.0, 0.0, 1.0]
                base += [0.0, 0.0, 0.0, 0.0]
                normal += [0.0, 0.0, 0.0, 0.0]
                depth += [65504.0, 0.0, 0.0, 0.0]
    return tuple(pack(p) for p in (hdr, base, normal, depth))


# endregion


def gil_gaps(work: callable) -> tuple[float, float, float]:
    """Runs `work` on a thread; meanwhile this thread wakes every 0.5 ms. Returns its longest
    and 99th-percentile wait past the wake time (ms), and the work's time (s)."""
    done = threading.Event()
    took = [0.0]

    def run() -> None:
        t = time.perf_counter()
        work()
        took[0] = time.perf_counter() - t
        done.set()

    gaps = []
    th = threading.Thread(target=run)
    th.start()
    while not done.is_set():
        t = time.perf_counter()
        time.sleep(0.0005)
        gaps.append((time.perf_counter() - t - 0.0005) * 1000)
    th.join()
    gaps.sort()
    return gaps[-1], gaps[int(len(gaps) * 0.99)], took[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    # The sRGB steps: the same byte as the power function, everywhere
    rnd = random.Random(1)
    xs = [rnd.uniform(-0.05, 1.3) for _ in range(200_000)] + [rnd.uniform(0, 0.005) for _ in range(50_000)]
    for s in P._steps():  # noqa: SLF001
        xs += [s, math.nextafter(s, -math.inf), math.nextafter(s, math.inf)]
    xs += [0.0, -0.0, 1.0, 2.0, 1e30, -1e30, 0.0031308, math.nextafter(0.0031308, 1)]
    bad = [x for x in xs if P._srgb(x) != P._srgb_fast(x)]  # noqa: SLF001
    check(not bad, f"_srgb_fast matches _srgb on {len(xs)} values" + (f" (first miss {bad[0]!r})" if bad else ""))

    # _kth: the k-th smallest, sampled or not
    breathe = P._Breather()  # noqa: SLF001
    for n, k in ((10, 3), (100_000, 50_000), (100_000, 90_000), (100_000, 0), (100_000, 99_999), (70_001, 35_000)):
        vals = [rnd.random() ** 3 for _ in range(n)]
        check(P._kth(vals, k, breathe) == sorted(vals)[k], f"_kth({n}, {k})")  # noqa: SLF001
    vals = [0.5] * 60_000 + [rnd.random() for _ in range(40_000)]  # many equal values
    check(P._kth(vals, 30_000, breathe) == sorted(vals)[30_000], "_kth with many equal values")  # noqa: SLF001
    # Nearly all one value (Logistics Manager Reyes's paint: black nearly everywhere; this recursed
    # without end in game, 2026-11-02), all one value, and two values
    for label, vals in (("97% one value", [0.0] * 97_000 + [rnd.random() for _ in range(3_000)]),
                        ("all one value", [0.25] * 120_000),
                        ("two values", [0.0] * 50_000 + [1.0] * 50_000),
                        ("70% one value, widely spread rest", [rnd.random() * 1e-6 for _ in range(30_000)] + [0.5] * 70_000)):  # fmt: skip
        rnd.shuffle(vals)
        ok = all(P._kth(vals, k, breathe) == sorted(vals)[k] for k in (0, len(vals) // 2, int(len(vals) * 0.9), len(vals) - 1))  # noqa: SLF001
        check(ok, f"_kth: {label}")

    # Made-up captures: every branch of compose
    cases = [("small 61x47", 61, 47, {}), ("black paint", 80, 64, {"black": True}), ("all white paint", 80, 64, {"white": True})]
    cases += [("black paint 512x512", 512, 512, {"black": True})]  # big enough for _kth's sampling
    cases += [(f"seed {s} 96x80", 96, 80, {}) for s in range(5)]
    cases += [("512x512", 512, 512, {})]
    for i, (name, w, h, kw) in enumerate(cases):
        caps = scene(w, h, i, **kw)
        check(P.compose(w, h, *caps) == reference(w, h, *caps), f"compose = first version: {name}")
    caps = scene(40, 30, 0, empty=True)
    for fn, label in ((P.compose, "compose"), (reference, "first version")):
        try:
            fn(40, 30, *caps)
            check(False, f"{label}: an empty capture raises")
        except ValueError:
            check(True, f"{label}: an empty capture raises")
    try:
        P.compose(40, 30, b"\0" * 16, *caps[1:])
        check(False, "compose: a short pass raises")
    except ValueError:
        check(True, "compose: a short pass raises")

    # Real captures
    if args.raw:
        files = sorted(args.raw.glob("*.hdr.raw"))
        check(files, f"real captures in {args.raw}")
        for f in files:
            boss = f.name[: -len(".hdr.raw")]
            caps = tuple((args.raw / f"{boss}.{p}.raw").read_bytes() for p in ("hdr", "base", "normal", "depth"))
            try:
                ref = reference(512, 512, *caps)
            except ValueError:
                ref = None
            try:
                got = P.compose(512, 512, *caps)
            except ValueError:
                got = None
            check(got == ref, f"compose = first version: {boss}")

    if not args.quick:
        caps = scene(512, 512, 7)

        def best(fn: callable, runs: int = 3) -> float:
            times = []
            for _ in range(runs):
                t = time.perf_counter()
                fn()
                times.append(time.perf_counter() - t)
            return min(times)

        old = best(lambda: reference(512, 512, *caps), 2)
        slice_s = P.SLICE_S
        P.SLICE_S = 1e9  # no breaks: the work alone
        try:
            work = best(lambda: P.compose(512, 512, *caps))
        finally:
            P.SLICE_S = slice_s
        wall = best(lambda: P.compose(512, 512, *caps))
        print(f"INFO  512x512: first version {old:.2f} s; now {work:.2f} s of work ({old / work:.1f}x), {wall:.2f} s with its breaks")
        check(work < old, "compose does less work than the first version")
        idle = gil_gaps(lambda: time.sleep(1.0))
        print(f"INFO  this machine, nothing running: a 0.5 ms sleep overshoots by up to {idle[0]:.1f} ms (p99 {idle[1]:.1f} ms)")
        worst, p99, took = gil_gaps(lambda: reference(512, 512, *caps))
        print(f"INFO  first version on a thread: another thread waited up to {worst:.1f} ms (p99 {p99:.1f} ms)")
        worst, p99, took = gil_gaps(lambda: P.compose(512, 512, *caps))
        print(f"INFO  now, on a thread: another thread waited up to {worst:.1f} ms (p99 {p99:.1f} ms)")
        # The worst single wait is at the OS's mercy (a busy machine preempts either thread): p99
        check(p99 < 3.0, f"compose lets another thread in within 3 ms, 99% of the time (p99 {p99:.1f} ms)")

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
