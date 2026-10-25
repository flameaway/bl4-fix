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

import json
import math
import struct
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


def small_path(png: Path) -> Path:
    return png.parent / SMALL_DIR / png.name


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
                time.sleep(SLICE_S)
                until = time.perf_counter() + SLICE_S
    return w, h, bytes(out)


def write_small(png: Path, width: int, height: int, rgba: bytes) -> None:
    """small/<name> for a picture just written from these pixels, and small/index.json."""
    w, h, small = shrink(width, height, rgba)
    target = small_path(png)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_png(target, w, h, small)
    write_small_index(png.parent)


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


def compose(width: int, height: int, hdr: bytes, base: bytes, normal: bytes, depth: bytes) -> bytes:
    """
    The picture, drawn from captures of the same frozen view (float RGBA per pixel each):
    - hdr: lit scene colour; its alpha is how see-through (0 where the boss is), its light per
      pixel (lit over paint brightness, levelled on the median) shades the paint between AMBIENT
      and full;
    - base: paint colour (linear), which is what keeps every boss's true colours;
    - normal, depth: where surfaces turn or step away, and the silhouette, get ink lines, as the
      game's own outlines do.
    """
    n = width * height
    a = struct.unpack(f"<{n * 4}f", hdr)
    c = struct.unpack(f"<{n * 4}f", base)
    nm = struct.unpack(f"<{n * 4}f", normal)
    dp = struct.unpack(f"<{n * 4}f", depth)
    # The boss: drawn in the HDR shot and solid (effects like smoke and particle clouds are drawn but
    # have no depth or paint, and came out as black blobs)
    inside = [a[i * 4 + 3] < 1 and dp[i * 4] < NO_DEPTH for i in range(n)]
    # Some materials give no paint colour (it reads pure white): those pixels take their colour
    # from the HDR shot instead
    unpainted = [inside[i] and min(c[i * 4], c[i * 4 + 1], c[i * 4 + 2]) >= UNPAINTED for i in range(n)]

    def lum(f: tuple[float, ...], i: int) -> float:
        return 0.2126 * f[i * 4] + 0.7152 * f[i * 4 + 1] + 0.0722 * f[i * 4 + 2]

    # Others give none as pure black, all over (Logistics Manager Reyes, Volcanus): a boss whose paint
    # is black nearly everywhere is all unpainted (real black paint is only ever part of one)
    paint = sorted(lum(c, i) for i in range(n) if inside[i])
    if paint and paint[int(len(paint) * BLACK_SHARE)] < BLACK_PAINT:
        unpainted = inside[:]

    if not any(inside):
        raise ValueError("the capture is empty")
    light = [lum(a, i) / max(lum(c, i), 0.02) if inside[i] and not unpainted[i] else 0.0 for i in range(n)]
    lit = sorted(light[i] for i in range(n) if inside[i] and not unpainted[i])
    median = (lit[len(lit) // 2] or 1.0) if lit else 1.0
    # Unpainted pixels (fur and the like): the HDR shot's hue, at a brightness squeezed into a band
    # around their own median (their lighting is harsh, with pure black shadows); where a pixel
    # is too dark to have a hue, the average hue of the rest
    raw = sorted(max(lum(a, i), 0.0) for i in range(n) if unpainted[i])
    raw_median = (raw[len(raw) // 2] if raw else 0.0) or 1e-4
    hue_sum = [0.0, 0.0, 0.0]
    hued = 0
    for i in range(n):
        if unpainted[i] and (v := lum(a, i)) > HUE_FLOOR * raw_median:
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
                hue = [max(a[i * 4 + k], 0.0) / v for k in range(3)] if v > HUE_FLOOR * raw_median else avg_hue
                level = MID_PAINT * min(max((v / raw_median) ** UNPAINTED_CURVE, UNPAINTED_LOW), UNPAINTED_HIGH)
                r, g, b = hue[0] * level, hue[1] * level, hue[2] * level
            else:
                shade = AMBIENT + LIGHT_GAIN * min(light[i] / median, LIGHT_MAX) * (1 - AMBIENT)
                r, g, b = c[i * 4] * shade, c[i * 4 + 1] * shade, c[i * 4 + 2] * shade
            if _inked(i, x, y, width, height, inside, nm, dp):
                r, g, b = r * INK, g * INK, b * INK
            out[i * 4 : i * 4 + 4] = bytes((_srgb(r), _srgb(g), _srgb(b), 255))
    return bytes(out)


def _inked(i: int, x: int, y: int, width: int, height: int, inside: list[bool], nm: tuple[float, ...], dp: tuple[float, ...]) -> bool:
    if x == 0 or y == 0 or x == width - 1 or y == height - 1:
        return True
    d = dp[i * 4]
    for j in (i - 1, i + 1, i - width, i + width):
        if not inside[j]:
            return True  # the silhouette
        if abs(dp[j * 4] - d) > INK_DEPTH * max(d, 1.0):
            return True
        if nm[i * 4] * nm[j * 4] + nm[i * 4 + 1] * nm[j * 4 + 1] + nm[i * 4 + 2] * nm[j * 4 + 2] < INK_NORMAL:
            return True
    return False


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
