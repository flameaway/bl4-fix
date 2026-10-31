"""
The picture in the minimap: the game's own big-map model of this world, filmed from above.

The big map (ECHO menu) draws each world with a "map actor" (maps.json, from the game's
`gbx_discovery_viewable_map_def` data): a textured plane for small maps, one textured mesh per
region for the open world. Its model is the world at 1/100 scale around the same origin.

The big map films its model in a private world of its own (the OakMapViewer's), with no sun, sky
or post-process volumes, and its camera's own post-process settings and show flags. To look the
same we spawn our copy of the model in that same world, far below the big map's own (ORIGIN), on
the same dark background plane, and film it with an orthographic SceneCapture2D with the big map
camera's settings, looking straight down over the point that matches the player. (Filmed in the
level instead, the level's sun and post-processing washed it out.) If that world can't be found we
fall back to the level, unlit.

The capture renders only our actors (show-only), so nothing else in either world costs anything.

Pictures are cached. The map art never changes, but each capture costs ~3.9 ms of GPU (~1.1 ms of
it the fog plane) and a new picture is filmed 5 times while its texture detail streams in. So
pictures sit on a grid (in the north-up frame, spacing = how far the view may slide in one) and the
last CACHE_SIZE of them are kept, each with its own capture and render target: walking back into an
area shows its picture again with no capture at all. A fog change (only when the ECHO map was
open) marks them stale: each is filmed again the next time it's needed.
"""

from __future__ import annotations

import json
import math
import time
from importlib import resources
from typing import Any

import unrealsdk
from unrealsdk import logging
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer

# Through importlib.resources: an installed .sdkmod is a zip, where a Path next to __file__ doesn't exist
MAPS = json.loads((resources.files(__package__) / "maps.json").read_text(encoding="utf-8"))["maps"]

MAP_SCALE = 0.01  # map units per world unit
ORIGIN = (0.0, 0.0, -2_000_000.0)  # where the map model goes: 20 km under the world
CAMERA_HEIGHT = 5000.0  # map units above the model
# Each picture covers PICTURE_SCALE times the minimap's range, north up; the widget shows the part
# around the player and turns it, so a new picture is only needed after walking about one range
PICTURE_SCALE = 2.5
RT_SIZE = 1024  # about 400 px across the minimap's view
RTF_RGBA8 = 2
# The big map's camera writes into a linear target (display gamma 1) and the ECHO menu shows those
# values as they are, which is the dark, saturated look. UMG instead treats a texture's values as
# linear light and gamma-encodes them for the screen, which washed ours out (with RGBA8 and with
# RGBA8_SRGB alike). So our camera writes with display gamma 1/2.2: UMG's encoding undoes it and the
# screen gets the same values the ECHO map shows.
TARGET_GAMMA = 1 / 2.2
ORTHOGRAPHIC = 1
CAPTURE_SOURCE_FINAL_LDR = 2
PRIMITIVE_RENDER_SHOW_ONLY = 2
BACKGROUND_MATERIAL = "/Game/GameData/Map/Materials/M_BigMapBackground.M_BigMapBackground"
PLANE_MESH = "/Engine/BasicShapes/Plane.Plane"
BACKGROUND_Z = -10.0  # map units under the model, as the big map's
BACKGROUND_SCALE = 1000.0  # the Plane is 100 units: 100,000 map units wide
# The map texture is virtual: its detail streams in from what earlier renders asked for, so a new
# picture is filmed again for a moment until it's sharp: every SETTLE_EVERY_S for SETTLE_S, then
# once more at the end. It was every frame (~60 captures after each new picture, in a vehicle every
# 3-4 s); one capture of the 1024^2 target costs ~4.85 ms GPU + ~2 ms game thread (measured
# 2026-10-24), so 5 now
SETTLE_S = 1.0
SETTLE_EVERY_S = 0.25
CACHE_SIZE = 3  # filmed pictures kept (when tiles can't be used): 4 MB of video memory each plus an idle capture actor
COMPOSED_SIZE = 4  # pictures put together from tiles (tiles.py) kept
VIEWER_SEARCH_S = 2.0  # between searches for the big map's world while paused (each searches every object)
MOVABLE = 2
OVERLAY_COMPONENTS = ("OakMapViewerRegionMeshComponent", "OakMapViewerLabelComponent")  # not the Split ones
# Our extras on top of the big map camera's own show flags: costly and not needed for the map
EXTRA_HIDDEN_SHOW_FLAGS = (
    "DistanceFieldAO", "GlobalIllumination", "ReflectionEnvironment", "SkyLighting", "Particles",
    "MotionBlur", "Atmosphere", "Cloud", "LocalFogVolumes", "ContactShadows", "VolumetricLightmap",
    "PostProcessMaterial",  # the big map's loading effect (see ready())
)  # fmt: skip
# Fallback (filming in the level): unlit and stripped down
HIDDEN_SHOW_FLAGS = (
    "Fog", "Atmosphere", "VolumetricFog", "Cloud", "LocalFogVolumes", "MotionBlur", "Bloom",
    "EyeAdaptation", "LensFlares", "Vignette", "Grain", "DepthOfField", "AmbientOcclusion",
    "Lighting",  # the world's light washes the map art out; unlit shows its own colours
    # Not needed for a flat unlit map, and costly to render every update. Keep anti-aliasing:
    # with it off the picture comes out with alpha 0 and the widget draws nothing
    "DynamicShadows", "ContactShadows", "CapsuleShadows",
    "DistanceFieldAO", "GlobalIllumination", "LumenGlobalIllumination", "LumenReflections",
    "ScreenSpaceReflections", "ReflectionEnvironment", "SkyLighting", "VolumetricLightmap",
    "Particles", "Decals", "ScreenSpaceAO", "LightShafts", "PostProcessMaterial", "MotionBlur",
    "SeparateTranslucency", "ScreenPercentage",
)  # fmt: skip


def log(msg: str) -> None:
    logging.info(f"[Minimap] {msg}")


def _load(path: str) -> UObject | None:
    """Loads an asset (blocking) by object path, e.g. a blueprint's `/Game/X.Y_C`."""
    lib = unrealsdk.find_class("AssetRegistryHelpers").ClassDefaultObject
    registry = lib.GetAssetRegistry()
    get = BoundFunction(unrealsdk.find_class("AssetRegistry")._find("GetAssetByObjectPath"), registry)
    result = lib.GetAsset(get(path, False))
    return result[0] if isinstance(result, tuple) else result


def _spawn(pc: UObject, cls: UObject, where: tuple[float, float, float]) -> UObject:
    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    xf = unrealsdk.make_struct(
        "Transform",
        Translation=unrealsdk.make_struct("Vector", X=where[0], Y=where[1], Z=where[2]),
        Rotation=unrealsdk.make_struct("Quat", W=1),
        Scale3D=unrealsdk.make_struct("Vector", X=1, Y=1, Z=1),
    )
    spawned = gs.BeginDeferredActorSpawnFromClass(pc, cls, xf, 1, None, 0)
    actor = spawned[0] if isinstance(spawned, tuple) else spawned
    done = gs.FinishSpawningActor(actor, xf, 0)
    return done[0] if isinstance(done, tuple) else done


def map_info(world_name: str) -> dict[str, Any] | None:
    return MAPS.get(world_name.lower())


class Viewer:
    """
    The big map's viewer actor (its private world) and camera, found once per session. Finding it
    means searching every object (17-72 ms), so it's only done while the game is paused (a menu
    hides the hitch) or once per level while it loads in; never repeatedly during play.
    """

    actor = WeakPointer()
    next_search = 0.0

    @classmethod
    def get(cls, *, search: bool = False) -> UObject | None:
        viewer = cls.actor()
        if viewer is not None or not search:
            return viewer
        now = time.monotonic()
        if now < cls.next_search:
            return None
        cls.next_search = now + VIEWER_SEARCH_S
        for found in unrealsdk.find_all("OakMapViewer", exact=True):
            if "Default__" not in found._path_name() and found.ViewSceneCaptureComponent is not None:  # noqa: SLF001
                cls.actor = WeakPointer(found)
                return found
        return None

    @classmethod
    def camera(cls, *, search: bool = False) -> UObject | None:
        viewer = cls.get(search=search)
        return viewer.ViewSceneCaptureComponent if viewer is not None else None


class _Entry:
    """One cached picture: its own capture actor (whose TextureTarget holds the render target)."""

    def __init__(self, capture: UObject, target: UObject, kind: str):
        self.capture = WeakPointer(capture)
        self.target = WeakPointer(target)
        self.kind = kind  # "film": filmed directly; "compose": drawn from tiles (tiles.py)
        self.version = -1  # composed: the tiles' version it was drawn from
        self.key: tuple | None = None  # (grid up, grid right, yaw, radius)
        self.shot: tuple[float, float, float, float] | None = None  # (x, y, yaw, radius) filmed
        self.settle_until = 0.0
        self.settled_at = 0.0
        self.used = 0.0
        self.stale = True  # needs filming before it can be shown

    def alive(self) -> bool:
        return self.capture() is not None and self.target() is not None


class Scene:
    world = WeakPointer()  # the level we're showing the map of
    world_name = ""
    context = WeakPointer()  # what we spawn with: the big map's viewer (its world), or the player controller
    in_viewer = False
    searched_world = WeakPointer()  # the level we last searched for the big map's world in
    actor = WeakPointer()  # the map model
    background = WeakPointer()
    extra = WeakPointer()  # filmed along with the model (the fog plane)
    extra_address: int | None = None
    entries: list[_Entry] = []  # cached pictures, see the module docstring
    current: _Entry | None = None  # the one shown
    # The current entry's capture and render target. Only WeakPointers: Unreal's garbage collector
    # doesn't see Python references, so a target kept in a plain field outlives its camera (map
    # change) as a dangling pointer, and using it crashed the game. Each target is held by its
    # camera's TextureTarget (and the shown one by the widget's brush too).
    capture = WeakPointer()
    target = WeakPointer()
    missing_logged = ""
    fresh = False  # just spawned or what's filmed changed: the cached pictures are stale
    shot: tuple[float, float, float, float] | None = None  # (x, y, yaw, radius) of the shown picture

    @classmethod
    def ready(cls, pc: UObject, world: UObject, *, paused: bool = False) -> bool:
        """Spawns the map model and the camera for this world if needed. False: no map here."""
        if world is cls.world() and cls.actor() is not None and cls.capture() is not None:
            if cls.in_viewer:
                return True
            if Viewer.get(search=paused) is None:
                return True  # unlit fallback look until a pause lets us look for the big map's camera
            # The big map's camera turned up: rebuild with its look
        elif world is not cls.searched_world():
            # A new level: one search now, while it loads in, is allowed
            cls.searched_world = WeakPointer(world)
            Viewer.next_search = 0.0
            Viewer.get(search=True)
        cls.destroy()
        name = str(world.Name)
        info = map_info(name)
        if info is None:
            if cls.missing_logged != name:
                log(f"no map for {name}")
                cls.missing_logged = name
            return False
        map_class = _load(info["actor"])
        if map_class is None:
            log(f"couldn't load {info['actor']}")
            return False
        viewer = Viewer.get()
        # In the level, not the big map's private world: the map texture is virtual, and its detail
        # only streams in for what the level's views ask for. Filmed in the big map's world it went
        # grey (no detail) unless the ECHO map had just been opened over that area.
        context = pc
        actor = _spawn(context, map_class, ORIGIN)
        actor.SetActorEnableCollision(False)
        # Only the textured map: the region outlines/fills and labels are the big map's hover and
        # zoom overlays. Nothing needs to tick (the open world's model has 37 hover timelines).
        actor.SetActorTickEnabled(False)
        primitive = unrealsdk.find_class("PrimitiveComponent")
        for comp in actor.K2_GetComponentsByClass(unrealsdk.find_class("ActorComponent")):
            comp.SetComponentTickEnabled(False)
            if comp.Class.Name in OVERLAY_COMPONENTS:
                comp.SetVisibility(False, False)
            if comp.Class._inherits(primitive):
                comp.SetVisibleInSceneCaptureOnly(True)
        background = cls._background(context)
        cls.world, cls.world_name = WeakPointer(world), name
        cls.context, cls.in_viewer = WeakPointer(context), viewer is not None
        cls.actor = WeakPointer(actor)
        cls.background = WeakPointer(background) if background is not None else WeakPointer()
        cls._show(cls._new_entry(pc))  # the first picture's camera, filmed on first use
        look = "the big map camera's look" if viewer is not None else "unlit (big map camera not found yet)"
        log(f"map for {name}: {info['actor'].rsplit('.', 1)[-1]}, {look}")
        cls.fresh = True
        return True

    @classmethod
    def shown_actors(cls) -> list[UObject]:
        return [a for a in (cls.actor(), cls.background(), cls.extra()) if a is not None]

    @classmethod
    def _new_entry(cls, pc: UObject, kind: str = "film") -> _Entry:
        """A camera and render target for one cached picture (a composed one: TargetGamma 1, so
        tile-to-picture draws are byte-exact; a filmed one: the capture's gamma)."""
        capture, target = cls.make_capture(pc, RT_SIZE, TARGET_GAMMA if kind == "film" else 1.0)
        entry = _Entry(capture, target, kind)
        cls.entries.append(entry)
        return entry

    @classmethod
    def make_capture(cls, pc: UObject, size: int, gamma: float) -> tuple[UObject, UObject]:
        """An orthographic camera with the big map camera's look, filming only on request, and its
        render target (`size`^2, TargetGamma `gamma`), held by the camera's TextureTarget."""
        context = cls.context() or pc
        capture = _spawn(context, unrealsdk.find_class("SceneCapture2D"), ORIGIN)
        comp = capture.CaptureComponent2D
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        clear = unrealsdk.make_struct("LinearColor", R=0, G=0, B=0, A=1)
        target = krl.CreateRenderTarget2D(pc, size, size, RTF_RGBA8, clear, False, False)
        target.TargetGamma = gamma  # before its first use
        comp.TextureTarget = target  # holds it from here on
        comp.bCaptureEveryFrame = False
        comp.bCaptureOnMovement = False
        comp.ProjectionType = ORTHOGRAPHIC
        comp.CaptureSource = CAPTURE_SOURCE_FINAL_LDR
        viewer = Viewer.get() if cls.in_viewer else None
        camera = viewer.ViewSceneCaptureComponent if viewer is not None else None
        if camera is not None:
            # The big map camera's look: its show flags (lighting on, no outlines, ...) and post-process
            flags = [
                unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=f.ShowFlagName, Enabled=f.Enabled)
                for f in camera.ShowFlagSettings
            ]
            named = {str(f.ShowFlagName) for f in camera.ShowFlagSettings}
            flags += [
                unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=n, Enabled=False)
                for n in EXTRA_HIDDEN_SHOW_FLAGS if n not in named
            ]  # fmt: skip
            # Unlit: the map's materials go flat grey under the level's (or no) light; unlit matches
            # the ECHO map's colours (measured: on-screen mean ~(64,78,67) vs the map's ~(52,72,67))
            flags += [
                unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=n, Enabled=False)
                for n in ("Lighting", "Vignette", "Grain", "LensFlares")
            ]  # fmt: skip
            comp.SetShowFlagSettings(flags)
            comp.PostProcessSettings = camera.PostProcessSettings
            # But not its post-process materials: the big map blends in a "loading" effect
            # (MI_PP_Map_LoadingEffect, a grey radial gradient) while it opens and closes, and a copy
            # taken at that moment left the minimap solid grey
            comp.PostProcessSettings.WeightedBlendables.Array = []
            comp.PostProcessBlendWeight = camera.PostProcessBlendWeight
            comp.CaptureSource = camera.CaptureSource
        else:
            comp.SetShowFlagSettings(
                [unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=n, Enabled=False) for n in HIDDEN_SHOW_FLAGS],
            )
        comp.PrimitiveRenderMode = PRIMITIVE_RENDER_SHOW_ONLY
        comp.ShowOnlyActors = cls.shown_actors()
        return capture, target

    @classmethod
    def _show(cls, entry: _Entry) -> None:
        cls.current = entry
        cls.capture, cls.target = entry.capture, entry.target
        cls.shot = entry.shot
        entry.used = time.monotonic()

    @staticmethod
    def _background(context: UObject) -> UObject | None:
        """The big map's dark background plane, under our model."""
        mesh, material = _load(PLANE_MESH), _load(BACKGROUND_MATERIAL)
        if mesh is None or material is None:
            return None
        plane = _spawn(context, unrealsdk.find_class("StaticMeshActor"), (ORIGIN[0], ORIGIN[1], ORIGIN[2] + BACKGROUND_Z))
        comp = plane.StaticMeshComponent
        comp.SetMobility(MOVABLE)
        comp.SetStaticMesh(mesh)
        comp.SetMaterial(0, material)
        plane.SetActorScale3D(unrealsdk.make_struct("Vector", X=BACKGROUND_SCALE, Y=BACKGROUND_SCALE, Z=1))
        plane.SetActorEnableCollision(False)
        comp.SetVisibleInSceneCaptureOnly(True)
        return plane

    @classmethod
    def show_also(cls, extra: UObject | None) -> None:
        """Films `extra` (the fog plane) along with the map model."""
        # Called several times a second: only an actual change touches the cameras
        address = extra._get_address() if extra is not None else None  # noqa: SLF001
        if cls.actor() is None or address == cls.extra_address:
            return
        cls.extra_address = address
        cls.extra = WeakPointer(extra) if extra is not None else WeakPointer()
        shown = cls.shown_actors()
        for entry in cls.entries:
            capture = entry.capture()
            if capture is not None:
                capture.CaptureComponent2D.ShowOnlyActors = shown
        cls.fresh = True  # every cached picture was filmed without it (or with it)

    @classmethod
    def invalidate(cls) -> None:
        """What's filmed changed (fog): every filmed picture is filmed again when next needed.
        Composed ones follow their tiles (tiles.py re-films only tiles whose fog changed)."""
        for entry in cls.entries:
            if entry.kind == "film":
                entry.stale = True
        cls.fresh = False

    @staticmethod
    def _key(x: float, y: float, yaw: float, radius: float, grid: float) -> tuple[tuple, float, float]:
        """The grid point nearest world (x, y) in the north-up frame: (key, its world x, y)."""
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        up, right = x * c + y * s, -x * s + y * c
        if grid > 0:
            up, right = round(up / grid) * grid, round(right / grid) * grid
        key = (round(up, 1), round(right, 1), round(yaw, 3), round(radius, 1))
        return key, up * c - right * s, up * s + right * c

    @classmethod
    def _take(cls, pc: UObject, kind: str) -> _Entry:
        """An entry of this kind to (re)draw: a new one, one never used, or the least recently shown."""
        cls.entries = [e for e in cls.entries if e.alive()]
        mine = [e for e in cls.entries if e.kind == kind]
        entry = next((e for e in mine if e.key is None), None)
        if entry is not None:
            return entry
        if len(mine) < (CACHE_SIZE if kind == "film" else COMPOSED_SIZE):
            return cls._new_entry(pc, kind)
        return min((e for e in mine if e is not cls.current), key=lambda e: e.used, default=mine[0])

    @classmethod
    def _compose(cls, pc: UObject, key: tuple, x: float, y: float, yaw: float, radius: float) -> bool:
        """Shows the picture for `key` drawn from tiles (cached, or drawn now). False if tiles are
        missing (or can't be used at this size)."""
        from . import tiles  # noqa: PLC0415 - tiles imports this module

        if not tiles.Tiles.usable(radius):
            return False
        entry = next((e for e in cls.entries if e.kind == "compose" and e.key == key and e.alive()), None)
        if entry is not None and entry.version == tiles.Tiles.version and not entry.stale:
            if entry is not cls.current:
                cls._show(entry)
            return True
        entry = entry or cls._take(pc, "compose")
        target = entry.target()
        if target is None or not tiles.Tiles.compose(pc, target, x, y, radius, yaw):
            return False
        entry.key, entry.shot, entry.version = key, (x, y, yaw, radius), tiles.Tiles.version
        entry.stale, entry.settle_until = False, 0.0
        cls._show(entry)
        return True

    @classmethod
    def needs_film(cls, *, alive: bool = True) -> bool:
        """`alive=False`: skip the pointer checks (two WeakPointer calls; the caller asks at a slower rate)."""
        return cls.current is None or cls.current.stale or (alive and not cls.current.alive())

    @classmethod
    def use(cls, pc: UObject, x: float, y: float, yaw: float, radius: float, grid: float) -> bool:
        """
        Shows the cached picture for the grid point nearest world point (x, y) (north-up frame,
        `grid` world units apart), filming it first if it isn't cached or is stale. Returns True if
        the shown picture changed.
        """
        key, cx, cy = cls._key(x, y, yaw, radius, grid)
        previous = cls.current
        if cls._compose(pc, key, cx, cy, yaw, radius):
            return cls.current is not previous
        entry = next((e for e in cls.entries if e.kind == "film" and e.key == key and e.alive()), None)
        if entry is not None and not entry.stale:
            if entry is cls.current:
                return False
            cls._show(entry)
            return True
        entry = entry or cls._take(pc, "film")
        entry.key = key
        cls._film(entry, cx, cy, yaw, radius)
        cls._show(entry)
        return True

    @classmethod
    def tick_tiles(
        cls, pc: UObject, x: float, y: float, yaw: float, radius: float, room: float, *,
        fog_on: bool, strength: float, paused: bool,
    ) -> bool:  # fmt: skip
        """
        Every frame: lets tiles.py load or film a tile, then draws the shown picture from tiles if
        it can now (a filmed stand-in is replaced, a composed one redrawn when its tiles changed).
        Returns True if the shown picture changed.
        """
        from . import tiles  # noqa: PLC0415

        tiles.Tiles.tick(pc, x, y, radius, room, yaw, fog_on=fog_on, strength=strength, paused=paused)
        entry = cls.current
        if entry is None or entry.shot is None or entry.key is None or not tiles.Tiles.usable(radius):
            return False
        if entry.kind == "compose" and entry.version == tiles.Tiles.version:
            return False
        sx, sy, syaw, sradius = entry.shot
        if syaw != yaw or sradius != radius:
            return False
        if entry.kind == "compose":
            target = entry.target()
            if target is not None and tiles.Tiles.compose(pc, target, sx, sy, sradius, syaw):
                entry.version = tiles.Tiles.version
                return True
            return False
        return cls._compose(pc, entry.key, sx, sy, syaw, sradius)

    @classmethod
    def _film(cls, entry: _Entry, x: float, y: float, yaw: float, radius: float) -> None:
        """
        Films the map around world point (x, y): `radius` world units to the edge, `yaw` (degrees)
        pointing up.
        """
        capture = entry.capture()
        if capture is None:
            return
        where = unrealsdk.make_struct(
            "Vector",
            X=ORIGIN[0] + x * MAP_SCALE,
            Y=ORIGIN[1] + y * MAP_SCALE,
            Z=ORIGIN[2] + CAMERA_HEIGHT,
        )
        # Looking straight down, the top of the picture is the camera's yaw
        rot = unrealsdk.make_struct("Rotator", Pitch=-90.0, Yaw=yaw, Roll=0.0)
        capture.K2_SetActorLocationAndRotation(where, rot, False, unrealsdk.make_struct("HitResult"), True)
        comp = capture.CaptureComponent2D
        comp.OrthoWidth = 2 * radius * MAP_SCALE
        comp.CaptureScene()
        now = time.monotonic()
        if entry.stale or entry.shot != (x, y, yaw, radius):
            entry.settle_until = now + SETTLE_S
        entry.settled_at = now
        entry.shot = (x, y, yaw, radius)
        entry.stale = False

    @classmethod
    def settle(cls) -> bool:
        """
        Films the shown picture again while its texture detail is still streaming in: every
        SETTLE_EVERY_S, and once more when SETTLE_S is over. True if it filmed. A cached picture
        that settled once is never filmed again (unless it goes stale).
        """
        entry = cls.current
        if entry is None or entry.shot is None or not entry.settle_until:
            return False
        now = time.monotonic()
        last = now >= entry.settle_until
        if not last and now - entry.settled_at < SETTLE_EVERY_S:
            return False
        until = entry.settle_until
        cls._film(entry, *entry.shot)
        entry.settle_until = 0.0 if last else until
        return True

    @classmethod
    def settling(cls) -> bool:
        """Whether the shown picture may still change (redraw the minimap from it each frame)."""
        entry = cls.current
        if entry is None or entry.shot is None:
            return False
        now = time.monotonic()
        return now < entry.settle_until or now - entry.settled_at < SETTLE_EVERY_S

    @classmethod
    def destroy(cls) -> None:
        from . import tiles  # noqa: PLC0415

        tiles.Tiles.drop()
        for ptr in [e.capture for e in cls.entries] + [cls.actor, cls.background]:
            actor = ptr()
            if actor is not None:
                try:
                    actor.K2_DestroyActor()
                except Exception as ex:  # noqa: BLE001
                    log(f"destroy failed: {ex!r}")
        cls.entries = []
        cls.current = None
        cls.actor = WeakPointer()
        cls.capture = WeakPointer()
        cls.target = WeakPointer()
        cls.background = WeakPointer()
        cls.extra = WeakPointer()
        cls.extra_address = None
        cls.context = WeakPointer()
        cls.in_viewer = False
        cls.world = WeakPointer()
        cls.world_name = ""
        cls.shot = None
