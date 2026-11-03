"""
Turns a raw HDR capture of a boss into the dex's picture (plain Python, no game access).

Scene captures ignore the exposure settings, so the bake captures raw HDR scene colour, which the
game exports as a Radiance .hdr file, and we tone map it here:
- brightness is levelled per picture (the brighter parts of the boss land at the same place every
  time), so all pictures look alike whatever the lighting where they were taken;
- a filmic curve (ACES fit) keeps highlights from clipping, then sRGB gamma;
- pixels the boss doesn't cover are exactly 0 in the capture (only the boss is drawn), so they
  become transparent.
"""

from __future__ import annotations

import array
import bisect
import json
import math
import os
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

# The share of the boss's pixels allowed to be brighter than the level we expose for
BRIGHT_SHARE = 0.03
TARGET = 1.6  # where that level lands before the curve
MID_TARGET = 0.25  # from_capture: where the median brightness lands before the curve
BRIGHT_LIMIT = 4.0  # from_capture: the most the bright level may land at


def read_hdr(path: Path) -> tuple[int, int, list[tuple[float, float, float]]]:
    data = path.read_bytes()
    pos = 0
    while True:  # header, then a blank line
        end = data.index(b"\n", pos)
        line = data[pos:end]
        pos = end + 1
        if not line.strip():
            break
    end = data.index(b"\n", pos)
    parts = data[pos:end].split()
    pos = end + 1
    height, width = int(parts[1]), int(parts[3])
    pixels: list[tuple[float, float, float]] = []
    for _ in range(height):
        if data[pos] == 2 and data[pos + 1] == 2 and (data[pos + 2] << 8 | data[pos + 3]) == width:
            # Adaptive RLE: each of R, G, B, E stored in turn
            pos += 4
            channels = []
            for _c in range(4):
                row = bytearray()
                while len(row) < width:
                    count = data[pos]
                    pos += 1
                    if count > 128:
                        row += bytes([data[pos]]) * (count - 128)
                        pos += 1
                    else:
                        row += data[pos : pos + count]
                        pos += count
                channels.append(row)
            r, g, b, e = channels
            for x in range(width):
                pixels.append(_rgbe(r[x], g[x], b[x], e[x]))
        else:
            for _x in range(width):
                pixels.append(_rgbe(data[pos], data[pos + 1], data[pos + 2], data[pos + 3]))
                pos += 4
    return width, height, pixels


def _rgbe(r: int, g: int, b: int, e: int) -> tuple[float, float, float]:
    if e == 0:
        return (0.0, 0.0, 0.0)
    scale = 2.0 ** (e - 136)  # (e - 128) - 8
    return (r * scale, g * scale, b * scale)


def _aces(x: float) -> float:
    return max(0.0, min(1.0, (x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14)))


def _srgb(x: float) -> int:
    v = 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return max(0, min(255, round(v * 255)))


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))  # fmt: skip
    # The bake's writer and the small-copy backfill may write at once: each uses its own temp file
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_bytes(png)
    tmp.replace(path)


# region Small copies
# Every list thumbnail used to load the full picture (512x512 per boss, 512x256 per item, ~440 of
# them once the item list had scrolled by). Each picture gets a quarter-size copy in small/ that
# the page uses wherever it draws a thumbnail; the full one only in the details.

SMALL_DIR = "small"
PNG_SIGNATURE = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))
SMALL_FACTOR = 4  # 512x512 -> 128x128, 512x256 -> 128x64
SLICE_S = 0.001  # work this long, then sleep: a worker in pure Python holds the GIL the whole time
# ...this long: enough for a waiting thread to take the GIL. It was SLICE_S, which halved the pace;
# 0.1 ms lets another thread in as soon (p99 1.7-1.8 ms against 1.7-2.0) and compose took 25-30%
# less time (real captures, offline, 2026-11-03). sleep(0) starves it (it takes the GIL straight back)
BREATH_S = 0.0001


def small_path(png: Path) -> Path:
    return png.parent / SMALL_DIR / png.name


PNG_END = bytes.fromhex("0000000049454e44ae426082")  # the IEND chunk every whole PNG ends with
# path -> (size, mtime_ns) of files found whole: each is opened once a session (until it changes)
_WHOLE: dict[str, tuple[int, int]] = {}
_WHOLE_LOCK = threading.Lock()


def whole_pngs(folder: Path) -> list[str]:
    """
    The names (no .png) of the folder's pictures that are whole: the PNG signature first and the
    IEND chunk last. An empty file or one cut short (an unpack or copy that stopped part way)
    counts as missing, so it's made again. Opening every file is slow cold (~870 files: 6.4 s,
    0.15 s warm; 2026-11-03), so a file found whole isn't opened again while its size and date
    stay the same: a later scan is a folder listing (~2 ms). Any thread.
    """
    out = []
    try:
        entries = [e for e in os.scandir(folder) if e.name.endswith(".png") and e.is_file()]
    except OSError:
        return []
    for entry in entries:
        try:
            st = entry.stat()
        except OSError:
            continue
        stamp = (st.st_size, st.st_mtime_ns)
        with _WHOLE_LOCK:
            known = _WHOLE.get(entry.path) == stamp
        if not known:
            if not _is_whole(entry.path, st.st_size):
                continue
            with _WHOLE_LOCK:
                _WHOLE[entry.path] = stamp
        out.append(entry.name[:-4])
    return sorted(out)


def _is_whole(path: str, size: int) -> bool:
    if size < len(PNG_SIGNATURE) + len(PNG_END):
        return False
    try:
        with open(path, "rb") as f:
            if f.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                return False
            f.seek(-len(PNG_END), os.SEEK_END)
            return f.read(len(PNG_END)) == PNG_END
    except OSError:
        return False


def shrink(width: int, height: int, rgba: bytes, factor: int = SMALL_FACTOR) -> tuple[int, int, bytes]:
    """
    A box filter weighted by alpha (transparent pixels don't darken the edges). Pure Python, ~0.1 s
    for a boss picture, so it lets go of the GIL every millisecond: on a worker thread a long loop
    makes every mod's hooks wait for it. sleep(0) wouldn't do (it takes the GIL straight back).
    """
    w, h = width // factor, height // factor
    out = bytearray(w * h * 4)
    stride = width * 4
    span = factor * 4
    count = factor * factor
    until = time.perf_counter() + SLICE_S
    for oy in range(h):
        rows = [rgba[(oy * factor + dy) * stride : (oy * factor + dy + 1) * stride] for dy in range(factor)]
        for ox in range(w):
            r = g = b = a = 0
            x0 = ox * span
            for row in rows:
                for i in range(x0, x0 + span, 4):
                    pa = row[i + 3]
                    if pa:
                        r += row[i] * pa
                        g += row[i + 1] * pa
                        b += row[i + 2] * pa
                        a += pa
            if a:
                o = (oy * w + ox) * 4
                out[o] = r // a
                out[o + 1] = g // a
                out[o + 2] = b // a
                out[o + 3] = (a + count // 2) // count
            if time.perf_counter() > until:
                time.sleep(BREATH_S)
                until = time.perf_counter() + SLICE_S
    return w, h, bytes(out)


def write_small(png: Path, width: int, height: int, rgba: bytes) -> None:
    """small/<name> for a picture just written from these pixels, and small/index.json."""
    w, h, small = shrink(width, height, rgba)
    target = small_path(png)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_png(target, w, h, small)
    write_small_index(png.parent)


def write_index(folder: Path) -> None:
    """
    index.json, which the page reads: {name: version} for each picture, the version being its
    file's date, which the page puts in the picture's address so a picture taken again shows at once
    instead of the copy it already loaded. Each writer writes its own temp file and swaps it in.
    """
    index = {p.stem: int(p.stat().st_mtime) for p in sorted(folder.glob("*.png"))}
    tmp = folder / f"index.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(folder / "index.json")


def write_small_index(folder: Path) -> None:
    """small/index.json for the page: {name: version (the file's date)}, as the full pictures'."""
    small = folder / SMALL_DIR
    if not small.is_dir():
        return
    index = {p.stem: int(p.stat().st_mtime) for p in sorted(small.glob("*.png"))}
    tmp = small / f"index.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(small / "index.json")


def read_png(path: Path) -> tuple[int, int, bytes] | None:
    """One of our own PNGs (write_png: 8-bit RGBA, no row filters); None for anything else."""
    data = path.read_bytes()
    if data[:8] != PNG_SIGNATURE:
        return None
    pos = 8
    width = height = 0
    idat = []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or colour != 6 or interlace:
                return None
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(b"".join(idat))
    stride = width * 4 + 1
    if len(raw) != stride * height or any(raw[y * stride] for y in range(height)):
        return None
    return width, height, b"".join(raw[y * stride + 1 : (y + 1) * stride] for y in range(height))


def backfill(folders: list[Path]) -> int:
    """
    Makes the small copy of every picture that has none (or an older one): pictures taken before
    small copies existed. Worker thread only. Returns how many it made.
    """
    made = 0
    for folder in folders:
        if not folder.is_dir():
            continue
        done_here = 0
        for png in sorted(folder.glob("*.png")):
            target = small_path(png)
            try:
                if target.exists() and target.stat().st_mtime >= png.stat().st_mtime:
                    continue
                pixels = read_png(png)
                if pixels is None:
                    continue
                w, h, small = shrink(*pixels)
                target.parent.mkdir(parents=True, exist_ok=True)
                write_png(target, w, h, small)
            except OSError:
                continue
            made += 1
            done_here += 1
            if done_here % 20 == 0:
                write_small_index(folder)  # the page picks them up as they come
            time.sleep(0.005)
        write_small_index(folder)
    return made


# endregion


def from_capture(width: int, height: int, raw: bytes) -> bytes:
    """
    Tone maps a capture read straight from its render target (float RGBA per pixel, as
    ReadRenderTargetRaw gives it) into 8-bit RGBA. The capture's alpha is "how see-through": 0
    where the boss is drawn, 1 where nothing is, so that's the picture's transparency (dark parts
    of the boss stay solid, unlike the .hdr files' black-is-empty).
    """
    f = struct.unpack(f"<{width * height * 4}f", raw)
    lums = sorted(0.2126 * f[i] + 0.7152 * f[i + 1] + 0.0722 * f[i + 2] for i in range(0, len(f), 4) if f[i + 3] < 1)
    if not lums:
        raise ValueError("the capture is empty")
    mid = lums[len(lums) // 2]
    bright = lums[min(len(lums) - 1, int(len(lums) * (1 - BRIGHT_SHARE)))]
    # Mid-tones land at the same place in every picture, unless that would blow out the bright parts
    k = min(MID_TARGET / mid if mid > 0 else math.inf, BRIGHT_LIMIT / bright if bright > 0 else math.inf)
    if not math.isfinite(k):
        k = 1.0
    out = bytearray(width * height * 4)
    for p in range(width * height):
        i = p * 4
        a = f[i + 3]
        if a < 1:
            out[i : i + 4] = bytes((_srgb(_aces(f[i] * k)), _srgb(_aces(f[i + 1] * k)), _srgb(_aces(f[i + 2] * k)),
                                    max(0, min(255, round((1 - a) * 255)))))  # fmt: skip
    return bytes(out)


# compose(): the look picked on Idolator Sol and Brain Zane
AMBIENT = 0.6  # the darkest a surface gets, as a share of its paint colour
LIGHT_GAIN = 0.6  # how much the HDR shot's light adds on top
LIGHT_MAX = 1.6  # light above this (levelled: 1 = the median lit surface) adds no more
INK = 0.12  # an ink line's brightness, as a share of what's under it
INK_DEPTH = 0.015  # a depth step of this share of the distance draws a line
INK_NORMAL = 0.75  # neighbouring surfaces facing apart more than this (cosine) draw a line
NO_DEPTH = 60000.0  # the depth pass reads 65504 (half float's largest) where nothing solid is
UNPAINTED = 0.95  # a paint colour this white in all three channels is a material that gives none
MID_PAINT = 0.3  # an unpainted pixel at its own median brightness lands here (linear)
UNPAINTED_CURVE = 0.4  # unpainted brightness: (shot / median) ** this, kept within LOW..HIGH
UNPAINTED_LOW = 0.55
UNPAINTED_HIGH = 1.25
HUE_FLOOR = 0.2  # unpainted pixels darker than this share of their median take the average hue
BLACK_PAINT = 0.004  # paint darker than this ...
BLACK_SHARE = 0.9  # ... on this share of the boss: its materials give no paint colour at all


CHUNK = 512  # pixels per step of compose's quick loops: the clock is read between steps (_Breather)
SLOW_CHUNK = 256  # the same for its per-pixel loops (~2 µs a pixel)
KTH_SORT = 16384  # _kth: lists up to this long are just sorted (a few ms at most)


class _Breather:
    """
    Lets go of the GIL every SLICE_S of work (as shrink does): compose runs on a worker thread
    for about a second, and in one go it made every mod's hooks on the game thread wait for it.
    """

    def __init__(self) -> None:
        self.until = time.perf_counter() + SLICE_S

    def __call__(self) -> None:
        if time.perf_counter() > self.until:
            time.sleep(BREATH_S)
            self.until = time.perf_counter() + SLICE_S

    def steps(self, n: int, size: int = CHUNK) -> range:
        """Start indices of `size`-long steps through n items (call self() after each)."""
        return range(0, n, size)


def _kth(values: list[float], k: int, breathe: _Breather) -> float:
    """
    sorted(values)[k], without sorting it all in one go (a C sort of ~100k floats holds the GIL
    for ~15 ms): keeps the values between two pivots picked from a sample, then sorts those.
    """
    n = len(values)
    if n <= KTH_SORT:
        return sorted(values)[k]
    sample = sorted(values[:: max(1, n // 1024)])
    m = len(sample)
    # The sample's k-th share, widened: the true k-th value is almost always in between
    lo = sample[max(0, k * m // n - 32)]
    hi = sample[min(m - 1, k * m // n + 32)]
    below = 0
    middle: list[float] = []
    for s in breathe.steps(n):
        part = values[s : s + CHUNK]
        below += sum(1 for v in part if v < lo)
        middle += [v for v in part if lo <= v <= hi]
        breathe()
    if below <= k < below + len(middle):
        if lo == hi:
            return lo  # all of the middle is that one value (Logistics Manager Reyes: black nearly everywhere)
        if KTH_SORT * 4 < len(middle) <= n // 2:
            return _kth(middle, k - below, breathe)  # it shrank: narrow it down again
        return sorted(middle)[k - below]
    return sorted(values)[k]  # the sample missed it (very unlikely): the plain way


def _floats(blob: bytes, n: int) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    if len(values) != n * 4:
        raise ValueError(f"a pass holds {len(values) // 4} pixels, expected {n}")
    if sys.byteorder != "little":
        values.byteswap()
    return values


def compose(width: int, height: int, hdr: bytes, base: bytes, normal: bytes, depth: bytes) -> bytes:
    """
    The picture, drawn from captures of the same frozen view (float RGBA per pixel each):
    - hdr: lit scene colour; its alpha is how see-through (0 where the boss is), its light per
      pixel (lit over paint brightness, levelled on the median) shades the paint between AMBIENT
      and full;
    - base: paint colour (linear), which is what keeps every boss's true colours;
    - normal, depth: where surfaces turn or step away, and the silhouette, get ink lines, as the
      game's own outlines do.

    Worker thread: pure Python, so it lets go of the GIL every millisecond (_Breather; the first
    version held it up to ~70 ms at a time, in its sorts and when it returned). Works channel by
    channel on the boss's own pixels only: byte for byte the first version's picture
    (dev/kairosdex_compose_test.py) for ~half the work (0.48 s against 0.84 s offline); with its
    breaks it takes about as long as before.
    """
    n = width * height
    breathe = _Breather()
    a, c, nm, dp = (_floats(blob, n) for blob in (hdr, base, normal, depth))
    a_r, a_g, a_b, a_a = a[0::4], a[1::4], a[2::4], a[3::4]
    c_r, c_g, c_b = c[0::4], c[1::4], c[2::4]
    n_x, n_y, n_z = nm[0::4], nm[1::4], nm[2::4]
    d = dp[0::4]
    del a, c, nm, dp
    breathe()
    # The boss: drawn in the HDR shot and solid (effects like smoke and particle clouds are drawn but
    # have no depth or paint, and came out as black blobs)
    inside = [False] * n
    idx: list[int] = []  # the boss's pixels, in order
    for s in breathe.steps(n):
        e = min(n, s + CHUNK)
        here = [x < 1 and z < NO_DEPTH for x, z in zip(a_a[s:e], d[s:e])]
        inside[s:e] = here
        idx += [s + j for j, v in enumerate(here) if v]
        breathe()
    del a_a
    if not idx:
        raise ValueError("the capture is empty")
    m = len(idx)
    # Per boss pixel (in idx order): HDR and paint brightness, paint colour
    pr: list[float] = []  # paint
    pg: list[float] = []
    pb: list[float] = []
    hr: list[float] = []  # HDR
    hg: list[float] = []
    hb: list[float] = []
    lum_a: list[float] = []
    lum_c: list[float] = []
    unpainted: list[bool] = []
    for s in breathe.steps(m):
        part = idx[s : s + CHUNK]
        cr, cg, cb = [c_r[i] for i in part], [c_g[i] for i in part], [c_b[i] for i in part]
        ar, ag, ab = [a_r[i] for i in part], [a_g[i] for i in part], [a_b[i] for i in part]
        pr += cr
        pg += cg
        pb += cb
        hr += ar
        hg += ag
        hb += ab
        lum_a += [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in zip(ar, ag, ab)]
        lum_c += [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in zip(cr, cg, cb)]
        # Some materials give no paint colour (it reads pure white): those pixels take their colour
        # from the HDR shot instead
        unpainted += [min(r, g, b) >= UNPAINTED for r, g, b in zip(cr, cg, cb)]
        breathe()
    # Others give none as pure black, all over (Logistics Manager Reyes, Volcanus): a boss whose paint
    # is black nearly everywhere is all unpainted (real black paint is only ever part of one)
    if _kth(lum_c, int(m * BLACK_SHARE), breathe) < BLACK_PAINT:
        unpainted = [True] * m
    light = [0.0] * m
    for s in breathe.steps(m):
        e = min(m, s + CHUNK)
        light[s:e] = [0.0 if u else la / max(lc, 0.02) for u, la, lc in zip(unpainted[s:e], lum_a[s:e], lum_c[s:e])]
        breathe()
    lit: list[float] = []
    raw: list[float] = []
    for s in breathe.steps(m):
        e = min(m, s + CHUNK)
        lit += [v for v, u in zip(light[s:e], unpainted[s:e]) if not u]
        raw += [max(la, 0.0) for la, u in zip(lum_a[s:e], unpainted[s:e]) if u]
        breathe()
    median = (_kth(lit, len(lit) // 2, breathe) or 1.0) if lit else 1.0
    # Unpainted pixels (fur and the like): the HDR shot's hue, at a brightness squeezed into a band
    # around their own median (their lighting is harsh, with pure black shadows); where a pixel
    # is too dark to have a hue, the average hue of the rest
    raw_median = (_kth(raw, len(raw) // 2, breathe) if raw else 0.0) or 1e-4
    floor = HUE_FLOOR * raw_median
    hue_r = hue_g = hue_b = 0.0
    hued = 0
    for s in breathe.steps(m):
        for k in range(s, min(m, s + CHUNK)):
            if unpainted[k] and (v := lum_a[k]) > floor:
                hue_r += max(hr[k], 0.0) / v
                hue_g += max(hg[k], 0.0) / v
                hue_b += max(hb[k], 0.0) / v
                hued += 1
        breathe()
    avg_hue = (hue_r / hued, hue_g / hued, hue_b / hued) if hued else (1.0, 1.0, 1.0)
    del c_r, c_g, c_b, a_r, a_g, a_b
    # Ink, per boss pixel: the frame's edge, the silhouette, a depth step or a turn of the surface
    ink = [True] * m
    last = n - width
    for s in breathe.steps(m, SLOW_CHUNK):
        for k in range(s, min(m, s + SLOW_CHUNK)):
            i = idx[k]
            x = i % width
            if x == 0 or x == width - 1 or i < width or i >= last:
                continue
            dd = d[i]
            step = INK_DEPTH * max(dd, 1.0)
            nx, ny, nz = n_x[i], n_y[i], n_z[i]
            j = i - 1
            if not inside[j] or abs(d[j] - dd) > step or nx * n_x[j] + ny * n_y[j] + nz * n_z[j] < INK_NORMAL:
                continue
            j = i + 1
            if not inside[j] or abs(d[j] - dd) > step or nx * n_x[j] + ny * n_y[j] + nz * n_z[j] < INK_NORMAL:
                continue
            j = i - width
            if not inside[j] or abs(d[j] - dd) > step or nx * n_x[j] + ny * n_y[j] + nz * n_z[j] < INK_NORMAL:
                continue
            j = i + width
            if not inside[j] or abs(d[j] - dd) > step or nx * n_x[j] + ny * n_y[j] + nz * n_z[j] < INK_NORMAL:
                continue
            ink[k] = False
        breathe()
    out = bytearray(n * 4)
    level_of, steps = bisect.bisect_right, _steps()
    gain = LIGHT_GAIN
    keep = 1 - AMBIENT
    for s in breathe.steps(m, SLOW_CHUNK):
        for k in range(s, min(m, s + SLOW_CHUNK)):
            if unpainted[k]:
                v = lum_a[k]
                if v < 0.0:
                    v = 0.0
                level = MID_PAINT * min(max((v / raw_median) ** UNPAINTED_CURVE, UNPAINTED_LOW), UNPAINTED_HIGH)
                if v > floor:
                    r, g, b = max(hr[k], 0.0) / v * level, max(hg[k], 0.0) / v * level, max(hb[k], 0.0) / v * level
                else:
                    r, g, b = avg_hue[0] * level, avg_hue[1] * level, avg_hue[2] * level
            else:
                shade = AMBIENT + gain * min(light[k] / median, LIGHT_MAX) * keep
                r, g, b = pr[k] * shade, pg[k] * shade, pb[k] * shade
            if ink[k]:
                r, g, b = r * INK, g * INK, b * INK
            o = idx[k] * 4
            out[o : o + 4] = (level_of(steps, r), level_of(steps, g), level_of(steps, b), 255)
        breathe()
    # Freed a list at a time: all at once (on return) is a million floats, ~12 ms in one go
    for big in (pr, pg, pb, hr, hg, hb, lum_a, lum_c, light, lit, raw, unpainted, ink, idx, inside):
        big.clear()
        breathe()
    del d, n_x, n_y, n_z
    breathe()
    return bytes(out)


def _srgb_steps() -> list[float]:
    """
    For each output level 1..255, the smallest float _srgb gives it for: _srgb is a step function,
    so a level is how many of these a value reaches (bisect), the same byte without the power
    function. Each found by halving the gap between doubles (their bit patterns sort as they do).
    """

    def as_float(bits: int) -> float:
        return struct.unpack("<d", struct.pack("<q", bits))[0]

    def as_bits(x: float) -> int:
        return struct.unpack("<q", struct.pack("<d", x))[0]

    steps = []
    lo_bits = as_bits(0.0)
    for level in range(1, 256):
        hi_bits = as_bits(1.0)  # _srgb(1.0) is 255
        while lo_bits < hi_bits:
            mid = (lo_bits + hi_bits) // 2
            if _srgb(as_float(mid)) >= level:
                hi_bits = mid
            else:
                lo_bits = mid + 1
        steps.append(as_float(lo_bits))
    return steps


_SRGB_STEPS: list[float] = []  # made the first time compose runs (~14 ms: not at import, on the game thread)


def _steps() -> list[float]:
    if not _SRGB_STEPS:
        _SRGB_STEPS[:] = _srgb_steps()
    return _SRGB_STEPS


def _srgb_fast(x: float) -> int:
    return bisect.bisect_right(_steps(), x)


def from_look(width: int, height: int, hdr: bytes, look: bytes) -> bytes:
    """
    The picture from two captures of the same view (float RGBA per pixel each): the look shot's
    colour (the game's final image: already tone mapped and display-encoded, 0..1) and the HDR
    shot's transparency (its alpha is how see-through: 0 where the boss is drawn).
    """
    a = struct.unpack(f"<{width * height * 4}f", hdr)
    c = struct.unpack(f"<{width * height * 4}f", look)
    out = bytearray(width * height * 4)
    for p in range(width * height):
        i = p * 4
        if a[i + 3] < 1:
            out[i : i + 4] = bytes((_byte(c[i]), _byte(c[i + 1]), _byte(c[i + 2]), _byte(1 - a[i + 3])))
    return bytes(out)


def _byte(x: float) -> int:
    return max(0, min(255, round(x * 255)))


def convert(hdr: Path, png: Path) -> None:
    """Tone maps a boss capture (.hdr) into a transparent PNG, then deletes the .hdr."""
    width, height, pixels = read_hdr(hdr)
    lums = sorted(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels if r or g or b)
    if not lums:
        raise ValueError("the capture is empty")
    bright = lums[min(len(lums) - 1, int(len(lums) * (1 - BRIGHT_SHARE)))]
    k = TARGET / bright if bright > 0 else 1.0
    out = bytearray(width * height * 4)
    for i, (r, g, b) in enumerate(pixels):
        if r or g or b:
            out[i * 4 : i * 4 + 4] = bytes((_srgb(_aces(r * k)), _srgb(_aces(g * k)), _srgb(_aces(b * k)), 255))
    write_png(png, width, height, bytes(out))
    hdr.unlink(missing_ok=True)
