"""
The map filmed once: tiles kept on disk per character and map.

The map art never changes, so instead of filming the minimap's picture every time you walk out of
it (5 captures of ~3.9 ms GPU each, every ~room of travel), the map is filmed once in TILE_WORLD
tiles at about the art's own detail (T_Cowbell_P is 4096^2 over ~4.1 km: ~1 m per texel; maps with
finer art, like the vaults, in DENSE_TILE_WORLD tiles), saved as PNGs, and the minimap's picture is
put together from tiles with a few Canvas draws.

- Tiles sit on a grid in the north-up frame (the same frame as the pictures). Tile (i, j) covers
  `up` in [i, i+1) * world and `right` in [j, j+1) * world (`Tiles.world`, per level).
- A tile is filmed with the fog plane in it (the ECHO map's look; Canvas can't draw the fog
  material with its transparency), and re-filmed while its texture detail streams in
  (SETTLE_AT_S, measured: 6-8 captures over 2-3 s).
- Saved only while the game is paused (the export reads the target back from the GPU: a hitch),
  per character, map, fog setting and north; wiped when the game's exe changes (a patch may change
  the art).
- Loaded ahead of the player: `ImportFileAsTexture2D` of a 512^2 PNG is ~5 ms on the game thread,
  so at most one load or capture every ACTION_GAP_S.
- Fog of war: each tile remembers a digest of the fog mask under it (from the fog PNG fog.py saves
  when the ECHO map is opened; the mask maps straight onto world X/Y, measured). When that PNG
  changes, only tiles whose part of the mask changed are filmed again.

Colour round trips (measured, byte-exact): a capture writes into a target with TargetGamma 1/2.2;
its PNG imported (an sRGB texture) and drawn into a target with TargetGamma 2.2 gives the same bytes
back; target-to-target draws are exact into a target with TargetGamma 1 (the composed picture).

Every target is held by an idle SceneCapture2D's TextureTarget (Unreal's GC doesn't see Python
references); Python keeps WeakPointers only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
import zlib
from itertools import accumulate
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR
from unrealsdk.unreal import IGNORE_STRUCT, UObject, WeakPointer

from . import scene

FOLDER: Path = SETTINGS_DIR / "minimap_tiles"
FOG_FOLDER: Path = SETTINGS_DIR / "minimap_fog"
FORMAT = 2  # bump to drop every saved tile (2: tiles carry a MARGIN_PX border)
TILE_PX = 512
TILE_WORLD = 51200.0  # world units a tile stands for: 512 m (1 m a pixel), for art ~1 m a texel
# Maps whose art is finer (Vault_Mountains_P: 4096^2 over 1.4 km, 0.34 m a texel) get tiles half as
# wide (0.5 m a pixel): at 1 m a pixel the minimap showed them blurry (2026-10-30). Same pixels per
# tile, so loads cost the same; pictures over DENSE_TILE_WORLD (range > ~100 m) are filmed directly.
DENSE_TILE_WORLD = 25600.0
DENSE_TEXEL = 60.0  # world units a texel: art at least this fine gets DENSE_TILE_WORLD tiles
# Each tile is filmed MARGIN_PX wider on every side and only its inside is drawn: drawn edge to edge,
# a tile's outer pixels were filtered against nothing and left a faint seam (measured: 5 vs 1.4 mean
# difference on the boundary rows)
MARGIN_PX = 2
FILM_SLOTS = 12  # tiles filmed this session (held until saved and no longer needed; up to 9 wanted at once)
LOAD_SLOTS = 12  # tiles loaded from disk
SETTLE_AT_S = (0.15, 0.35, 0.6, 0.9, 1.3, 1.8, 2.5)  # captures after the first, while detail streams in
ACTION_GAP_S = 0.05  # at most one tile load or capture per this
SAVE_GAP_S = 0.25  # while paused: one tile export per this
FOG_CHECK_S = 2.0  # how often to look whether fog.py saved a new mask
CHAR_CHECK_S = 1.0  # how often the character's id is read again while a session is open
# Blurry tiles: the map art sometimes doesn't stream in at all while a tile settles (World_P,
# Cowbell_P and Vault_Mountains_P tiles were saved at the lowest mip after all 8 captures, and
# Vault_Mountains_P's after 4 films over 3 minutes of one visit; filmed again later, sharp within
# ~1.2 s). Each saved or loaded tile is scored on a worker (`_sharpness`: how much of its contrast
# is fine detail, independent of how much of the tile is map); under SHARP_LOW it's filmed again
# RETRY_GAPS_S after the score (later each time, so a stretch where the art won't stream passes),
# and on every later visit while it still scores low. The score is saved in the index ("sharp").
SHARP_LOW = 0.16  # measured: lowest-mip tiles 0.13-0.14, the softest real art 0.18, most 0.2-0.45
SHARP_LIT = 10  # green value over which a pixel is map art (the rest is the black around the map)
SHARP_STEP = 8  # px: the coarse gradient's distance (a lowest-mip picture is smooth at this scale)
SHARP_ROW_EVERY = 4  # rows sampled for the gradients (every row is unfiltered)
SHARP_MIN_PAIRS = 400  # fewer lit pixel pairs than this: no map art to judge (always fine)
RETRY_GAPS_S = (15.0, 60.0, 180.0)  # refilms of a blurry tile per visit, this long after each score
GAMMA_CAPTURE = scene.TARGET_GAMMA
GAMMA_LOADED = 2.2
BLEND_OPAQUE = 0


def _v2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=x, Y=y)


def _png_channel(path: Path) -> tuple[int, int, bytes]:
    """The first channel of an 8-bit PNG (gray, RGB or RGBA): (width, height, w*h bytes).
    Pure Python, for the 128x128 fog mask; run it on a worker (it yields between rows)."""
    data = path.read_bytes()
    pos, idat = 8, bytearray()
    width = height = channels = 0
    while pos < len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack_from(">IIBB", body)
            if depth != 8:
                raise ValueError(f"{depth}-bit PNG")
            channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    prev = bytearray(stride)
    out = bytearray(width * height)
    for row in range(height):
        start = row * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b, c = prev[i], prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out[row * width:(row + 1) * width] = line[0::channels]
        prev = line
        if row % 8 == 7:
            time.sleep(0.001)  # let the game thread have the GIL (sleep(0) starves it)
    return width, height, bytes(out)


def _sharpness(path: Path) -> float | None:
    """
    How sharp a saved tile is: over its map art (green > SHARP_LIT), the sum of 1-px gradients over
    the sum of SHARP_STEP-px ones, along rows. A picture filmed before the art streamed in (its
    lowest mip, ~10-20 m a texel) is smooth at that scale: 1-px steps are ~1/8 of the 8-px ones
    (0.13-0.14 measured); streamed art has detail at every scale (0.18-0.45). It doesn't depend on
    how much of the tile is map (the old residual score did: a vault's sharp tiles, mostly black,
    scored as low as its blurry ones, so all were kept). -1.0: too little map art to judge.

    Unfilters the green channel only (PNG filters work per byte, a pixel apart: each channel on
    its own). ~40 ms of pure Python per 512^2 tile (80 worst, offline), resting 1 ms every 4 rows
    so the game thread gets the GIL back. None if the file can't be read as an 8-bit RGB(A) PNG.
    """
    data = path.read_bytes()
    pos, idat = 8, bytearray()
    width = height = channels = 0
    while pos + 8 <= len(data):  # Unreal pads its PNGs past IEND
        length, kind = struct.unpack_from(">I4s", data, pos)
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack_from(">IIBB", data, pos + 8)
            if depth != 8 or color not in (2, 6):
                return None
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not channels or width <= SHARP_STEP:
        return None
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    prev = [0] * width
    fine = coarse = pairs = 0
    lit, step = SHARP_LIT, SHARP_STEP
    span = range(width - step)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        res = raw[start + 2:start + 1 + stride:channels]  # green's filtered bytes
        if kind == 0:
            cur = list(res)
        elif kind == 1:  # Sub
            cur = list(accumulate(res, lambda a, b: (a + b) & 0xFF))
        elif kind == 2:  # Up
            cur = [(r + u) & 0xFF for r, u in zip(res, prev)]
        elif kind == 3:  # Average
            cur, left = [], 0
            for r, u in zip(res, prev):
                left = (r + ((left + u) >> 1)) & 0xFF
                cur.append(left)
        else:  # Paeth
            cur, left, ul = [], 0, 0
            for r, u in zip(res, prev):
                p = left + u - ul
                pa, pb, pc = abs(p - left), abs(p - u), abs(p - ul)
                left = (r + (left if pa <= pb and pa <= pc else u if pb <= pc else ul)) & 0xFF
                cur.append(left)
                ul = u
        prev = cur
        if row % SHARP_ROW_EVERY == 0:
            for x in span:
                a, b = cur[x], cur[x + step]
                if a > lit or b > lit:
                    fine += abs(cur[x + 1] - a)
                    coarse += abs(b - a)
                    pairs += 1
        if row % 4 == 3:
            time.sleep(0.001)  # let the game thread have the GIL (sleep(0) starves it)
    if pairs < SHARP_MIN_PAIRS or coarse == 0:
        return -1.0
    return fine / coarse


class _Slot:
    """A render target held by an idle capture actor; holds one tile at a time."""

    def __init__(self, capture: UObject, target: UObject, kind: str):
        self.capture = WeakPointer(capture)
        self.target = WeakPointer(target)
        self.kind = kind  # "film" or "load"
        self.key: tuple[int, int] | None = None
        self.used = 0.0

    def alive(self) -> bool:
        return self.capture() is not None and self.target() is not None


class _Tile:
    def __init__(self, key: tuple[int, int], slot: _Slot, digest: str):
        self.key = key
        self.slot = slot
        self.digest = digest  # the fog mask under it when it was filmed
        self.settle: list[float] = []  # when to film it again
        self.saved = False


class Tiles:
    session: tuple | None = None  # (character, world, fog key, north, tile world)
    world = TILE_WORLD  # world units a tile of this session stands for
    world_for: tuple[str, float] = ("", TILE_WORLD)  # (level, its tile world): `_tile_world`, once per level
    folder: Path | None = None
    index: dict[str, Any] = {}
    slots: list[_Slot] = []
    tiles: dict[tuple[int, int], _Tile] = {}
    version = 0  # bumps whenever a tile's pixels change (composed pictures are redrawn)
    north = 0.0
    next_action = 0.0
    # The last tick found every wanted tile there and none still sharpening (the caller may then ask
    # less often: nothing changes until you move a long way or the fog changes)
    idle = False
    next_save = 0.0
    next_fog_check = 0.0
    # Fog mask (from fog.py's PNG): bytes of its first channel, its size and placement
    mask: tuple[int, int, bytes] | None = None
    mask_stamp: float | None = None
    placement: dict[str, Any] | None = None
    worker: threading.Thread | None = None
    worker_result: tuple[float, tuple[int, int, bytes] | None, dict[str, Any] | None] | None = None
    # Sharpness checks (see SHARP_LOW): (folder, tile name) waiting / scored, on their own worker
    checker: threading.Thread | None = None
    check_queue: list[tuple[Path, str]] = []
    check_results: list[tuple[Path, str, float | None]] = []
    checking: set[str] = set()
    # Blurry tiles: name -> when to film it again (monotonic); refilms of each this visit
    low: dict[str, float] = {}
    tries: dict[str, int] = {}
    logged = ""
    char: str | None = None  # scene_character's last answer (CHAR_CHECK_S)
    char_at = 0.0
    char_world = ""  # the level `char` was read in

    # region Session

    @classmethod
    def _exe_stamp(cls) -> float:
        try:
            return Path(sys.executable).stat().st_mtime
        except OSError:
            return 0.0

    @classmethod
    def _open(cls, session: tuple) -> None:
        """Switches to this character/world/fog/north: drops loaded tiles, reads its index."""
        cls.drop()
        cls.session = session
        char, world, fog_key, north, tile_world = session
        cls.north, cls.world = north, tile_world
        size = "" if tile_world == TILE_WORLD else f"_t{round(tile_world / 100)}"
        cls.folder = FOLDER / f"{char}_{world}_{fog_key}_n{round(north)}{size}"
        cls.index = {"format": FORMAT, "exe": cls._exe_stamp(), "tiles": {}}
        path = cls.folder / "index.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("format") == FORMAT and saved.get("exe") == cls.index["exe"]:
                cls.index = saved
            else:
                scene.log(f"tiles: {cls.folder.name} is from another game version, filming again")
                for png in cls.folder.glob("*.png"):
                    png.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        cls.mask = cls.placement = None
        cls.mask_stamp = None
        cls.next_fog_check = 0.0
        cls.check_queue, cls.checking, cls.low, cls.tries = [], set(), {}, {}
        cls.index.pop("detail", None)  # the old score (unfit for mostly-black tiles): judged again on load
        # Tiles still blurry from an earlier visit: filmed again once wanted (the art may stream now)
        now = time.monotonic()
        for name, score in cls.index.get("sharp", {}).items():
            if 0 <= score < SHARP_LOW and name in cls.index["tiles"]:
                cls.low[name] = now + RETRY_GAPS_S[0]
        blurry = f", {len(cls.low)} blurry" if cls.low else ""
        scene.log(f"tiles: {cls.folder.name}, {len(cls.index['tiles'])} on disk{blurry}")

    @classmethod
    def _write_index(cls) -> None:
        if cls.folder is None:
            return
        cls.folder.mkdir(parents=True, exist_ok=True)
        path = cls.folder / "index.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cls.index), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def drop(cls) -> None:
        """Forgets every loaded tile and destroys the slots' actors (map change, mod off)."""
        for slot in cls.slots:
            actor = slot.capture()
            if actor is not None:
                try:
                    actor.K2_DestroyActor()
                except Exception as ex:  # noqa: BLE001
                    scene.log(f"tiles: destroy failed: {ex!r}")
        cls.slots = []
        cls.tiles = {}
        cls.session = None
        cls.version += 1

    # endregion
    # region Fog digests

    @classmethod
    def _check_fog(cls, char: str, world: str, fog_on: bool) -> None:
        """Starts reading fog.py's saved mask when it changed; applies a finished read."""
        if cls.worker_result is not None:
            stamp, mask, placement = cls.worker_result
            cls.worker_result = None
            cls.mask, cls.placement, cls.mask_stamp = mask, placement, stamp
            cls._refresh_digests()
        if not fog_on or cls.worker is not None:
            return
        now = time.monotonic()
        if now < cls.next_fog_check:
            return
        cls.next_fog_check = now + FOG_CHECK_S
        png = FOG_FOLDER / f"{char}_{world}.png"
        try:
            stamp = png.stat().st_mtime
        except OSError:
            return
        if stamp == cls.mask_stamp:
            return

        def read() -> None:
            try:
                mask = _png_channel(png)
                placement = json.loads(png.with_suffix(".json").read_text(encoding="utf-8"))
                cls.worker_result = (stamp, mask, placement)
            except Exception as ex:  # noqa: BLE001
                scene.log(f"tiles: couldn't read the fog mask: {ex!r}")
                cls.worker_result = (stamp, None, None)
            finally:
                cls.worker = None

        cls.worker = threading.Thread(target=read, name="bl4_minimap tiles fog", daemon=True)
        cls.worker.start()

    @classmethod
    def digest(cls, key: tuple[int, int]) -> str:
        """The fog mask under tile `key` (plus a texel around it): what it was filmed with."""
        if cls.mask is None or cls.placement is None:
            return "-"
        width, height, data = cls.mask
        c, s = math.cos(math.radians(cls.north)), math.sin(math.radians(cls.north))
        xs, ys = [], []
        for du in (0, 1):
            for dr in (0, 1):
                up, right = (key[0] + du) * cls.world, (key[1] + dr) * cls.world
                xs.append(up * c - right * s)
                ys.append(up * s + right * c)
        span = 100 * cls.placement["scale"][0]
        loc = cls.placement["loc"]

        def texel(world: float, centre: float, size: int) -> float:
            return ((world * scene.MAP_SCALE - centre) / span + 0.5) * size

        c0 = max(0, int(texel(min(xs), loc[0], width)) - 1)
        c1 = min(width, int(texel(max(xs), loc[0], width)) + 2)
        r0 = max(0, int(texel(min(ys), loc[1], height)) - 1)
        r1 = min(height, int(texel(max(ys), loc[1], height)) + 2)
        if c0 >= c1 or r0 >= r1:
            return "out"
        h = hashlib.blake2s(digest_size=8)
        for row in range(r0, r1):
            h.update(data[row * width + c0:row * width + c1])
        return h.hexdigest()

    @classmethod
    def _refresh_digests(cls) -> None:
        """New fog mask: tiles whose part of it changed are dropped (filmed again when needed)."""
        stale = 0
        for name, old in list(cls.index["tiles"].items()):
            i, j = (int(n) for n in name.split("_"))
            if cls.digest((i, j)) != old:
                del cls.index["tiles"][name]
                cls.index.get("sharp", {}).pop(name, None)
                cls.low.pop(name, None)
                if cls.folder is not None:
                    (cls.folder / f"{name}.png").unlink(missing_ok=True)
                stale += 1
        for key, tile in list(cls.tiles.items()):
            if tile.digest != cls.digest(key):
                tile.slot.key = None
                del cls.tiles[key]
                stale += 1
        if stale:
            cls._write_index()
            cls.version += 1
            scene.log(f"tiles: fog changed under {stale} tile(s)")

    # endregion
    # region Sharpness checks

    @classmethod
    def _queue_check(cls, name: str) -> None:
        if cls.folder is None or name in cls.checking:
            return
        cls.checking.add(name)
        cls.check_queue.append((cls.folder, name))
        if cls.checker is not None:
            return

        def run() -> None:
            try:
                while cls.check_queue:
                    folder, tile = cls.check_queue.pop(0)
                    try:
                        score = _sharpness(folder / f"{tile}.png")
                    except Exception as ex:  # noqa: BLE001
                        scene.log(f"tiles: couldn't score {tile}: {ex!r}")
                        score = None
                    cls.check_results.append((folder, tile, score))
                    time.sleep(0.005)
            finally:
                cls.checker = None

        cls.checker = threading.Thread(target=run, name="bl4_minimap tiles sharpness", daemon=True)
        cls.checker.start()

    @classmethod
    def _apply_checks(cls) -> None:
        """Records scores; a blurry tile stays shown and is filmed again later (`tick`, RETRY_GAPS_S)."""
        changed = False
        while cls.check_results:
            folder, name, score = cls.check_results.pop(0)
            cls.checking.discard(name)
            if folder != cls.folder or name not in cls.index["tiles"] or score is None:
                continue
            cls.index.setdefault("sharp", {})[name] = round(score, 3)
            tries = cls.tries.get(name, 0)
            if 0 <= score < SHARP_LOW:
                if tries < len(RETRY_GAPS_S):
                    cls.low[name] = time.monotonic() + RETRY_GAPS_S[tries]
                    scene.log(f"tiles: {name} looks blurry (sharpness {score:.3f}), filming it again in "
                              f"{RETRY_GAPS_S[tries]:.0f} s")  # fmt: skip
                else:
                    cls.low.pop(name, None)
                    scene.log(f"tiles: {name} still blurry ({score:.3f}) after {tries} films, next visit")
            else:
                cls.low.pop(name, None)
                if tries:
                    scene.log(f"tiles: {name} filmed again, sharpness {score:.3f}")
            changed = True
        if changed:
            cls._write_index()

    # endregion
    # region Slots

    @classmethod
    def _slot(cls, pc: UObject, kind: str, wanted: set[tuple[int, int]]) -> _Slot | None:
        """A free slot of this kind: a new one, an empty one, or the least recently used tile that
        isn't wanted now (a film slot only once its tile is saved, or if it's no longer wanted)."""
        cls.slots = [s for s in cls.slots if s.alive()]
        mine = [s for s in cls.slots if s.kind == kind]
        empty = next((s for s in mine if s.key is None), None)
        if empty is not None:
            return empty
        if len(mine) < (FILM_SLOTS if kind == "film" else LOAD_SLOTS):
            size = TILE_PX
            capture, target = scene.Scene.make_capture(pc, size, GAMMA_CAPTURE if kind == "film" else GAMMA_LOADED)
            slot = _Slot(capture, target, kind)
            cls.slots.append(slot)
            return slot
        candidates = [
            s for s in mine
            if s.key not in wanted and (kind == "load" or cls.tiles[s.key].saved or not cls.tiles[s.key].settle)
        ]  # fmt: skip
        if not candidates:
            return None
        slot = min(candidates, key=lambda s: s.used)
        cls.tiles.pop(slot.key, None)
        slot.key = None
        return slot

    # endregion
    # region Filming and loading

    @classmethod
    def _film(cls, tile: _Tile) -> None:
        capture = tile.slot.capture()
        if capture is None:
            return
        c, s = math.cos(math.radians(cls.north)), math.sin(math.radians(cls.north))
        up, right = (tile.key[0] + 0.5) * cls.world, (tile.key[1] + 0.5) * cls.world
        x, y = up * c - right * s, up * s + right * c
        where = unrealsdk.make_struct(
            "Vector",
            X=scene.ORIGIN[0] + x * scene.MAP_SCALE,
            Y=scene.ORIGIN[1] + y * scene.MAP_SCALE,
            Z=scene.ORIGIN[2] + scene.CAMERA_HEIGHT,
        )
        rot = unrealsdk.make_struct("Rotator", Pitch=-90.0, Yaw=cls.north, Roll=0.0)
        capture.K2_SetActorLocationAndRotation(where, rot, False, unrealsdk.make_struct("HitResult"), True)
        comp = capture.CaptureComponent2D
        comp.ShowOnlyActors = scene.Scene.shown_actors()  # the fog plane may have come or gone
        comp.OrthoWidth = cls.world * TILE_PX / (TILE_PX - 2 * MARGIN_PX) * scene.MAP_SCALE
        comp.CaptureScene()
        tile.slot.used = time.monotonic()
        cls.version += 1

    @classmethod
    def _load(cls, pc: UObject, key: tuple[int, int], slot: _Slot) -> bool:
        if cls.folder is None:
            return False
        png = cls.folder / f"{key[0]}_{key[1]}.png"
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        texture = krl.ImportFileAsTexture2D(pc, str(png)) if png.exists() else None
        target = slot.target()
        if texture is None or target is None:
            cls.index["tiles"].pop(f"{key[0]}_{key[1]}", None)
            return False
        # Drawn into the slot in this same call: nothing holds the texture once we return
        _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, target, None, IGNORE_STRUCT, IGNORE_STRUCT)
        white = unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1)
        canvas.K2_DrawTexture(texture, _v2(0, 0), _v2(TILE_PX, TILE_PX), _v2(0, 0), _v2(1, 1), white, BLEND_OPAQUE,
                              0.0, _v2(0.5, 0.5))  # fmt: skip
        krl.EndDrawCanvasToRenderTarget(pc, ctx)
        tile = _Tile(key, slot, cls.index["tiles"][f"{key[0]}_{key[1]}"])
        tile.saved = True
        slot.key, slot.used = key, time.monotonic()
        cls.tiles[key] = tile
        cls.version += 1
        if f"{key[0]}_{key[1]}" not in cls.index.get("sharp", {}):
            cls._queue_check(f"{key[0]}_{key[1]}")  # not judged yet (saved before this score existed)
        return True

    @classmethod
    def _save_one(cls, pc: UObject) -> None:
        """While paused: exports one finished, unsaved tile."""
        now = time.monotonic()
        if now < cls.next_save or cls.folder is None:
            return
        cls.next_save = now + SAVE_GAP_S  # (also when there's nothing to save: the scan at that pace)
        tile = next((t for t in cls.tiles.values() if not t.saved and not t.settle), None)
        if tile is None:
            return
        target = tile.slot.target()
        if target is None:
            return
        cls.folder.mkdir(parents=True, exist_ok=True)
        name = f"{tile.key[0]}_{tile.key[1]}"
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        krl.ExportRenderTarget(pc, target, str(cls.folder), f"{name}.png")
        tile.saved = True
        cls.index["tiles"][name] = tile.digest
        cls.index.get("sharp", {}).pop(name, None)
        cls._write_index()
        cls._queue_check(name)

    # endregion
    # region Per frame

    @classmethod
    def _cover(cls, cx: float, cy: float, half: float, north: float) -> list[tuple[int, int]]:
        """The tiles a square of `half` around world (cx, cy) touches, nearest first."""
        side = cls.world
        c, s = math.cos(math.radians(north)), math.sin(math.radians(north))
        up, right = cx * c + cy * s, -cx * s + cy * c
        i0, i1 = math.floor((up - half) / side), math.floor((up + half) / side)
        j0, j1 = math.floor((right - half) / side), math.floor((right + half) / side)
        keys = [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
        keys.sort(key=lambda k: ((k[0] + 0.5) * side - up) ** 2 + ((k[1] + 0.5) * side - right) ** 2)
        return keys

    @classmethod
    def _tile_world(cls, level: str) -> float:
        """How wide this level's tiles are: DENSE_TILE_WORLD if its map art is one textured plane at
        least DENSE_TEXEL fine, else TILE_WORLD (World_P's split meshes, Cowbell_P's ~1 m texels).
        Worked out once per level (a few property reads)."""
        if cls.world_for[0] == level:
            return cls.world_for[1]
        actor = scene.Scene.actor()
        if actor is None:
            return TILE_WORLD  # (not remembered: asked again once the map model is there)
        result = TILE_WORLD
        texel = None
        try:
            texel = _art_texel(actor)
        except Exception as ex:  # noqa: BLE001
            scene.log(f"tiles: couldn't read the map art's size: {ex!r}")
        if texel is not None and texel <= DENSE_TEXEL:
            result = DENSE_TILE_WORLD
        if texel is not None:
            scene.log(f"tiles: {level} art {texel / 100:.2f} m a texel, tiles {result / 100:.0f} m")
        cls.world_for = (level, result)
        return result

    @classmethod
    def usable(cls, picture: float) -> bool:
        """Whether pictures this size can be put together from tiles (at most 3x3 of them)."""
        return cls.session is not None and picture <= cls.world

    @classmethod
    def paused_tick(cls, pc: UObject) -> None:
        """While the game is paused (a menu is open): saves finished tiles, one at a time."""
        if cls.session is not None:
            cls._apply_checks()
            cls._save_one(pc)

    @classmethod
    def tick(
        cls, pc: UObject, x: float, y: float, picture: float, room: float, north: float, *,
        fog_on: bool, strength: float, paused: bool,
    ) -> None:  # fmt: skip
        """Every frame: keeps the tiles around the player loaded or filmed, one step at a time."""
        world = scene.Scene.world_name
        # The character's id (PlayerState + a GUID struct + formatting): read again at once in another
        # level (another character always means a trip through the title screen), else once a second
        now = time.monotonic()
        cls.idle = False
        if world != cls.char_world or now >= cls.char_at:
            cls.char, cls.char_at, cls.char_world = scene_character(pc), now + CHAR_CHECK_S, world
        char = cls.char
        tile_world = cls._tile_world(world)
        if not world or char is None or not scene.Scene.in_viewer or picture > tile_world:
            if cls.session is not None:
                cls.drop()
            return
        fog_key = f"fog{round(strength * 100)}" if fog_on else "nofog"
        session = (char, world.lower(), fog_key, round(north, 1), tile_world)
        if session != cls.session:
            cls._open(session)
        cls._check_fog(char, world.lower(), fog_on)
        if cls.check_results:
            cls._apply_checks()
        if paused:
            cls._save_one(pc)
            return
        if now < cls.next_action:
            return
        # Film again what's still sharpening
        for tile in cls.tiles.values():
            if tile.settle and now >= tile.settle[0]:
                tile.settle.pop(0)
                cls._film(tile)
                cls.next_action = now + ACTION_GAP_S
                return
        if cls.worker is not None:
            return  # the fog mask is being read: digests aren't known yet
        if fog_on and scene.Scene.extra() is None and cls.mask_stamp is not None:
            return  # there's a saved fog mask but fog.py hasn't put its plane up yet: tiles would lack it
        wanted = cls._cover(x, y, picture + 1.5 * room, north)
        wanted_set = set(wanted)
        # Every wanted tile there (the usual case): look again after ACTION_GAP_S, not every frame
        # (the cover's sort and the walk cost ~10-20 us; tiles reach 1.5 rooms past the picture)
        cls.next_action = now + ACTION_GAP_S
        for key in wanted:
            tile = cls.tiles.get(key)
            if tile is not None:
                tile.slot.used = now
                continue
            name = f"{key[0]}_{key[1]}"
            cls.next_action = now + ACTION_GAP_S
            if name in cls.index["tiles"] and cls.index["tiles"][name] == cls.digest(key):
                slot = cls._slot(pc, "load", wanted_set)
                if slot is not None and cls._load(pc, key, slot):
                    return
            slot = cls._slot(pc, "film", wanted_set)
            if slot is None:
                return  # every film slot holds an unsaved tile that's still needed
            tile = _Tile(key, slot, cls.digest(key))
            slot.key = key
            cls.tiles[key] = tile
            cls._film(tile)
            tile.settle = [now + d for d in SETTLE_AT_S]
            return
        # Every wanted tile is there: film a blurry one again once it's due (shown as it is meanwhile)
        if cls.low and cls._refilm_due(pc, wanted, wanted_set, now):
            return
        cls.idle = not any(tile.settle for tile in cls.tiles.values())

    @classmethod
    def _refilm_due(cls, pc: UObject, wanted: list[tuple[int, int]], wanted_set: set[tuple[int, int]],
                    now: float) -> bool:  # fmt: skip
        """Films the nearest wanted tile that scored blurry and is due again (see RETRY_GAPS_S).
        Saved (behind the next pause) and scored like a new tile."""
        for key in wanted:
            name = f"{key[0]}_{key[1]}"
            due = cls.low.get(name)
            tile = cls.tiles.get(key)
            if due is None or now < due or tile is None or tile.settle or not tile.saved:
                continue
            if tile.slot.kind == "film":
                slot = tile.slot  # filmed this visit: film it again in place
            else:
                slot = cls._slot(pc, "film", wanted_set)
                if slot is None:
                    return False
                tile.slot.key = None  # the loaded copy's slot is free again
            del cls.low[name]
            cls.tries[name] = cls.tries.get(name, 0) + 1
            fresh = _Tile(key, slot, cls.digest(key))
            slot.key = key
            cls.tiles[key] = fresh
            cls._film(fresh)
            fresh.settle = [now + d for d in SETTLE_AT_S]
            cls.next_action = now + ACTION_GAP_S
            return True
        return False

    @classmethod
    def compose(cls, pc: UObject, target: UObject, x: float, y: float, half: float, north: float) -> bool:
        """Draws the picture around world (x, y) (`half` to the edge, north up) from tiles into
        `target`. False (nothing drawn) if a tile it needs isn't there yet."""
        keys = cls._cover(x, y, half, north)
        if any(k not in cls.tiles or cls.tiles[k].slot.target() is None for k in keys):
            return False
        c, s = math.cos(math.radians(north)), math.sin(math.radians(north))
        up, right = x * c + y * s, -x * s + y * c
        size = float(target.SizeX)
        scale = size / (2 * half)
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        white = unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1)
        _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, target, None, IGNORE_STRUCT, IGNORE_STRUCT)
        side = cls.world * scale
        inset = MARGIN_PX / TILE_PX
        for i, j in keys:
            # The picture's top is +up and its left -right (north up, like the capture)
            left = (j * cls.world - (right - half)) * scale
            top = ((up + half) - (i + 1) * cls.world) * scale
            canvas.K2_DrawTexture(cls.tiles[(i, j)].slot.target(), _v2(left, top), _v2(side, side), _v2(inset, inset),
                                  _v2(1 - 2 * inset, 1 - 2 * inset), white, BLEND_OPAQUE, 0.0, _v2(0.5, 0.5))  # fmt: skip
        krl.EndDrawCanvasToRenderTarget(pc, ctx)
        return True

    # endregion


def _art_texel(actor: UObject | None) -> float | None:
    """World units per texel of a map actor whose art is one textured plane (the small maps); None
    for anything else (World_P's split meshes) or before the actor is there."""
    if actor is None:
        return None
    planes = [
        c for c in actor.BlueprintCreatedComponents
        if c is not None and c.Class.Name == "StaticMeshComponent" and c.StaticMesh is not None
        and c.StaticMesh._path_name() == scene.PLANE_MESH
    ]  # fmt: skip
    if len(planes) != 1:
        return None
    plane = planes[0]
    material = plane.GetMaterial(0)
    textures = [
        t.ParameterValue for t in getattr(material, "TextureParameterValues", ())
        if t.ParameterValue is not None and t.ParameterValue.Class.Name == "Texture2D"
    ]  # fmt: skip
    if len(textures) != 1:
        return None
    size = textures[0].Blueprint_GetSizeX()
    scale = plane.K2_GetComponentScale().X
    if size <= 0 or scale <= 0:
        return None
    # The Plane is 100 map units at scale 1; map units are world / 100 (scene.MAP_SCALE)
    return 100 * scale / scene.MAP_SCALE / size


def scene_character(pc: UObject) -> str | None:
    """The character's id, as fog.py keys its masks (imported late: fog imports scene)."""
    from . import fog  # noqa: PLC0415

    return fog._character(pc)  # noqa: SLF001
