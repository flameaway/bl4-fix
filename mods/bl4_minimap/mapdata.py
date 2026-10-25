"""
The ECHO map's icons and route line, copied for the minimap.

While the big map is open, its private world holds one actor per icon (`BP_MapPOI_StandardIcon_C`
/ `BP_MapPOI_StandardAreaIcon_C`, each with an `OakMapViewerPOIIconComponent` whose material
instance carries the icon texture and flags: `IsHidden`, `PinOpacity`, `IsBlip`) and the route to
your waypoint (`OakMapViewerWaypointPath`: a list of spline-mesh segments from your position to the
destination). Positions are in map space (world / 100, same origin). The game builds all of it
natively when the map opens, and none of it exists while the map is closed.

So once per opening (a moment after it opens, behind the paused menu) we read it all and keep it:
icons and the route are saved per character and map (JSON in the SDK settings folder) so
they're there next session. The minimap shows the icons in range and the route as
a dotted line, trimmed to the part still ahead of you. Open the map again to refresh them.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR
from unrealsdk.unreal import UObject

from . import scene

FOLDER: Path = SETTINGS_DIR / "minimap_icons"
SNAPSHOT_DELAY_S = 1.0  # after the map opens: let it finish placing its icons and route
ICON_CLASSES = (
    "/Game/GameData/Map/BP_MapPOI_StandardIcon.BP_MapPOI_StandardIcon_C",
    "/Game/GameData/Map/BP_MapPOI_StandardAreaIcon.BP_MapPOI_StandardAreaIcon_C",
)
MAP_TO_WORLD = 100.0
ROUTE_START_SLACK = 5000.0  # world units: a route starting this close to the player is the live one
DEFAULT_ROUTE_COLOR = (0.565, 0.804, 0.859, 1.0)
# The tracked mission's objective icons on the map: ico_main_mission, ico_side_mission_dlc, ...
OBJECTIVE_ICON = re.compile(r"^ico_(main|side)_mission(_optnl)?(_dlc)?$", re.IGNORECASE)


def is_objective(texture: str) -> bool:
    """A mission objective's icon (not a mission giver's: those end in _npc)."""
    return OBJECTIVE_ICON.search(texture.rsplit(".", 1)[-1]) is not None


def _character(pc: UObject) -> str | None:
    state = pc.PlayerState
    if state is None:
        return None
    guid = state.ActiveCharGuid
    parts = [guid.A, guid.B, guid.C, guid.D]
    return "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts) if any(parts) else None


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


class MapData:
    key: tuple[str, str] | None = None  # (character, world)
    icons: list[dict[str, Any]] = []  # noqa: RUF012  # {"x", "y", "texture", "blip", "pinned"} world units
    routes: list[list[tuple[float, float]]] = []  # noqa: RUF012  # world-unit polylines
    route_color = DEFAULT_ROUTE_COLOR
    opened_at = 0.0  # when we saw the map open (0: closed)
    snapped = False  # read this opening already
    version = 0  # bumped when icons or routes change

    @classmethod
    def _path(cls) -> Path:
        char, world = cls.key or ("", "")
        return FOLDER / f"{char}_{world}.json"

    @classmethod
    def _load(cls) -> None:
        cls.icons, cls.routes = [], []
        try:
            data = json.loads(cls._path().read_text(encoding="utf-8"))
            cls.icons = data.get("icons", [])
            cls.routes = [[tuple(p) for p in route] for route in data.get("routes", [])]
            cls.route_color = tuple(data.get("route_color", DEFAULT_ROUTE_COLOR))
        except (OSError, ValueError):
            pass
        cls.version += 1

    @classmethod
    def _save(cls) -> None:
        FOLDER.mkdir(parents=True, exist_ok=True)
        tmp = cls._path().with_suffix(".tmp")
        data = {"icons": cls.icons, "routes": cls.routes, "route_color": cls.route_color}
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cls._path())

    @classmethod
    def _snapshot(cls, pc: UObject | None = None) -> None:
        viewer = scene.Viewer.get()
        if viewer is None:
            return
        level = viewer.Outer
        poi_component = unrealsdk.find_class("OakMapViewerPOIIconComponent")
        icons: list[dict[str, Any]] = []
        for path in ICON_CLASSES:
            try:
                cls_obj = unrealsdk.find_object("BlueprintGeneratedClass", path)
            except ValueError:
                continue
            for actor in unrealsdk.find_all(cls_obj, exact=True):
                if actor.Outer is not level or actor.bHidden or "Default__" in actor._path_name():  # noqa: SLF001
                    continue
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
        routes: list[list[tuple[float, float]]] = []
        for path_actor in unrealsdk.find_all("OakMapViewerWaypointPath", exact=True):
            if path_actor.Outer is not level or path_actor.bHidden:
                continue
            points: list[tuple[float, float]] = []
            for segment in path_actor.PathSplines:
                mesh = segment.SplineMesh
                if mesh is None or not mesh.bVisible:
                    continue
                start, end = mesh.GetStartPosition(), mesh.GetEndPosition()
                if not points:
                    points.append((start.X * MAP_TO_WORLD, start.Y * MAP_TO_WORLD))
                points.append((end.X * MAP_TO_WORLD, end.Y * MAP_TO_WORLD))
                material = segment.Material
                for p in getattr(material, "VectorParameterValues", []):
                    if str(p.ParameterInfo.Name) == "LineColor":
                        c = p.ParameterValue
                        cls.route_color = (c.R, c.G, c.B, 1.0)
            if len(points) >= 2:  # noqa: PLR2004
                routes.append(points)
        # The live route starts where the player stands; paths left from earlier openings start
        # elsewhere (keeping those drew two routes)
        pawn = pc.Pawn if pc is not None else None
        if pawn is not None and routes:
            loc = pawn.K2_GetActorLocation()
            live = [r for r in routes if (r[0][0] - loc.X) ** 2 + (r[0][1] - loc.Y) ** 2 <= ROUTE_START_SLACK**2]
            routes = live or routes
        if icons:
            cls.icons = icons
        cls.routes = routes
        cls._save()
        cls.version += 1
        scene.log(f"map data: {len(icons)} icons, {len(routes)} route(s)")

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
            cls.opened_at = 0.0
            cls.snapped = False
            return
        if cls.opened_at == 0.0:
            cls.opened_at = now
        if not cls.snapped and now - cls.opened_at >= SNAPSHOT_DELAY_S:
            cls.snapped = True
            cls._snapshot(pc)

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
            lengths = [math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:])]
            # The point of the route nearest the player: the route goes on from there
            best, best_i, best_t = math.inf, 0, 0.0
            for i, length in enumerate(lengths):
                (ax, ay), (bx, by) = points[i], points[i + 1]
                t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / length**2))
                d = (ax + (bx - ax) * t - x) ** 2 + (ay + (by - ay) * t - y) ** 2
                if d < best:
                    best, best_i, best_t = d, i, t
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
    def objective_texture(cls) -> str | None:
        """The icon the ECHO map last gave the tracked mission's objectives (main / side / DLC)."""
        found = [i["texture"] for i in cls.icons if is_objective(i["texture"])]
        plain = [t for t in found if "_optnl" not in t]
        return (plain or found or [None])[0]

    @classmethod
    def icons_near(cls, x: float, y: float, reach: float) -> list[dict[str, Any]]:
        r2 = reach * reach
        return [i for i in cls.icons if (i["x"] - x) ** 2 + (i["y"] - y) ** 2 <= r2]
