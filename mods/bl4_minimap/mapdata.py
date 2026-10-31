"""
The ECHO map's icons and route line, copied for the minimap.

While the big map is open, its private world holds one actor per icon (`OakMapViewerPOI`s:
standard, area and vault tracker icons, each with an `OakMapViewerPOIIconComponent` whose material
instance carries the icon texture and flags: `IsHidden`, `PinOpacity`, `IsBlip`) and the route to
your waypoint (`OakMapViewerWaypointPath`: a list of spline-mesh segments from your position to the
destination). Positions are in map space (world / 100, same origin). The game builds all of it
natively when the map opens, and none of it exists while the map is closed.

So once per opening (a moment after it opens, behind the paused menu) we read it all and keep it,
and again whenever your custom waypoint changes while the map is open (placing a pin there
redraws the map's route to it): icons and the route are saved per character and map (JSON in the
SDK settings folder) so they're there next session. The minimap shows the icons in range and the
route as a dotted line, trimmed to the part still ahead of you. Open the map again to refresh them.
Icons the minimap draws live instead (your waypoint pin, players, mission givers) are left out
or replaced by the caller.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from collections.abc import Generator
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR
from unrealsdk.unreal import UObject, WeakPointer

from . import scene

FOLDER: Path = SETTINGS_DIR / "minimap_icons"
SNAPSHOT_DELAY_S = 1.0  # after the map opens: let it finish placing its icons and route
# Every icon actor on the map is an OakMapViewerPOI (seen: BP_MapPOI_StandardIcon_C,
# _StandardAreaIcon_C, _VaultTrackerAreaIcon_C, _PlayerIcon_C). Players are drawn live (markers.py).
POI_CLASS = "OakMapViewerPOI"
SKIP_POI_CLASSES = frozenset({"BP_MapPOI_PlayerIcon_C"})
MAP_TO_WORLD = 100.0
ROUTE_START_SLACK = 5000.0  # world units: a route starting this close to the player is the live one
ROUTE_END_SLACK = 3000.0  # world units: a route ending this close to the waypoint pin leads to it
RESNAP_DELAY_S = 0.5  # after the waypoint pin changes with the map open: let it redraw the route
# While the map is open: look whether its route line changed this often. The game redraws it some
# time after a pin moves; the one read 0.5 s after the pin change could still be the old line.
ROUTE_POLL_S = 0.25
REFIND_S = 1.0  # search for new route actors at most this often, only when needed
JOB_BUDGET_S = 0.0025  # per frame for a read of the open map (`MapData.run`)
REFIND_FOR_S = 10.0  # ... and only this long after the map opened or the pin moved
# World units: closer than this to the ECHO map's route, you're on it; further, it was copied from
# somewhere else and the way from you to it is planned over the graph (`joined`)
JOIN_SLACK = 1500.0
CURVE_STEP = 5.0  # map units (5 m) between the points a curved route segment is copied as
CURVE_MIN_BEND = 1.0  # map units: a segment bending less than this from straight stays one line
CURVE_MAX_POINTS = 16
# World units: a route line no longer than this is one the game has reset (as the map closes, its
# lines all sit at the map's origin for a moment: World_P 2026-10-29)
EMPTY_ROUTE = 100.0
PIN_CUSTOM_WAYPOINT = 1
DEFAULT_ROUTE_COLOR = (0.565, 0.804, 0.859, 1.0)
# The tracked mission's objective icons on the map: ico_main_mission, ico_side_mission_dlc, a
# bounty's ico_contract_mission, an activity's ico_activity_waypoint, ... (_brdcrmb: the marker when
# the objective is through a door or in another area). Drawn live instead (objectives.py).
OBJECTIVE_ICON = re.compile(
    r"^ico_((main|side)_mission(_brdcrmb)?(_optnl)?(_dlc)?|contract_mission|faction_mission"
    r"|activity_waypoint(_brdcrmb|_optnl)?)$",
    re.IGNORECASE,
)
# Mission givers: ico_side_mission_npc[_dlc], ico_faction_mission_npc, ico_dlc_mission_giver_maingame
GIVER_ICON = re.compile(r"^ico_(\w+_mission_npc(_dlc)?|dlc_mission_giver_\w+)$", re.IGNORECASE)
# Rift events and the other world events (they come and go): Cowbell's ico_rift_boss /
# ico_dlc_rift_boss_totem, Harmonica's Digi-Rifts, and ico_world_events (world boss "Rift Champion"
# events, caravans, migrations: NCS missiontype Event). Shown only with "Always show rift events".
RIFT_ICON = re.compile(r"^ico_(rift_boss|dlc_rift_boss_\w+|world_events)$", re.IGNORECASE)
# Drawn live by the minimap (your pin: markers.py, players), so the map's copies would be stale doubles
LIVE_ICON = re.compile(r"^(ico_Custom_Waypoint_PlayerOwned|player_class_\w+)$", re.IGNORECASE)


def _icon_name(texture: str) -> str:
    return texture.rsplit(".", 1)[-1]


def is_objective(texture: str) -> bool:
    """A mission objective's icon (not a mission giver's: those end in _npc)."""
    return OBJECTIVE_ICON.search(_icon_name(texture)) is not None


def is_giver(texture: str) -> bool:
    """A mission giver's icon (someone with a mission for you)."""
    return GIVER_ICON.search(_icon_name(texture)) is not None


def is_rift(texture: str) -> bool:
    """A rift event's or another world event's icon."""
    return RIFT_ICON.search(_icon_name(texture)) is not None


def is_live(texture: str) -> bool:
    """An icon the minimap draws from live data instead (your own waypoint pin, players)."""
    return LIVE_ICON.search(_icon_name(texture)) is not None


def pin_location(pc: UObject, world_name: str) -> tuple[float, float, float] | None:
    """Your custom waypoint (the map pin the game routes you to), when it's on this map."""
    state = pc.PlayerState
    if state is None:
        return None
    for pin in state.DiscoveryPinningState.PinnedDatas:
        if pin.pintype == PIN_CUSTOM_WAYPOINT and str(pin.PinnedCustomWaypointWorldName).lower() == world_name.lower():
            loc = pin.PinnedCustomWaypointLocation
            return (float(loc.X), float(loc.Y), float(loc.Z))
    return None


def _character(pc: UObject) -> str | None:
    state = pc.PlayerState
    if state is None:
        return None
    guid = state.ActiveCharGuid
    parts = [guid.A, guid.B, guid.C, guid.D]
    return "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts) if any(parts) else None


def _icons_of(actor: UObject, poi_component: UObject) -> list[dict[str, Any]]:
    """The visible icons of one of the open map's icon actors."""
    icons = []
    for comp in actor.K2_GetComponentsByClass(poi_component):
        if not comp.bVisible or comp.K2_GetComponentScale().X <= 0:
            continue  # an old one from an earlier opening, on its way out
        textures, scalars = _params(comp.MaterialInstance)
        texture = textures.get(str(comp.MaterialTextureParamName))
        if texture is None or scalars.get(str(comp.MaterialIsHiddenParamName), 0.0) > 0.5:
            continue
        loc = comp.K2_GetComponentLocation()
        icons.append({
            "x": round(loc.X * MAP_TO_WORLD, 1),
            "y": round(loc.Y * MAP_TO_WORLD, 1),
            "texture": texture._path_name(),  # noqa: SLF001
            "blip": scalars.get(str(comp.MaterialIsBlipParamName), 0.0) > 0.5,
            "pinned": scalars.get(str(comp.MaterialPinOpacityParamName), 0.0) > 0.5,
            "area": bool(comp.bIsAreaComponent),
        })
    return icons


def _params(material: UObject | None) -> tuple[dict[str, Any], dict[str, float]]:
    textures: dict[str, Any] = {}
    scalars: dict[str, float] = {}
    if material is None:
        return textures, scalars
    for p in getattr(material, "TextureParameterValues", []):
        textures[str(p.ParameterInfo.Name)] = p.ParameterValue
    for p in getattr(material, "ScalarParameterValues", []):
        scalars[str(p.ParameterInfo.Name)] = float(p.ParameterValue)
    return textures, scalars


def _name_number(actor: UObject) -> int:
    """The number in an actor's name (OakMapViewerWaypointPath_2147354118): smaller is newer."""
    tail = str(actor.Name).rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 1 << 62


def _nearest_on(points: list[tuple[float, float]], x: float, y: float) -> tuple[float, int, float]:
    """The point of a polyline nearest (x, y): (squared distance, segment, fraction along it)."""
    best, best_i, best_t = math.inf, 0, 0.0
    for i, ((ax, ay), (bx, by)) in enumerate(zip(points, points[1:])):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        d = (ax + dx * t - x) ** 2 + (ay + dy * t - y) ** 2
        if d < best:
            best, best_i, best_t = d, i, t
    return best, best_i, best_t


# Per line (by identity): where on it the player was last, and its segment lengths. A whole line
# was walked twice per update (on it? + where the dots go): 0.35 ms with the curves' 378 points.
_LINES: dict[int, list[Any]] = {}  # id(line) -> [line, last segment, calls, lengths]
WINDOW_BACK, WINDOW_AHEAD = 20, 60  # segments searched around the last spot
FULL_EVERY = 30  # calls between whole-line searches (you might have jumped: a teleport)


def _line_info(points: list[tuple[float, float]]) -> list[Any]:
    info = _LINES.get(id(points))
    if info is None or info[0] is not points:
        if len(_LINES) > 32:  # noqa: PLR2004
            _LINES.clear()
        lengths = [math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:])]
        info = _LINES[id(points)] = [points, -1, 0, lengths]
    return info


def _nearest_near(points: list[tuple[float, float]], x: float, y: float) -> tuple[float, int, float]:
    """`_nearest_on`, searching only around where you were on this line last time (mostly)."""
    info = _line_info(points)
    info[2] += 1
    last = info[1]
    if last < 0 or info[2] % FULL_EVERY == 0:
        found = _nearest_on(points, x, y)
    else:
        lo = max(0, last - WINDOW_BACK)
        found = _nearest_on(points[lo : last + WINDOW_AHEAD + 2], x, y)
        found = (found[0], found[1] + lo, found[2])
    info[1] = found[1]
    return found


def near_line(points: list[tuple[float, float]], x: float, y: float) -> bool:
    """Whether (x, y) is on a route line (within `JOIN_SLACK`)."""
    return len(points) >= 2 and _nearest_near(points, x, y)[0] <= JOIN_SLACK**2  # noqa: PLR2004


def _curve(start: Any, start_tangent: Any, end: Any, end_tangent: Any) -> list[tuple[float, float]]:
    """
    The points after `start` along one spline mesh segment of the map's route (map units). A spline
    mesh bends along the Hermite curve of its ends and tangents (UE's CubicInterp); the ends alone
    cut every corner. A segment that's nearly straight stays one line (fewer points per update).
    """
    p0, t0 = (start.X, start.Y), (start_tangent.X, start_tangent.Y)
    p1, t1 = (end.X, end.Y), (end_tangent.X, end_tangent.Y)

    def at(a: float) -> tuple[float, float]:
        a2, a3 = a * a, a * a * a
        h00, h10, h01, h11 = 2 * a3 - 3 * a2 + 1, a3 - 2 * a2 + a, -2 * a3 + 3 * a2, a3 - a2
        return (h00 * p0[0] + h10 * t0[0] + h01 * p1[0] + h11 * t1[0],
                h00 * p0[1] + h10 * t0[1] + h01 * p1[1] + h11 * t1[1])  # fmt: skip

    mid = at(0.5)
    bend = math.hypot(mid[0] - (p0[0] + p1[0]) / 2, mid[1] - (p0[1] + p1[1]) / 2)
    length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if not math.isfinite(bend) or bend < CURVE_MIN_BEND:
        return [p1]
    n = max(2, min(CURVE_MAX_POINTS, math.ceil(length / CURVE_STEP)))
    return [*(at(k / n) for k in range(1, n)), p1]


class MapData:
    key: tuple[str, str] | None = None  # (character, world)
    icons: list[dict[str, Any]] = []  # noqa: RUF012  # {"x", "y", "texture", "blip", "pinned"} world units
    routes: list[list[tuple[float, float]]] = []  # noqa: RUF012  # world-unit polylines
    route_pin: tuple[float, float] | None = None  # your waypoint pin when the route was copied (x, y)
    route_color = DEFAULT_ROUTE_COLOR  # the copied line's colour (the map colours it by mission)
    # The map's line to your waypoint pin, learned when a copy reaches the pin. A line we plan to the
    # pin ourselves used `route_color`: the tracked bounty's orange (2026-10-29)
    pin_color = DEFAULT_ROUTE_COLOR
    mission_color = DEFAULT_ROUTE_COLOR  # the map's last line to a mission (not to your pin)
    opened_at = 0.0  # when we saw the map open (0: closed)
    snapped = False  # read this opening already
    snap_pin: tuple[float, float, float] | None = None  # the pin when we last read (or None)
    resnap_at = 0.0  # read again at this time (the pin changed with the map open); 0: no
    version = 0  # bumped when icons or routes change
    paths: list[WeakPointer] = []  # noqa: RUF012  # the open map's route actors
    route_sig: tuple[Any, ...] = ()  # what the route actors showed when last read (`_signature`)
    poll_at = 0.0  # next look at the route line while the map is open
    refind_at = 0.0  # next search for route actors allowed
    changed_at = 0.0  # when the map opened or the pin last moved
    join_key: tuple[Any, int] | None = None  # (planned route, version) `joined` last worked out
    job: Generator[None, None, None] | None = None  # a read of the open map in progress (`run`)
    world_query: bool | None = None  # GetAllActorsOfClass finds the map's icons (None: not known yet)
    fallback_used = False  # find_all tried once because it found none
    join_result: list[tuple[float, float]] | None = None

    @classmethod
    def _path(cls) -> Path:
        char, world = cls.key or ("", "")
        return FOLDER / f"{char}_{world}.json"

    @classmethod
    def _load(cls) -> None:
        cls.icons, cls.routes, cls.route_pin = [], [], None
        try:
            data = json.loads(cls._path().read_text(encoding="utf-8"))
            cls.icons = data.get("icons", [])
            cls.routes = [[tuple(p) for p in route] for route in data.get("routes", [])]
            pin = data.get("route_pin")
            cls.route_pin = (float(pin[0]), float(pin[1])) if pin else None
            cls.route_color = tuple(data.get("route_color", DEFAULT_ROUTE_COLOR))
            cls.pin_color = tuple(data.get("pin_color", DEFAULT_ROUTE_COLOR))
            cls.mission_color = tuple(data.get("mission_color", cls.route_color))
        except (OSError, ValueError):
            pass
        cls.version += 1

    @classmethod
    def _save(cls) -> None:
        FOLDER.mkdir(parents=True, exist_ok=True)
        tmp = cls._path().with_suffix(".tmp")
        data = {"icons": cls.icons, "routes": cls.routes, "route_color": cls.route_color,
                "pin_color": cls.pin_color, "mission_color": cls.mission_color, "route_pin": cls.route_pin}
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cls._path())

    @classmethod
    def run(cls) -> None:
        """
        Every frame while a read of the open map runs (`job`): works on it for up to JOB_BUDGET_S.
        The game is paused behind the map then, but a long tick still stutters the menu: one read in
        one tick was 12-27 ms (Fortress_Grasslands_P, 14 icons; two `find_all`s of ~12 ms each, then a
        UFunction call or four per icon and route segment).
        """
        if cls.job is None:
            return
        end = time.perf_counter() + JOB_BUDGET_S
        try:
            while time.perf_counter() < end:
                next(cls.job)
        except StopIteration:
            cls.job = None
        except Exception as ex:  # noqa: BLE001 - a read gone wrong mustn't break the minimap
            cls.job = None
            scene.log(f"map data: read failed: {ex!r}")

    @classmethod
    def _actors(cls, viewer: UObject, class_name: str) -> list[WeakPointer]:
        """
        The open map's actors of a class, from its own world (`GetAllActorsOfClass` with the viewer as
        world context: under 0.1 ms where `find_all` walked every object, ~12 ms). If that finds no
        icons with the map open, `find_all` once per session to tell whether it missed them.
        """
        level = viewer.Outer
        found: list[Any] = []
        if cls.world_query is not False:
            try:
                actor_class = unrealsdk.find_class(class_name)
                gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
                result = gs.GetAllActorsOfClass(viewer, actor_class, [])
                found = [a for a in (result[1] if isinstance(result, tuple) else result) if a is not None]
            except (ValueError, AttributeError, TypeError):
                found = []
            if class_name == POI_CLASS and found:
                cls.world_query = True
        if cls.world_query is False:
            found = list(unrealsdk.find_all(class_name, exact=class_name != POI_CLASS))
        elif not found and class_name == POI_CLASS and not cls.fallback_used:
            cls.fallback_used = True
            found = list(unrealsdk.find_all(class_name, exact=False))
            if any(a.Outer is level for a in found):
                cls.world_query = False  # the world query missed them: find_all from now on
                scene.log("map data: GetAllActorsOfClass found no map icons, using find_all")
        return [WeakPointer(a) for a in found if a.Outer is level]

    @classmethod
    def _start(cls, job: Generator[None, None, None]) -> None:
        cls.job = job
        cls.run()

    @classmethod
    def _snapshot_job(cls, pc: WeakPointer, pin: tuple[float, float, float] | None) -> Generator[None, None, None]:
        """
        Reads the open map's icons and route, a piece per step (`run`). Only WeakPointers are kept
        from one step to the next.
        """
        viewer = scene.Viewer.get()
        if viewer is None:
            return
        pois = cls._actors(viewer, POI_CLASS)
        viewer = None
        poi_component = unrealsdk.find_class("OakMapViewerPOIIconComponent")
        icons: list[dict[str, Any]] = []
        for ptr in pois:
            yield
            actor = ptr()
            if actor is None or actor.bHidden or str(actor.Class.Name) in SKIP_POI_CLASSES:
                continue  # (class default objects live in their package, not the viewer's level)
            icons.extend(_icons_of(actor, poi_component))
            actor = None
        yield
        viewer = scene.Viewer.get()
        if viewer is None:
            return
        paths = cls._actors(viewer, "OakMapViewerWaypointPath")
        viewer = None
        cls.paths = paths
        cls.route_sig = cls._signature([a for a in (p() for p in paths) if a is not None])
        routes = yield from cls._routes_job(paths, pc, pin)
        if routes is None:
            routes = cls.routes  # lines the game has just reset: keep what we read before
        yield
        if icons:
            cls.icons = icons
        cls.routes = routes
        cls.route_pin = (pin[0], pin[1]) if pin is not None else None
        cls._save()
        cls.version += 1
        scene.log(f"map data: {len(icons)} icons, {len(routes)} route(s)")

    @classmethod
    def _routes_job(cls, paths: list[WeakPointer], pc: WeakPointer,
                    pin: tuple[float, float, float] | None) -> Generator[None, None, list[list[tuple[float, float]]] | None]:  # fmt: skip
        """
        The map's live route line (world units), as a list of one (or none), and its colour; a
        segment per step.

        The map keeps every line it drew while it's open (World_P 2026-10-29: 6 at once, 4 white-blue
        to the tracked bounty and 2 orange from a mission before), all visible to us. The live one is
        the newest: each redraw makes a new actor, and the game names it with a smaller number than
        the one before (2147391455 -> 2147362869 -> 2147354118 over the day; the newest was the line
        the map showed). The pin can't pick it: a pin moved or removed on the open map isn't in
        `PinnedDatas` until the map closes, so the line to the old pin was kept (and its colour).
        None when every line read was one the game had reset (all at the map's origin, as the map
        closes); keep the previous copy then.
        """
        lines: list[tuple[int, list[tuple[float, float]], tuple[float, float, float, float] | None]] = []
        reset = 0
        for ptr in paths:
            yield
            path_actor = ptr()
            if path_actor is None or path_actor.bHidden:
                continue
            number = _name_number(path_actor)
            # This step: the segments' meshes and the line's colour (property reads); then a segment
            # (four UFunction calls) per step
            meshes: list[WeakPointer] = []
            color = None
            for segment in path_actor.PathSplines:
                mesh = segment.SplineMesh
                if mesh is None or not mesh.bVisible:
                    continue
                meshes.append(WeakPointer(mesh))
                if color is None:
                    for p in getattr(segment.Material, "VectorParameterValues", []):
                        if str(p.ParameterInfo.Name) == "LineColor":
                            c = p.ParameterValue
                            color = (c.R, c.G, c.B, 1.0)
            path_actor = mesh = segment = None
            points: list[tuple[float, float]] = []
            for mesh_ptr in meshes:
                yield
                mesh = mesh_ptr()
                if mesh is None:
                    continue
                start, end = mesh.GetStartPosition(), mesh.GetEndPosition()
                if not points:
                    points.append((start.X * MAP_TO_WORLD, start.Y * MAP_TO_WORLD))
                bent = _curve(start, mesh.GetStartTangent(), end, mesh.GetEndTangent())
                points.extend((x * MAP_TO_WORLD, y * MAP_TO_WORLD) for x, y in bent)
                mesh = start = end = None
            if len(points) < 2:  # noqa: PLR2004
                continue
            if max(abs(x - points[0][0]) + abs(y - points[0][1]) for x, y in points) < EMPTY_ROUTE:
                reset += 1
                continue
            lines.append((number, points, color))
        if reset and not lines:
            return None
        if not lines:
            return []
        # The live line starts where the player stands (the map was opened from here)
        controller = pc()
        pawn = controller.Pawn if controller is not None else None
        if pawn is not None:
            loc = pawn.K2_GetActorLocation()
            here = [ln for ln in lines if (ln[1][0][0] - loc.X) ** 2 + (ln[1][0][1] - loc.Y) ** 2 <= ROUTE_START_SLACK**2]
            lines = here or lines
        _, points, color = min(lines, key=lambda ln: ln[0])  # the newest
        if color is not None:
            cls.route_color = color
            at_pin = pin is not None and (points[-1][0] - pin[0]) ** 2 + (points[-1][1] - pin[1]) ** 2 <= ROUTE_END_SLACK**2
            if at_pin:
                cls.pin_color = color
            else:
                cls.mission_color = color
        return [points]

    @classmethod
    def sync(cls, pc: UObject, world_name: str) -> None:
        """Call every so often (the map can be open: the game is paused then)."""
        char = _character(pc)
        if char is None or scene.map_info(world_name) is None:
            return
        key = (char, world_name.lower())
        if key != cls.key:
            cls.key = key
            cls._load()
        camera = scene.Viewer.camera()
        is_open = camera is not None and camera.TextureTarget is not None
        now = time.monotonic()
        if not is_open:
            cls.job = None  # (a read cut short by the map closing: its actors are gone)
            cls.opened_at = 0.0
            cls.snapped = False
            cls.resnap_at = 0.0
            cls.paths, cls.route_sig = [], ()  # its actors go with it
            return
        if cls.opened_at == 0.0:
            cls.opened_at = cls.changed_at = now
        pin = pin_location(pc, world_name)
        if cls.job is not None:
            if cls.snapped and pin != cls.snap_pin:
                cls.snap_pin = pin
                cls.resnap_at = now + RESNAP_DELAY_S
                cls.changed_at = now
            return  # a read is running (`run`, every frame)
        if not cls.snapped and now - cls.opened_at >= SNAPSHOT_DELAY_S:
            cls.snapped = True
            cls.snap_pin = pin
            cls._start(cls._snapshot_job(WeakPointer(pc), pin))
            cls.poll_at = now + ROUTE_POLL_S
        elif cls.snapped and pin != cls.snap_pin:
            # A pin placed, moved or removed on the open map: it redraws its route; read it again
            cls.snap_pin = pin
            cls.resnap_at = now + RESNAP_DELAY_S
            cls.changed_at = now
        elif cls.resnap_at and now >= cls.resnap_at:
            cls.resnap_at = 0.0
            cls._start(cls._snapshot_job(WeakPointer(pc), pin))
            cls.poll_at = now + ROUTE_POLL_S
        elif cls.snapped and now >= cls.poll_at:
            cls.poll_at = now + ROUTE_POLL_S
            cls._poll_route(pc, pin, now)

    @staticmethod
    def _signature(actors: list[UObject]) -> tuple[Any, ...]:
        """Each shown route's segment count, start and end: a cheap look at whether it changed."""
        sig = []
        for actor in actors:
            if actor.bHidden:
                continue
            shown = [s.SplineMesh for s in actor.PathSplines if s.SplineMesh is not None and s.SplineMesh.bVisible]
            if shown:
                start, end = shown[0].GetStartPosition(), shown[-1].GetEndPosition()
                sig.append((len(shown), round(start.X), round(start.Y), round(end.X), round(end.Y)))
        return tuple(sig)

    @classmethod
    def _poll_route(cls, pc: UObject, pin: tuple[float, float, float] | None, now: float) -> None:
        """
        While the map is open: copy its route line again once it changed. After a pin moves the game
        redraws the line later than our re-read 0.5 s on, sometimes (user report 2026-10-29: the
        minimap only got the new line when they stayed in the map a while, or not at all).
        """
        actors = [a for a in (w() for w in cls.paths) if a is not None]
        reaches_pin = pin is None or any(
            (r[-1][0] - pin[0]) ** 2 + (r[-1][1] - pin[1]) ** 2 <= ROUTE_END_SLACK**2 for r in cls.routes if r
        )
        if (not actors or not reaches_pin) and now >= cls.refind_at and now - cls.changed_at <= REFIND_FOR_S:
            # The line may be drawn by an actor that wasn't there at the last read
            cls.refind_at = now + REFIND_S
            viewer = scene.Viewer.get()
            if viewer is None:
                return
            cls.paths = cls._actors(viewer, "OakMapViewerWaypointPath")
            actors = [a for a in (w() for w in cls.paths) if a is not None]
        sig = cls._signature(actors)
        if sig == cls.route_sig:
            return
        cls.route_sig = sig
        cls._start(cls._poll_job(list(cls.paths), WeakPointer(pc), pin, now))

    @classmethod
    def _poll_job(cls, paths: list[WeakPointer], pc: WeakPointer, pin: tuple[float, float, float] | None,
                  now: float) -> Generator[None, None, None]:  # fmt: skip
        """The route line read again (it changed), a segment per step."""
        routes = yield from cls._routes_job(paths, pc, pin)
        if routes is None or routes == cls.routes:
            return
        cls.routes = routes
        cls.route_pin = (pin[0], pin[1]) if pin is not None else None
        cls._save()
        cls.version += 1
        scene.log(f"map data: route redrawn {now - cls.changed_at:.1f} s after the map opened / the pin moved")

    @classmethod
    def route_dots(cls, x: float, y: float, spacing: float, reach: float,
                   routes: list[list[tuple[float, float]]] | None = None) -> list[tuple[float, float]]:  # fmt: skip
        """
        Dots every `spacing` along the routes, within `reach` of the player (x, y): from where the
        player is along the route on (the part already walked is left out), plus a short lead-in
        from the player to the route. The route's dots sit at fixed distances along it, so they
        stay put on the ground as you move (re-spacing them from the player made them crawl).
        """
        dots: list[tuple[float, float]] = []
        reach2 = reach * reach
        for points in cls.routes if routes is None else routes:
            if len(points) < 2:  # noqa: PLR2004
                continue
            lengths = _line_info(points)[3]
            # The point of the route nearest the player: the route goes on from there
            best, best_i, best_t = _nearest_near(points, x, y)
            (ax, ay), (bx, by) = points[best_i], points[best_i + 1]
            px, py = ax + (bx - ax) * best_t, ay + (by - ay) * best_t
            # Lead-in: from the player towards the route, when off it (only the part in range: the
            # route can be far away, and dots all the way there cost hundreds of updates a frame)
            gap = math.sqrt(best)
            steps = int(min(gap, reach) // spacing)
            for k in range(1, steps + 1):
                f = k * spacing / gap
                dots.append((x + (px - x) * f, y + (py - y) * f))
            # The route from there on, at fixed distances from its start
            walked = sum(lengths[:best_i]) + lengths[best_i] * best_t
            along = math.ceil(walked / spacing) * spacing
            i, start = best_i, sum(lengths[:best_i])
            while i < len(lengths):
                if along > start + lengths[i]:
                    start += lengths[i]
                    i += 1
                    continue
                f = (along - start) / lengths[i] if lengths[i] else 0.0
                (ax, ay), (bx, by) = points[i], points[i + 1]
                dx, dy = ax + (bx - ax) * f, ay + (by - ay) * f
                d2 = (dx - x) ** 2 + (dy - y) ** 2
                if d2 <= reach2:
                    dots.append((dx, dy))
                elif d2 > 9 * reach2:
                    break  # far off: the rest won't come back in range soon
                along += spacing
        return dots

    @classmethod
    def on_route(cls, x: float, y: float) -> bool:
        """Whether (x, y) is on the ECHO map's copied route (within `JOIN_SLACK`)."""
        return any(near_line(r, x, y) for r in cls.routes)

    @classmethod
    def joined(cls, planned: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
        """
        `planned` (your route over the graph) up to where it meets the ECHO map's copied route, then
        the copy on to the goal; None if they never meet. The copy is the game's own way, but from
        where the map was opened: walk away from it and only a straight line led back to it (World_P
        2026-10-29: copied 208 m from the player; the game re-plans from where you are). Worked out
        once per planned route and copy (~60 x 80 segment checks).
        """
        key = (planned, cls.version)
        if cls.join_key is not None and cls.join_key[0] is planned and cls.join_key[1] == cls.version:
            return cls.join_result
        result = None
        for copy in cls.routes:
            if len(copy) < 2:  # noqa: PLR2004
                continue
            for k, (x, y) in enumerate(planned):
                d, i, t = _nearest_on(copy, x, y)
                if d <= JOIN_SLACK**2:
                    (ax, ay), (bx, by) = copy[i], copy[i + 1]
                    result = [*planned[: k + 1], (ax + (bx - ax) * t, ay + (by - ay) * t), *copy[i + 1 :]]
                    break
            if result:
                break
        cls.join_key, cls.join_result = key, result
        return result

    @classmethod
    def objective_texture(cls) -> str | None:
        """The icon the ECHO map last gave the tracked mission's objectives (main / side / DLC)."""
        found = [i["texture"] for i in cls.icons if is_objective(i["texture"])]
        plain = [t for t in found if "_optnl" not in t and "_brdcrmb" not in t]
        return (plain or found or [None])[0]

    @classmethod
    def icons_near(cls, x: float, y: float, reach: float) -> list[dict[str, Any]]:
        r2 = reach * reach
        return [i for i in cls.icons if (i["x"] - x) ** 2 + (i["y"] - y) ** 2 <= r2]
