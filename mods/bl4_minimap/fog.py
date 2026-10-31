"""
Fog of war on the minimap, the same as the ECHO map's.

The big map lays a fog plane over its map model: an `OakMapViewerFog` actor whose mesh (a plane)
uses the `MI_BigMapFogRipple` material with a 128x128 mask render target as its `Fog` texture
(white = not explored yet, black = explored). The game builds that mask natively from your
discovery data, only while the map is open, and Python can't read the discovery data itself.

So we copy it: whenever the map is open on this world's map, the mask is copied into our own
render target (a GPU-side copy, cheap), and a moment after the map opens it's saved once, still
behind the menu (one PNG per character and map, in the SDK settings folder, written by the
engine's own exporter, with the plane's placement and material). Our own fog plane, with the same material, sits over the
minimap's map model. A saved mask is loaded when you come back to that map, so the fog carries
over between sessions.

While the map is closed the mask follows your exploring live: the game keeps what you've explored
in `GbxDiscoveryFODManagerCPU` (not reflected), a 128x128 byte grid (255 = explored) that the map
turns into its mask when it opens (mask = 255 - grid, byte for byte). We read that grid every few
seconds and, when it changed, write it as the saved PNG and draw it into our mask.

Python references don't keep Unreal objects alive (the garbage collector doesn't see them; a
render target held only by Python crashed the game once it was collected). Everything we make is
held by the game: the mask by our material's `Fog` parameter, the material by the plane's mesh,
the plane by the level. Python keeps WeakPointers only.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR
from unrealsdk.unreal import IGNORE_STRUCT, UObject, WeakPointer

from . import scene
from .objectives import _read

FOLDER: Path = SETTINGS_DIR / "minimap_fog"
CHECK_INTERVAL = 0.5  # s between looks for an open map
SAVE_DELAY_S = 1.0  # after the map opens: its mask is complete by then
FOG_SEARCH_S = 2.0  # between searches for the open map's fog actor
MASK_SIZE = 128
RTF_RGBA8 = 2
MOVABLE = 2
BLEND_OPAQUE = 0
PLANE_MESH = "/Engine/BasicShapes/Plane.Plane"
FOG_PARAM = "Fog"
OPACITY_PARAM = "MaxOpacity"  # 1 hides unexplored land; the ECHO map shows it dimmed (about 0.7 here)
# The live explored grid in GbxDiscoveryFODManagerCPU (measured in Cowbell_P, 2026-10-27)
LIVE_CHECK_S = 4.0  # between reads of the live grid while the map is closed
LIVE_MIN_CHANGE = 4 * 255  # grid sum change worth applying (about 4 cells newly explored)
FOD_HEADER = 0x100  # bytes of the manager read at once
FOD_ORIGIN = 0x78  # 2 doubles: the grid's world X/Y corner
FOD_EXTENT = 0x88  # 2 doubles: its world size
FOD_DIMS = 0xA8  # 2 int32: width, height
FOD_CELLS = 0xB0  # TArray<uint8>: data pointer, int32 count (255 = explored)
INVERT = bytes(255 - i for i in range(256))


def _character(pc: UObject) -> str | None:
    state = pc.PlayerState
    if state is None:
        return None
    guid = state.ActiveCharGuid
    parts = [guid.A, guid.B, guid.C, guid.D]
    return "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts) if any(parts) else None


def _vector(values: list[float]) -> Any:
    return unrealsdk.make_struct("Vector", X=values[0], Y=values[1], Z=values[2])


def _png_gray(width: int, height: int, pixels: bytes) -> bytes:
    """An 8-bit grayscale PNG (unfiltered rows; zlib does the work in C)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\x00" + pixels[r * width:(r + 1) * width] for r in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


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
    applied = b""  # the grid last written into our mask
    next_check = 0.0
    worker: threading.Thread | None = None  # writing the PNG
    written: tuple[tuple[str, str], bytes] | None = None  # (key, grid) the worker wrote, to draw

    @classmethod
    def _find(cls, world: str, paused: bool) -> UObject | None:
        manager = cls.manager()
        if manager is not None:
            return manager
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
        return manager

    @classmethod
    def grid(cls, world: str, paused: bool, placement: dict[str, Any]) -> bytes | None:
        """The explored grid, if it's there and covers this world's fog plane (else None)."""
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
        return _read(cells, count)


class Fog:
    changed = False  # the mask was redrawn: the minimap should film again
    strength: float | None = None  # MaxOpacity set on our plane
    plane = WeakPointer()  # our fog plane (StaticMeshActor)
    mask = WeakPointer()  # our copy of the mask (held by the plane's material)
    key: tuple[str, str] | None = None  # (character, world) the plane is for
    placement: dict[str, Any] | None = None  # loc/scale/rot/material of the plane
    unsaved = False  # the mask changed since it was saved
    was_open = False  # the big map was open at the last check
    opened_at = 0.0  # when we saw it open
    saved_this_opening = False
    next_check = 0.0

    @classmethod
    def _paths(cls) -> tuple[Path, Path]:
        char, world = cls.key or ("", "")
        base = FOLDER / f"{char}_{world}"
        return base.with_suffix(".png"), base.with_suffix(".json")

    @classmethod
    def actor(cls) -> UObject | None:
        return cls.plane()

    # region Our plane

    @classmethod
    def _build(cls, pc: UObject, placement: dict[str, Any]) -> bool:
        """Spawns our fog plane (and its mask) where the game's goes. False if something's missing."""
        cls.destroy()
        mesh = scene._load(PLANE_MESH)  # noqa: SLF001
        parent = scene._load(placement["material"])  # noqa: SLF001
        if mesh is None or parent is None:
            scene.log(f"fog: couldn't load {PLANE_MESH} / {placement['material']}")
            return False
        loc = placement["loc"]
        where = (scene.ORIGIN[0] + loc[0], scene.ORIGIN[1] + loc[1], scene.ORIGIN[2] + loc[2])
        context = scene.Scene.context() or pc  # the big map's world, like the model
        actor = scene._spawn(context, unrealsdk.find_class("StaticMeshActor"), where)  # noqa: SLF001
        comp = actor.StaticMeshComponent
        comp.SetMobility(MOVABLE)
        comp.SetStaticMesh(mesh)
        actor.K2_SetActorRotation(unrealsdk.make_struct("Rotator", Pitch=placement["rot"][0], Yaw=placement["rot"][1],
                                                        Roll=placement["rot"][2]), False)  # fmt: skip
        actor.SetActorScale3D(_vector(placement["scale"]))
        actor.SetActorEnableCollision(False)
        kml = unrealsdk.find_class("KismetMaterialLibrary").ClassDefaultObject
        mid = kml.CreateDynamicMaterialInstance(pc, parent, "None", 0)
        comp.SetMaterial(0, mid)  # the component holds the material from here on
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        white = unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1)
        mask = krl.CreateRenderTarget2D(pc, MASK_SIZE, MASK_SIZE, RTF_RGBA8, white, False, False)
        krl.ClearRenderTarget2D(pc, mask, white)  # all fogged until we know better
        mid.SetTextureParameterValue(FOG_PARAM, mask)  # the material holds the mask from here on
        comp.SetVisibleInSceneCaptureOnly(True)
        cls.plane, cls.mask = WeakPointer(actor), WeakPointer(mask)
        cls.placement = placement
        return True

    @classmethod
    def _draw(cls, pc: UObject, texture: UObject) -> None:
        """Copies a mask texture (the game's render target, or our saved PNG) into ours."""
        mask = cls.mask()
        if mask is None:
            return
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, mask, None, IGNORE_STRUCT, IGNORE_STRUCT)
        v = lambda x, y: unrealsdk.make_struct("Vector2D", X=x, Y=y)  # noqa: E731
        white = unrealsdk.make_struct("LinearColor", R=1, G=1, B=1, A=1)
        canvas.K2_DrawTexture(texture, v(0, 0), v(MASK_SIZE, MASK_SIZE), v(0, 0), v(1, 1), white, BLEND_OPAQUE, 0.0,
                              v(0.5, 0.5))  # fmt: skip
        krl.EndDrawCanvasToRenderTarget(pc, ctx)

    # endregion
    # region Saving

    @classmethod
    def _save(cls, pc: UObject) -> None:
        """Writes the mask (the engine's PNG exporter, no pixel loop in Python) and its placement."""
        mask = cls.mask()
        if not cls.unsaved or mask is None or cls.placement is None or cls.key is None:
            return
        png, meta = cls._paths()
        FOLDER.mkdir(parents=True, exist_ok=True)
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        krl.ExportRenderTarget(pc, mask, str(FOLDER), png.name)
        meta.write_text(json.dumps(cls.placement, indent=1), encoding="utf-8")
        cls.unsaved = False

    @classmethod
    def _restore(cls, pc: UObject) -> None:
        """Our plane from the saved mask for this character and map, if there is one."""
        png, meta = cls._paths()
        if not png.exists() or not meta.exists():
            return
        try:
            placement = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not cls._build(pc, placement):
            return
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        texture = krl.ImportFileAsTexture2D(pc, str(png))
        if texture is not None:
            cls._draw(pc, texture)  # used at once, in this same call
        cls.unsaved = False
        cls.changed = True

    @classmethod
    def _follow_live(cls, pc: UObject, world: str, paused: bool) -> None:
        """While the map is closed: the game's live explored grid into the saved PNG (on a worker:
        the write can take a few ms) and then into our mask. tiles.py re-films only the tiles whose
        part of the PNG changed."""
        done = _Live.written
        if done is not None:
            _Live.written = None
            key, grid = done
            png, _ = cls._paths()
            if key == cls.key and cls.mask() is not None:
                krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
                texture = krl.ImportFileAsTexture2D(pc, str(png))
                if texture is not None:
                    cls._draw(pc, texture)  # used at once, in this same call
                    _Live.applied = grid
                    cls.unsaved = False
                    cls.changed = True
            return
        now = time.monotonic()
        if now < _Live.next_check or _Live.worker is not None or cls.placement is None or cls.key is None:
            return
        _Live.next_check = now + LIVE_CHECK_S
        grid = _Live.grid(world, paused, cls.placement)
        if grid is None or cls.mask() is None:
            return
        # Explored cells only brighten; a few soft-edge steps aren't worth filming tiles again for
        if _Live.applied and abs(sum(grid) - sum(_Live.applied)) < LIVE_MIN_CHANGE:
            return
        key, placement = cls.key, cls.placement
        png, meta = cls._paths()

        def write() -> None:
            try:
                FOLDER.mkdir(parents=True, exist_ok=True)
                tmp = png.with_suffix(".tmp")
                tmp.write_bytes(_png_gray(MASK_SIZE, MASK_SIZE, grid.translate(INVERT)))
                os.replace(tmp, png)  # tiles.py's worker may be reading it: then try again next time
                if not meta.exists():
                    meta.write_text(json.dumps(placement, indent=1), encoding="utf-8")
                _Live.written = (key, grid)
            except OSError:
                pass
            finally:
                _Live.worker = None

        _Live.worker = threading.Thread(target=write, name="bl4_minimap fog", daemon=True)
        _Live.worker.start()

    # endregion
    # region The game's fog

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
    def _game_fog(map_class_path: str) -> tuple[UObject, UObject] | None:
        """
        The open big map's fog (mesh, mask render target), if it shows this world's map (the map
        menu can show other maps too). Searches every object only when the map has a new fog actor,
        i.e. once per opening, behind the menu.
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
        mesh = fog.FogMesh
        material = mesh.GetMaterial(0)
        for param in getattr(material, "TextureParameterValues", []):
            if str(param.ParameterInfo.Name) == FOG_PARAM and param.ParameterValue is not None:
                return mesh, param.ParameterValue
        return None

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

    # endregion

    @classmethod
    def _set_strength(cls, strength: float) -> None:
        actor = cls.plane()
        if actor is None:
            cls.strength = None
            return
        if strength == cls.strength:
            return
        material = actor.StaticMeshComponent.GetMaterial(0)
        if material is not None:
            material.SetScalarParameterValue(OPACITY_PARAM, strength)
            cls.strength = strength
            cls.changed = True

    @classmethod
    def sync(cls, pc: UObject, world_name: str, strength: float, paused: bool) -> None:
        """
        Called every frame (paused too: the game pauses while the map is open). `strength`: 0..1,
        how dark unexplored land is.
        """
        now = time.monotonic()
        if now < cls.next_check:
            return
        cls.next_check = now + CHECK_INTERVAL
        try:
            cls._sync(pc, world_name, paused)
        finally:
            cls._set_strength(strength)

    @classmethod
    def _sync(cls, pc: UObject, world_name: str, paused: bool) -> None:
        char = _character(pc)
        info = scene.map_info(world_name)
        if char is None or info is None:
            return
        key = (char, world_name.lower())
        if key != cls.key:
            cls._save(pc)  # the last map's, if the map closed with the level (travel from the map)
            cls.destroy()
            cls.key = key
            _Live.applied = b""
            cls._restore(pc)
        elif cls.plane() is None and cls.placement is not None:
            cls._restore(pc)  # our plane went with the world (map change back and forth)

        if not cls._map_open(paused):
            cls.was_open = False
            cls._follow_live(pc, world_name, paused)
            return
        if not cls.was_open:
            cls.was_open = True
            cls.opened_at = time.monotonic()
            cls.saved_this_opening = False
        found = cls._game_fog(info["actor"])
        if found is None:
            return
        mesh, source = found
        loc, scale, rot = mesh.K2_GetComponentLocation(), mesh.K2_GetComponentScale(), mesh.K2_GetComponentRotation()
        material = mesh.GetMaterial(0)
        parent = getattr(material, "Parent", None) or material
        placement = {
            "loc": [loc.X, loc.Y, loc.Z],
            "scale": [scale.X, scale.Y, scale.Z],
            "rot": [rot.Pitch, rot.Yaw, rot.Roll],
            "material": parent._path_name(),  # noqa: SLF001
        }
        if cls.plane() is None or placement != cls.placement:
            if not cls._build(pc, placement):
                return
        cls._draw(pc, source)  # GPU copy only; saved when the map closes
        cls.unsaved = True
        # Save while the map is still open, a moment after it opened: the export reads the texture
        # back from the GPU (a hitch of up to ~45 ms, measured), hidden behind the paused menu here.
        # Saving as the map closed put that hitch on the first frames of play.
        if not cls.saved_this_opening and time.monotonic() - cls.opened_at >= SAVE_DELAY_S:
            cls._save(pc)
            cls.saved_this_opening = True
        cls.changed = True

    @classmethod
    def destroy(cls) -> None:
        cls.was_open = False
        actor = cls.plane()
        if actor is not None:
            try:
                actor.K2_DestroyActor()
            except Exception as ex:  # noqa: BLE001
                scene.log(f"fog: destroy failed: {ex!r}")
        cls.plane = WeakPointer()
        cls.mask = WeakPointer()
        cls.placement = None
