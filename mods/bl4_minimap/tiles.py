"""
The map filmed once: tiles kept on disk per character and map.

The map art never changes, so instead of filming the minimap's picture every time you walk out of
it (5 captures of ~3.9 ms GPU each, every ~room of travel), the map is filmed once in TILE_WORLD
tiles at about the art's own detail (T_Cowbell_P is 4096^2 over ~4.1 km: ~1 m per texel), saved
as PNGs, and the minimap's picture is put together from tiles with a few Canvas draws.

- Tiles sit on a grid in the north-up frame (the same frame as the pictures). Tile (i, j) covers
  `up` in [i, i+1) * TILE_WORLD and `right` in [j, j+1) * TILE_WORLD.
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
TILE_WORLD = 51200.0  # world units a tile stands for: 512 m
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
# Blurry tiles: the map art sometimes doesn't stream in at all while a tile settles (World_P and
# Cowbell_P tiles were saved at the lowest mip, all 8 captures; filmed again later, sharp within
# 0.4 s). Each saved or loaded tile is scored on a worker (`_detail`); under DETAIL_LOW it's filmed
# again, and kept once a refilm isn't DETAIL_GAIN sharper (the art there is that plain) or after
# DETAIL_TRIES refilms. The verdict is saved in the index ("detail"), so a tile is judged once.
DETAIL_LOW = 0.9
DETAIL_FLAT = 0.02  # under this a tile is one flat colour: nothing to stream
DETAIL_GAIN = 1.25
DETAIL_TRIES = 2
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


_RESIDUAL = bytes(min(v, 256 - v) for v in range(256))  # a PNG filter residual byte -> its size


def _detail(path: Path) -> float | None:
    """
    How much fine detail a saved tile has: the mean size of its PNG filter residuals (how far each
    byte is from what its neighbours predict), RGB of every 8th row. Sharp map art scores 1-6, a tile
    filmed before the art streamed in ~0.1, one flat colour (off the map) 0, but so do mostly-black
    edge tiles and plain ground (0.1-0.9): see `Tiles._apply_checks`. No unfiltering needed, so it's
    ~5 ms of a worker per tile; zlib and the file read let go of the GIL, and it rests between rows.
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
    if not channels:
        return None
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    total = count = 0
    for n, row in enumerate(range(0, height, 8)):
        start = row * (stride + 1) + 1
        line = raw[start:start + stride]
        rgb = (line[0::channels] + line[1::channels] + line[2::channels]).translate(_RESIDUAL)
        total += sum(rgb)
        count += len(rgb)
        if n % 8 == 7:
            time.sleep(0.001)
    return total / count if count else None


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
    session: tuple | None = None  # (character, world, fog key, north)
    folder: Path | None = None
    index: dict[str, Any] = {}
    slots: list[_Slot] = []
    tiles: dict[tuple[int, int], _Tile] = {}
    version = 0  # bumps whenever a tile's pixels change (composed pictures are redrawn)
    north = 0.0
    next_action = 0.0
    next_save = 0.0
    next_fog_check = 0.0
    # Fog mask (from fog.py's PNG): bytes of its first channel, its size and placement
    mask: tuple[int, int, bytes] | None = None
    mask_stamp: float | None = None
    placement: dict[str, Any] | None = None
    worker: threading.Thread | None = None
    worker_result: tuple[float, tuple[int, int, bytes] | None, dict[str, Any] | None] | None = None
    # Detail checks (see DETAIL_LOW): (folder, tile name) waiting / scored, on their own worker
    checker: threading.Thread | None = None
    check_queue: list[tuple[Path, str]] = []
    check_results: list[tuple[Path, str, float | None]] = []
    checking: set[str] = set()
    retries: dict[str, tuple[int, float]] = {}  # tile name -> (refilms so far, score before the last)
    logged = ""

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
        char, world, fog_key, north = session
        cls.north = north
        cls.folder = FOLDER / f"{char}_{world}_{fog_key}_n{round(north)}"
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
        cls.check_queue, cls.checking, cls.retries = [], set(), {}
        scene.log(f"tiles: {cls.folder.name}, {len(cls.index['tiles'])} on disk")

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
                up, right = (key[0] + du) * TILE_WORLD, (key[1] + dr) * TILE_WORLD
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
                cls.index.get("detail", {}).pop(name, None)
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
    # region Detail checks

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
                        score = _detail(folder / f"{tile}.png")
                    except Exception as ex:  # noqa: BLE001
                        scene.log(f"tiles: couldn't score {tile}: {ex!r}")
                        score = None
                    cls.check_results.append((folder, tile, score))
                    time.sleep(0.005)
            finally:
                cls.checker = None

        cls.checker = threading.Thread(target=run, name="bl4_minimap tiles detail", daemon=True)
        cls.checker.start()

    @classmethod
    def _apply_checks(cls) -> None:
        """Keeps scored tiles, or drops a blurry one so it's filmed again (see DETAIL_LOW)."""
        changed = False
        while cls.check_results:
            folder, name, score = cls.check_results.pop(0)
            cls.checking.discard(name)
            if folder != cls.folder or name not in cls.index["tiles"] or score is None:
                continue
            tries, before = cls.retries.get(name, (0, 0.0))
            if (score >= DETAIL_LOW or score < DETAIL_FLAT or tries >= DETAIL_TRIES
                    or (tries and score < before * DETAIL_GAIN)):  # fmt: skip
                cls.index.setdefault("detail", {})[name] = round(score, 3)
                cls.retries.pop(name, None)
                if tries:
                    scene.log(f"tiles: {name} filmed again, detail {before:.2f} -> {score:.2f}, kept")
            else:
                scene.log(f"tiles: {name} looks blurry (detail {score:.2f}), filming it again")
                cls.retries[name] = (tries + 1, score)
                del cls.index["tiles"][name]
                cls.index.get("detail", {}).pop(name, None)
                (folder / f"{name}.png").unlink(missing_ok=True)
                key = tuple(int(n) for n in name.split("_"))
                tile = cls.tiles.pop(key, None)  # type: ignore[call-overload]
                if tile is not None:
                    tile.slot.key = None
                    cls.version += 1
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
        up, right = (tile.key[0] + 0.5) * TILE_WORLD, (tile.key[1] + 0.5) * TILE_WORLD
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
        comp.OrthoWidth = TILE_WORLD * TILE_PX / (TILE_PX - 2 * MARGIN_PX) * scene.MAP_SCALE
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
        if f"{key[0]}_{key[1]}" not in cls.index.get("detail", {}):
            cls._queue_check(f"{key[0]}_{key[1]}")  # saved before detail checks existed
        return True

    @classmethod
    def _save_one(cls, pc: UObject) -> None:
        """While paused: exports one finished, unsaved tile."""
        now = time.monotonic()
        if now < cls.next_save or cls.folder is None:
            return
        tile = next((t for t in cls.tiles.values() if not t.saved and not t.settle), None)
        if tile is None:
            return
        cls.next_save = now + SAVE_GAP_S
        target = tile.slot.target()
        if target is None:
            return
        cls.folder.mkdir(parents=True, exist_ok=True)
        name = f"{tile.key[0]}_{tile.key[1]}"
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        krl.ExportRenderTarget(pc, target, str(cls.folder), f"{name}.png")
        tile.saved = True
        cls.index["tiles"][name] = tile.digest
        cls.index.get("detail", {}).pop(name, None)
        cls._write_index()
        cls._queue_check(name)

    # endregion
    # region Per frame

    @staticmethod
    def _cover(cx: float, cy: float, half: float, north: float) -> list[tuple[int, int]]:
        """The tiles a square of `half` around world (cx, cy) touches, nearest first."""
        c, s = math.cos(math.radians(north)), math.sin(math.radians(north))
        up, right = cx * c + cy * s, -cx * s + cy * c
        i0, i1 = math.floor((up - half) / TILE_WORLD), math.floor((up + half) / TILE_WORLD)
        j0, j1 = math.floor((right - half) / TILE_WORLD), math.floor((right + half) / TILE_WORLD)
        keys = [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
        keys.sort(key=lambda k: ((k[0] + 0.5) * TILE_WORLD - up) ** 2 + ((k[1] + 0.5) * TILE_WORLD - right) ** 2)
        return keys

    @classmethod
    def usable(cls, picture: float) -> bool:
        """Whether pictures this size can be put together from tiles (at most 3x3 of them)."""
        return cls.session is not None and 2 * picture <= 2 * TILE_WORLD

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
        char = scene_character(pc)
        if not world or char is None or not scene.Scene.in_viewer or 2 * picture > 2 * TILE_WORLD:
            if cls.session is not None:
                cls.drop()
            return
        fog_key = f"fog{round(strength * 100)}" if fog_on else "nofog"
        session = (char, world.lower(), fog_key, round(north, 1))
        if session != cls.session:
            cls._open(session)
        cls._check_fog(char, world.lower(), fog_on)
        if cls.check_results:
            cls._apply_checks()
        if paused:
            cls._save_one(pc)
            return
        now = time.monotonic()
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
        side = TILE_WORLD * scale
        inset = MARGIN_PX / TILE_PX
        for i, j in keys:
            # The picture's top is +up and its left -right (north up, like the capture)
            left = (j * TILE_WORLD - (right - half)) * scale
            top = ((up + half) - (i + 1) * TILE_WORLD) * scale
            canvas.K2_DrawTexture(cls.tiles[(i, j)].slot.target(), _v2(left, top), _v2(side, side), _v2(inset, inset),
                                  _v2(1 - 2 * inset, 1 - 2 * inset), white, BLEND_OPAQUE, 0.0, _v2(0.5, 0.5))  # fmt: skip
        krl.EndDrawCanvasToRenderTarget(pc, ctx)
        return True

    # endregion


def scene_character(pc: UObject) -> str | None:
    """The character's id, as fog.py keys its masks (imported late: fog imports scene)."""
    from . import fog  # noqa: PLC0415

    return fog._character(pc)  # noqa: SLF001
