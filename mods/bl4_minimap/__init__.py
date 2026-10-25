"""
Minimap for Borderlands 4: a round minimap in the corner of the screen, drawn from the game's own
big-map art.

How it fits together:

- scene.py spawns this world's big-map model (the same one the ECHO menu's map shows) far below
  the world, where only scene captures can see it, and films it from straight above with an
  orthographic camera centred on the player. The model is the world at 1/100 scale, so the camera
  just follows the player's position divided by 100.
- widget.py shows that picture in a UMG widget: a circle with the player's arrow in the middle.
- Every frame we move the camera and take a new picture. By default the map turns with you (what
  you face is up); with "Rotate with view" off, north stays up and the arrow turns instead.
- A key (N by default) hides and shows it. Keys are read from the Windows keyboard state like our
  other mods, only while the game window is in front.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, KeybindOption, SliderOption, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

from . import breadcrumbs, fog, loot, mapdata, markers, objectives, scene, services, tiles
from .widget import DOT_TEXTURE, EDGE_LAYER, MARKER_LOOKS, OVERSCAN, Minimap

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

# Fires several times a frame (the bridge and Third Person use it too); we act once per frame
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyPostProcess"
ICON_SIZE = 36.0  # the map's icons, UI units at 100% icon size
ROUTE_DOT_SIZE = 14.0
ROUTE_DOTS_PER_RANGE = 12  # dots between the player and the minimap's edge
# Film a new picture a little before the view would reach the old one's edge (in minimap radii)
REFILM_MARGIN = 0.1
DEFAULT_NORTH_YAW = 270.0  # the compass's north (world -Y) if we can't read it
VERTICAL_GAP = 800.0  # world units (8 m) above/below you before a marker gets a chevron
ROUTE_END_SLACK = 3000.0  # world units: a route ending this close to a live objective still leads there
# The live objectives' icon when the ECHO map hasn't shown us which kind the tracked mission is
OBJECTIVE_TEXTURE = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/ico_main_mission.ico_main_mission"
TELEPORT_GRACE_S = 3.0  # a teleport notice counts this long even before movement input stops
# A shown cursor means a menu is up for this long; after that only while BL4 Mod Menu's page (it
# runs only while one of the game's menus shows, polling every 1 s) has polled within MENU_POLL_S
CURSOR_GRACE_S = 2.0
MENU_POLL_S = 1.5

# ctypes.windll, not PyDLL: these calls must let go of the GIL (GetAsyncKeyState can take ~100 ms,
# e.g. while the desktop is streamed). Keys go through BL4 Mod Menu's key thread when it's loaded.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def log(msg: str) -> None:
    logging.info(f"[Minimap] {msg}")


# region Options

toggle_key = KeybindOption("Show/hide key", "N", description="Hides or shows the minimap.")
size_option = SliderOption(
    "Size (px)", 240, 120, 480, 10, True, description="How wide the minimap is on screen.",
)  # fmt: skip
range_option = SliderOption(
    "Range (m)",
    120,
    30,
    600,
    10,
    True,
    description="How far the minimap reaches from you to its edge.",
)
icon_size_option = SliderOption(
    "Icon size (%)",
    100,
    50,
    250,
    10,
    True,
    description="How big your arrow and the markers (enemies, teammates, waypoint, ...) are.",
)
icons_option = BoolOption(
    "Map icons",
    True,
    description=(
        "Shows the ECHO map's icons (fast travel, vending machines, missions, collectibles, ...)."
        " The minimap learns them from the ECHO map: open it once on each world; it updates each"
        " time you open it."
    ),
)
route_option = BoolOption(
    "Route line",
    True,
    description=(
        "Shows the ECHO map's route to your waypoint or tracked mission as a dotted line. It's"
        " taken from the ECHO map when you open it, so open the map after picking a new waypoint."
    ),
)
loot_option = BoolOption(
    "Legendary drops",
    True,
    description="Marks legendary and pearlescent items lying on the ground (within 300 m).",
)
vertical_option = BoolOption(
    "Above/below arrows",
    True,
    description="Like the compass: a small arrow on markers that are well above or below you.",
)
square_option = BoolOption(
    "Square minimap",
    False,
    description="A square minimap instead of a round one.",
)
rotate_option = BoolOption(
    "Rotate with view",
    True,
    description="On: the map turns so the way you face is up. Off: north stays up and the arrow turns.",
)
north_option = BoolOption(
    "North marker",
    True,
    description="An N on the minimap's edge that shows which way north is, so you can tell where you're facing.",
)
rate_option = SliderOption(
    "Marker updates per second",
    30,
    10,
    120,
    5,
    True,
    description=(
        "How often enemies and other markers are looked up (the map itself moves and turns every"
        " frame). Lower is a little lighter on your frame rate."
    ),
)
fog_option = BoolOption(
    "Fog of war",
    True,
    description=(
        "Hides the parts of the map you haven't explored, like the ECHO map. The minimap learns"
        " your fog from the ECHO map: open the map once on each world (it catches up each time"
        " you open it). Remembered per character."
    ),
)
fog_strength_option = SliderOption(
    "Fog strength (%)",
    70,
    0,
    100,
    5,
    True,
    description="How dark unexplored land is on the minimap. 100 hides it completely.",
)
margin_x_option = SliderOption("Distance from right edge (px)", 40, 0, 1500, 5, True)
margin_y_option = SliderOption("Distance from top edge (px)", 110, 0, 1000, 5, True)
saved_on_option = BoolOption("Minimap on", True, is_hidden=True)

# endregion
# region Keys

_NAMED_KEYS = {
    "Tab": 0x09, "CapsLock": 0x14, "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "Tilde": 0xC0, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
    "Semicolon": 0xBA, "LeftBracket": 0xDB, "RightBracket": 0xDD, "Backslash": 0xDC, "Apostrophe": 0xDE,
}  # fmt: skip
_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]


def virtual_key(name: str | None) -> int | None:
    """Windows key code for an Unreal key name."""
    if not name:
        return None
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in _DIGITS:
        return ord("0") + _DIGITS.index(name)
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:  # noqa: PLR2004
        return 0x70 + int(name[1:]) - 1
    if name.startswith("NumPad") and name[6:] in _DIGITS:
        return 0x60 + _DIGITS.index(name[6:])
    return _NAMED_KEYS.get(name)


def _game_window_focused() -> bool:
    hwnd = _user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _async_down(vk: int) -> bool:
    """
    Whether a key is held while the game is in front. Read by BL4 Mod Menu's key thread when it's
    loaded: GetAsyncKeyState can take ~100 ms on the game thread (a streamed desktop, 2026-10-24).
    """
    keys = sys.modules.get("bl4_mod_menu.keys")
    if keys is not None and hasattr(keys, "_Keys"):
        return keys.vk_down(vk)
    return _game_window_focused() and bool(_user32.GetAsyncKeyState(vk) & 0x8000)


class Keys:
    was_down: dict[int, bool] = {}  # noqa: RUF012

    @classmethod
    def pressed(cls, option: KeybindOption) -> bool:
        """True once per press of the option's key."""
        vk = virtual_key(option.value)
        if vk is None:
            return False
        down = _async_down(vk)
        was = cls.was_down.get(vk, True)  # a key held when we start doesn't count
        cls.was_down[vk] = down
        return down and not was


# endregion
# region Frame


class State:
    last_frame = -1  # engine frame we last updated in
    view_key: tuple[Any, ...] = ()  # the view the icons were last placed for
    system: Any = None  # KismetSystemLibrary's default object
    last_update = 0.0  # the last full update (markers, fog, ...)
    markers: list[markers.Marker] = []  # noqa: RUF012  # from the last full update
    icons: list[dict[str, Any]] = []  # noqa: RUF012  # the map's icons near the player (last full update)
    route: list[tuple[float, float]] = []  # noqa: RUF012  # route dots near the player (last full update)
    error_logged = False
    north_yaw: float | None = None
    statics: Any = None  # GameplayStatics' default object, looked up once
    layout: Any = None  # WidgetLayoutLibrary's
    teleport_since = 0.0  # when the game said a teleport (fast travel portal) starts; 0: none
    cursor_since = 0.0  # when the mouse cursor came up; 0: it's hidden
    world = WeakPointer()  # the level of the last full update


def _north_yaw(pc: UObject) -> float:
    """
    The world direction the game's compass calls north, as a yaw. Its NorthVector reads (0, 1), but
    the compass on screen puts north at yaw ~270 (world -Y): checked against the HUD compass's
    letters at three headings (Cowbell_P, 2026-10-24). So north is the vector's opposite.
    """
    for comp in unrealsdk.find_all("OakUIDataCollector_Compass", exact=True):
        if comp.Outer is pc:
            north = comp.NorthVector
            if north.X or north.Y:
                return math.degrees(math.atan2(-north.Y, -north.X)) % 360
    return DEFAULT_NORTH_YAW


def _ui_scale(pc: UObject) -> float:
    """The viewport's UI (DPI) scale: screen pixels per UI unit."""
    if State.layout is None:
        State.layout = unrealsdk.find_class("WidgetLayoutLibrary").ClassDefaultObject
    scale = float(State.layout.GetViewportScale(pc))
    return scale if scale > 0 else 1.0


def _hud_hidden(pc: UObject) -> bool:
    """
    Cutscenes (a cinematic mode is active), the HUD switched off, and full-screen menus. In co-op
    the ECHO menu doesn't pause the game and drew under our widget; the game stops rendering the
    world behind it instead (`bPlayerViewRenderingSuspended`, measured 2026-10-25).
    """
    if pc.bPlayerViewRenderingSuspended:
        return True
    ident = pc.ActiveCinematicModeIdent
    if ident.TokenCount > 0 or str(ident.InlineTokens.Name) != "None":
        return True
    hud = pc.MyHUD
    return hud is not None and not hud.bShowHUD


def _menu_up(pc: UObject) -> bool:
    """
    One of the game's menus is up, for menus that don't pause the game or hide the world (the
    pause menu in co-op, 2026-10-25). The cursor can stay on with no menu (after a cinematic), so
    as in Weapondex it only counts while BL4 Mod Menu's page polls, apart from a short grace at
    first so we hide at once instead of after the page's first poll.
    """
    if not pc.bShowMouseCursor:
        State.cursor_since = 0.0
        return False
    now = time.monotonic()
    if not State.cursor_since:
        State.cursor_since = now
    if now - State.cursor_since < CURSOR_GRACE_S:
        return True
    web = sys.modules.get("bl4_mod_menu.web")
    if web is None:
        return True  # no way to tell: trust the cursor
    return now - web.Link.last_poll < MENU_POLL_S


def _teleporting(pc: UObject, pawn: UObject) -> bool:
    """
    The fast travel / vault portal: from the game's first teleport notice (`on_teleport`) until
    movement comes back. In the portal itself the player's collision is off and movement input is
    ignored.
    """
    if not State.teleport_since and pawn.bActorEnableCollision:
        return False
    if pc.IsMoveInputIgnored():
        return True
    if time.monotonic() - State.teleport_since > TELEPORT_GRACE_S:
        State.teleport_since = 0.0  # over (or a notice that never led anywhere)
    return False


def _route(pc: UObject, world_name: str, loc: Any, radius: float,
           live: list[tuple[float, float, float]] | None) -> list[tuple[float, float]]:  # fmt: skip
    """
    The route dots near you. Planned live over the level's breadcrumb graph to your map pin, or else
    to the nearest live objective. The ECHO map's copy (unless it's stale) where the graph can't
    help: no graph, you're off it (the Speakeasy has no nodes: the map's route leads to its door),
    or the graph ends short of the goal.
    """
    if not route_option.value:
        return []
    pins = [m for m in State.markers if m.kind == "waypoint"]
    goal = None
    if pins:
        goal = (pins[0].x, pins[0].y, pins[0].z if pins[0].z is not None else loc.Z)
    elif live:
        goal = min(live, key=lambda p: (p[0] - loc.X) ** 2 + (p[1] - loc.Y) ** 2)
    me = (loc.X, loc.Y, loc.Z)
    planned = breadcrumbs.Breadcrumbs.update(pc, world_name, me, goal) if live is not None or pins else None
    if planned == []:
        return []  # nowhere to go
    spacing, reach = radius / ROUTE_DOTS_PER_RANGE, radius * 1.5
    copy_ok = bool(mapdata.MapData.routes) and not _route_stale(live)
    if planned and (breadcrumbs.Breadcrumbs.reaches_goal or not copy_ok):
        return mapdata.MapData.route_dots(loc.X, loc.Y, spacing, reach, [planned])
    return mapdata.MapData.route_dots(loc.X, loc.Y, spacing, reach) if copy_ok else []


def _route_stale(live: list[tuple[float, float, float]] | None) -> bool:
    """
    The ECHO map's route (copied when the map was last open) leads somewhere none of the live
    objectives are any more: the objective moved on since. It's current when it ends at one (or at
    your waypoint pin), or when the objective icons the map showed with it are where a live
    objective is (from inside the Speakeasy it ends at the door, far from the objective). A route
    to your own waypoint pin is never stale.
    """
    if not live:
        return False
    targets = [(x, y) for x, y, _ in live] + [(m.x, m.y) for m in State.markers if m.kind == "waypoint"]
    ends = [route[-1] for route in mapdata.MapData.routes if route]
    shown = [(i["x"], i["y"]) for i in mapdata.MapData.icons if mapdata.is_objective(i["texture"])]
    slack = ROUTE_END_SLACK**2
    return bool(ends) and not any(
        (ex - tx) ** 2 + (ey - ty) ** 2 <= slack for ex, ey in ends + shown for tx, ty in targets
    )


def _paused(pc: UObject) -> bool:
    if State.statics is None:
        State.statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    return bool(State.statics.IsGamePaused(pc))


def _frame(pc: UObject, full: bool) -> None:
    """Every frame: move and turn the picture and the markers. `full`: also the heavier work."""
    if _game_window_focused() and Keys.pressed(toggle_key):
        saved_on_option.value = not saved_on_option.value
        mod.save_settings()

    pawn = pc.Pawn
    world = ENGINE.GameViewport.World
    paused = _paused(pc) if pawn is not None else False
    if not saved_on_option.value or pawn is None or world is None or not scene.Scene.ready(pc, world, paused=paused):
        Minimap.show(pc, False)
        return
    # Before the pause check: the game pauses while the map is open, and that's when we copy its fog
    if full:
        mapdata.MapData.sync(pc, str(world.Name))
        if fog_option.value:
            fog.Fog.sync(pc, str(world.Name), fog_strength_option.value / 100, paused)
            scene.Scene.show_also(fog.Fog.actor())
        else:
            scene.Scene.show_also(None)
    if paused or _hud_hidden(pc) or _menu_up(pc) or _teleporting(pc, pawn):
        if paused:
            tiles.Tiles.paused_tick(pc)  # saves finished map tiles behind the menu (a GPU readback each)
        Minimap.show(pc, False)
        return

    loc = pawn.K2_GetActorLocation()
    yaw = float(pc.GetControlRotation().Yaw)
    if State.north_yaw is None:
        State.north_yaw = _north_yaw(pc)
    north = State.north_yaw
    map_yaw = yaw if rotate_option.value else north
    radius = range_option.value * 100
    picture = radius * scene.PICTURE_SCALE
    square = bool(square_option.value)
    view = radius * OVERSCAN  # world units from the centre to the side of the turned quad drawn (widget.py)

    # Where the player is in the last picture (north up): world units above / right of its centre
    shot = scene.Scene.shot
    up = right = 0.0
    c, s = math.cos(math.radians(north)), math.sin(math.radians(north))
    if shot is not None:
        dx, dy = loc.X - shot[0], loc.Y - shot[1]
        up, right = dx * c + dy * s, -dx * s + dy * c
    room = picture - view * (1 + REFILM_MARGIN)  # how far the view can slide before it runs off
    if scene.Scene.fresh or fog.Fog.changed:
        scene.Scene.invalidate()  # the cached pictures were filmed with the old fog
        fog.Fog.changed = False
    if (
        shot is None or shot[3] != picture or shot[2] != north or scene.Scene.needs_film()
        or abs(up) > room or abs(right) > room
    ):  # fmt: skip
        # The cached picture for the nearest grid point (one `room` apart, so the player is at most
        # half a room from its centre), filmed only if it isn't cached yet
        filmed = scene.Scene.use(pc, loc.X, loc.Y, north, picture, room)
        shot = scene.Scene.shot
        dx, dy = loc.X - shot[0], loc.Y - shot[1]
        up, right = dx * c + dy * s, -dx * s + dy * c
        filmed = filmed or scene.Scene.settle()
    else:
        filmed = scene.Scene.settle()
    # Map tiles (tiles.py): load or film one around the player; swap in a picture drawn from them
    if scene.Scene.tick_tiles(pc, loc.X, loc.Y, north, picture, room, fog_on=bool(fog_option.value),
                              strength=fog_strength_option.value / 100, paused=False):  # fmt: skip
        shot = scene.Scene.shot
        dx, dy = loc.X - shot[0], loc.Y - shot[1]
        up, right = dx * c + dy * s, -dx * s + dy * c
        filmed = True
    turn = north - yaw if rotate_option.value else 0.0
    # Sizes are in screen pixels; UMG lays out in UI units (pixels / the viewport's UI scale)
    ui = _ui_scale(pc)
    margin = (float(margin_x_option.value) / ui, float(margin_y_option.value) / ui)
    Minimap.show(pc, True, float(size_option.value) / ui, margin, yaw - map_yaw, icon_size_option.value / 100,
                 square=square)  # fmt: skip
    Minimap.set_view(0.5 + right / (2 * picture), 0.5 - up / (2 * picture), radius / (2 * picture), turn,
                     force=filmed or scene.Scene.settling())  # fmt: skip
    north_at = None
    if north_option.value:
        # A point far to the north, pulled onto the rim
        far = radius * 4
        n = math.radians(north)
        north_at = markers.project(loc.X + far * math.cos(n), loc.Y + far * math.sin(n), loc.X, loc.Y, map_yaw, radius,
                                   clamp=True, square=square)  # fmt: skip
    Minimap.set_north(north_at)
    if full:
        State.markers = markers.collect(pc, pawn, world, loot_on=bool(loot_option.value))
        data = mapdata.MapData
        new_world = world is not State.world()
        State.world = WeakPointer(world)
        live = objectives.Objectives
        live.update(pawn, paused=paused, new_world=new_world)
        # Objectives are markers, so they stay on the rim when out of range. Live ones replace the
        # ECHO map's copies (those only change when the map is opened); the copies are the fallback.
        icons = data.icons_near(loc.X, loc.Y, radius * 1.5) if icons_option.value else []
        icons = [i for i in icons if not mapdata.is_objective(i["texture"])]
        if not live.broken:
            texture = data.objective_texture() or OBJECTIVE_TEXTURE
            State.markers += [markers.Marker("objective", x, y, True, z, texture) for x, y, z in live.points]
        else:
            State.markers += [
                markers.Marker("objective", i["x"], i["y"], True, i.get("z"), i["texture"])
                for i in data.icons if mapdata.is_objective(i["texture"])
            ]  # fmt: skip
        State.icons = icons
        if icons_option.value:
            services.Services.scan(pc, str(world.Name), paused)
            State.icons = State.icons + services.Services.near(loc.X, loc.Y, radius * 1.5, State.icons)
        State.route = _route(pc, str(world.Name), loc, radius, None if live.broken else live.points)

    # Icons only need moving when the view moved (or the full update changed what's shown)
    view_key = (round(loc.X), round(loc.Y), round(map_yaw, 1), radius, square, Minimap.size, Minimap.icon_scale)
    if not full and view_key == State.view_key:
        return
    State.view_key = view_key

    def place(x: float, y: float, clamp: bool = False) -> tuple[float, float] | None:
        return markers.project(x, y, loc.X, loc.Y, map_yaw, radius, clamp=clamp, square=square)

    def vertical(z: float | None) -> int:
        """1 well above you, -1 well below, else 0 (the compass's chevrons)."""
        if z is None or not vertical_option.value:
            return 0
        dz = z - loc.Z
        return 1 if dz > VERTICAL_GAP else -1 if dz < -VERTICAL_GAP else 0

    color = mapdata.MapData.route_color
    route_items = []
    for x, y in State.route:
        where = place(x, y)
        if where is not None:
            route_items.append((DOT_TEXTURE, ROUTE_DOT_SIZE, color, where[0], where[1], 0))
    Minimap.set_layer("route", route_items)
    icon_items = []
    for icon in State.icons:
        where = place(icon["x"], icon["y"])
        if where is not None:
            icon_items.append((icon["texture"], ICON_SIZE, (1, 1, 1, 1), where[0], where[1], vertical(icon.get("z"))))
    Minimap.set_layer("icon", icon_items)
    marker_items, edge_items = [], []
    for marker in State.markers:
        path, base, tint = MARKER_LOOKS[marker.kind]
        where = place(marker.x, marker.y)
        items = marker_items
        if where is None and marker.clamp:
            # Out of range: on the ring itself, so it doesn't look close
            where, items = place(marker.x, marker.y, True), edge_items
        if where is not None:
            items.append((marker.texture or path, base, tint, where[0], where[1], vertical(marker.z)))
    Minimap.set_layer("marker", marker_items)
    Minimap.set_layer(EDGE_LAYER, edge_items)


@hook(TICK_FUNC, Type.PRE)
def on_frame(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    # The hook fires several times a frame; act once per engine frame
    if State.system is None:
        State.system = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    frame = int(State.system.GetFrameCount())
    if frame == State.last_frame:
        return
    State.last_frame = frame
    now = time.monotonic()
    full = now - State.last_update >= 1 / max(1, rate_option.value)
    if full:
        State.last_update = now
    try:
        pc = get_pc(possibly_loading=True)
        if pc is not None:
            _frame(pc, full)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if not State.error_logged:
            logging.error(f"[Minimap] {ex!r}")
            State.error_logged = True


# Sent to each player before and after a teleport (fast travel, vault portals); the portal
# shows between them
@hook("/Script/OakGame.OakPlayerController:ClientNotifyTeleporting", Type.PRE)
def on_teleport(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    State.teleport_since = time.monotonic()


# endregion


def _enable() -> None:
    State.last_update = 0.0
    State.last_frame = -1
    State.markers = []
    State.icons = []
    State.route = []
    State.error_logged = False
    State.north_yaw = None
    State.teleport_since = 0.0
    Keys.was_down.clear()


def _disable() -> None:
    Minimap.remove()
    fog.Fog.destroy()
    fog.Fog.key = None
    scene.Scene.destroy()
    loot.Loot.clear()
    objectives.Objectives.clear()
    breadcrumbs.Breadcrumbs.clear()


mod = build_mod(
    options=[
        toggle_key, size_option, range_option, icon_size_option, icons_option, route_option, loot_option,
        vertical_option, square_option, rotate_option, north_option, rate_option, fog_option, fog_strength_option,
        margin_x_option, margin_y_option, saved_on_option,
    ],
    hooks=[on_frame, on_teleport],
    on_enable=_enable,
    on_disable=_disable,
)
