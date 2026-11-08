"""
Fog of war on the minimap, from the game's own explored grid, live.

The game keeps what you've explored in `GbxDiscoveryFODManagerCPU` (not reflected): a 128x128 byte
grid (255 = explored) over a world square, which the ECHO map turns into its fog mask when it
opens (mask = 255 - grid, byte for byte). We read that grid (one 16 KB memory read) and the
minimap draws it over the map itself: unexplored land in FOG_RGB at the fog strength's opacity
(widget.py: a texture drawn over the picture; the HUD page: an image over its tiles). The map is
filmed without fog, so a change shows within a read, with no filming again.

Until 0.1.12 the fog was filmed into the map: a copy of the ECHO map's fog plane (its own material
over a mask we copied from the open map, saved as a PNG, then kept up to date from the grid every
4 s) sat in the filmed scene, so a change showed only once the mask was written, read back and
the pictures under it filmed again (6-10 s).

What's still taken from the ECHO map: where its fog plane sits (the world square the grid must
match), read while the map is open and saved per character and map (JSON in the SDK settings
folder), so the grid is checked against it in later sessions without opening the map.
"""

from __future__ import annotations

import json
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR
from unrealsdk.unreal import UObject, WeakPointer

from . import scene
from .objectives import _read

FOLDER: Path = SETTINGS_DIR / "minimap_fog"
CHECK_INTERVAL = 0.5  # s between looks for an open map
FOG_SEARCH_S = 2.0  # between searches for the open map's fog actor
MASK_SIZE = 128
# The live explored grid in GbxDiscoveryFODManagerCPU (measured in Cowbell_P, 2026-10-27)
FOD_HEADER = 0x100  # bytes of the manager read at once
FOD_ORIGIN = 0x78  # 2 doubles: the grid's world X/Y corner
FOD_EXTENT = 0x88  # 2 doubles: its world size
FOD_DIMS = 0xA8  # 2 int32: width, height
FOD_CELLS = 0xB0  # TArray<uint8>: data pointer, int32 count (255 = explored)
# The fog layer's colour (unexplored land: this, at the fog strength's opacity, over the map)
FOG_RGB = (6, 16, 30)
PNG_SIGNATURE = bytes((0x89,)) + b"PNG" + bytes((13, 10, 26, 10))


def layer_png(grid: bytes, width: int, height: int, opacity: float) -> bytes:
    """
    The fog layer for an explored grid (255 = explored): an RGBA PNG, unexplored FOG_RGB at
    `opacity`, explored clear. ~0.25 ms for 128x128 (the byte work is done in C: translate, slices,
    zlib).
    """
    n = width * height
    pixels = bytearray(n * 4)
    for channel, value in enumerate(FOG_RGB):
        pixels[channel::4] = bytes((value,)) * n
    pixels[3::4] = grid.translate(bytes(round((255 - v) * opacity) for v in range(256)))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    stride = width * 4
    rows = b"".join(bytes(1) + pixels[r * stride : (r + 1) * stride] for r in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b"")


def _character(pc: UObject) -> str | None:
    state = pc.PlayerState
    if state is None:
        return None
    guid = state.ActiveCharGuid
    parts = [guid.A, guid.B, guid.C, guid.D]
    return "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts) if any(parts) else None


def _actors_of(context: UObject, actor_class: UObject | str) -> list[UObject] | None:
    """The actors of a class in `context`'s world (None if that can't be asked)."""
    try:
        if isinstance(actor_class, str):
            actor_class = unrealsdk.find_class(actor_class)
        gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
        result = gs.GetAllActorsOfClass(context, actor_class, [])
    except (ValueError, AttributeError, TypeError):
        return None
    return [a for a in (result[1] if isinstance(result, tuple) else result) if a is not None]


class _Viewer:
    """The open big map's current fog actor (searching for it is slow: once per opening)."""

    fog = WeakPointer()
    next_fog_search = 0.0


class _Live:
    """The game's live explored grid (GbxDiscoveryFODManagerCPU, one transient object)."""

    manager = WeakPointer()
    vtable = 0  # the class's vtable (from its CDO): checked before every read
    searched: str | None = None  # the world we last searched in (one search per world, or while paused)
    found = False  # `manager` was set by the last search (so it going away means search again)

    @classmethod
    def _find(cls, world: str, paused: bool) -> UObject | None:
        manager = cls.manager()
        if manager is not None:
            return manager
        if cls.found:
            # The one we had is gone while the world's name stayed: the map was loaded again (hosting
            # co-op reloads it, and co-op never pauses), or we'd caught the old one in the overlap
            cls.found = False
            cls.searched = None
        if cls.searched == world and not paused:
            return None
        cls.searched = world
        cls.manager = WeakPointer()
        for candidate in unrealsdk.find_all("GbxDiscoveryFODManagerCPU", exact=True):
            if "Default__" in candidate._path_name():  # noqa: SLF001
                head = _read(candidate._get_address(), 8)  # noqa: SLF001
                cls.vtable = struct.unpack("<Q", head)[0] if head else 0
            elif manager is None:
                manager = candidate
        if manager is not None:
            cls.manager = WeakPointer(manager)
            cls.found = True
        return manager

    @classmethod
    def read(cls, world: str, paused: bool, placement: dict[str, Any]) -> tuple[bytes, tuple[float, ...]] | None:
        """The explored grid and the world square it covers (X, Y corner, X, Y size), or None."""
        manager = cls._find(world, paused)
        if manager is None or not cls.vtable:
            return None
        head = _read(manager._get_address(), FOD_HEADER)  # noqa: SLF001
        if head is None or struct.unpack_from("<Q", head, 0)[0] != cls.vtable:
            return None
        width, height = struct.unpack_from("<ii", head, FOD_DIMS)
        cells, count = struct.unpack_from("<Qi", head, FOD_CELLS)
        if width != MASK_SIZE or height != MASK_SIZE or count != width * height:
            return None
        # Same square as the fog plane (map units x 100 = world): not another world's grid
        span = placement["scale"][0] * 10000
        ox, oy = struct.unpack_from("<dd", head, FOD_ORIGIN)
        ex, ey = struct.unpack_from("<dd", head, FOD_EXTENT)
        loc = placement["loc"]
        expect = (loc[0] * 100 - span / 2, loc[1] * 100 - span / 2, span, span)
        if any(abs(a - b) > span * 0.01 for a, b in zip((ox, oy, ex, ey), expect)):
            return None
        cells_raw = _read(cells, count)
        return (cells_raw, (ox, oy, ex, ey)) if cells_raw is not None else None


class Fog:
    key: tuple[str, str] | None = None  # (character, world) the placement is for
    placement: dict[str, Any] | None = None  # {"loc", "scale"} of the ECHO map's fog plane
    next_check = 0.0

    @classmethod
    def _path(cls) -> Path:
        char, world = cls.key or ("", "")
        return FOLDER / f"{char}_{world}.json"

    @staticmethod
    def _map_open(paused: bool) -> bool:
        """Whether the big map is open: its camera has a render target only then."""
        camera = scene.Viewer.camera(search=paused)
        return camera is not None and camera.TextureTarget is not None

    @staticmethod
    def _usable(fog: UObject | None) -> bool:
        if fog is None:
            return False
        mesh = fog.FogMesh
        # Old ones linger after the map closes: scaled to 0, no material
        return mesh is not None and mesh.K2_GetComponentScale().X > 0 and mesh.GetNumMaterials() > 0

    @staticmethod
    def _game_fog(map_class_path: str) -> UObject | None:
        """
        The open big map's fog mesh, if it shows this world's map (the map menu can show other
        maps too). Searches every object only when the map has a new fog actor, i.e. once per
        opening, behind the menu.
        """
        fog = _Viewer.fog()
        if not Fog._usable(fog):
            fog = None
            now = time.monotonic()
            if now < _Viewer.next_fog_search:
                return None
            _Viewer.next_fog_search = now + FOG_SEARCH_S
            # In the big map's own world (GetAllActorsOfClass there: well under 1 ms; `find_all` over
            # every object was ~12 ms, in one tick)
            viewer = scene.Viewer.get()
            candidates = _actors_of(viewer, "OakMapViewerFog") if viewer is not None else None
            if candidates is None:
                candidates = unrealsdk.find_all("OakMapViewerFog", exact=True)
            for candidate in candidates:
                if "Default__" not in candidate._path_name() and Fog._usable(candidate):  # noqa: SLF001
                    if Fog._map_in(candidate.Outer, map_class_path):
                        fog = candidate
                        break
            _Viewer.fog = WeakPointer(fog) if fog is not None else WeakPointer()
            if fog is None:
                return None
        return fog.FogMesh

    @staticmethod
    def _map_in(level: UObject, map_class_path: str) -> bool:
        """Whether a live actor of the map's class sits in this level (the map menu's world)."""
        try:
            cls = unrealsdk.find_object("BlueprintGeneratedClass", map_class_path)
        except ValueError:
            return False
        actors = _actors_of(level, cls)  # (the level's world)
        for actor in actors if actors is not None else unrealsdk.find_all(cls, exact=True):
            if actor.Outer is level and "Default__" not in actor._path_name():  # noqa: SLF001
                root = actor.RootComponent
                if root is not None and root.K2_GetComponentScale().X > 0:
                    return True
        return False

    @classmethod
    def sync(cls, pc: UObject, world_name: str, paused: bool) -> None:
        """Called every frame (paused too: the game pauses while the map is open)."""
        now = time.monotonic()
        if now < cls.next_check:
            return
        cls.next_check = now + CHECK_INTERVAL
        char = _character(pc)
        info = scene.map_info(world_name)
        if char is None or info is None:
            return
        key = (char, world_name.lower())
        if key != cls.key:
            cls.key = key
            try:
                cls.placement = json.loads(cls._path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cls.placement = None  # (until the ECHO map is opened on this map once)
        if not cls._map_open(paused):
            return
        mesh = cls._game_fog(info["actor"])
        if mesh is None:
            return
        loc, scale = mesh.K2_GetComponentLocation(), mesh.K2_GetComponentScale()
        placement = {"loc": [loc.X, loc.Y, loc.Z], "scale": [scale.X, scale.Y, scale.Z]}
        if placement != cls.placement:
            cls.placement = placement
            try:  # (a few hundred bytes, written behind the paused menu, once per change)
                FOLDER.mkdir(parents=True, exist_ok=True)
                cls._path().write_text(json.dumps(placement, indent=1), encoding="utf-8")
            except OSError as ex:
                scene.log(f"fog: couldn't save {cls._path().name}: {ex!r}")

    @classmethod
    def clear(cls) -> None:
        cls.key = cls.placement = None
        cls.next_check = 0.0
