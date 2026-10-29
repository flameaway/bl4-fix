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
import re
import sys
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, KeybindOption, SliderOption, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

from . import breadcrumbs, fog, health, loot, mapdata, markers, objectives, scene, services, tiles
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
GIVER_SLACK = 1000.0  # world units: a live mission giver this close to one on the ECHO map is that one
POSTER_SLACK = 500.0  # contract posters (givers too, to the game) sit 0.5-1.3 m from their bounty board
# A live mission giver's icon when the ECHO map hasn't shown one there yet
GIVER_TEXTURE = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/ico_side_mission_npc.ico_side_mission_npc"
# The live objectives' icon when the ECHO map hasn't shown us which kind the tracked mission is
OBJECTIVE_TEXTURE = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/ico_main_mission.ico_main_mission"
SIDE_OBJECTIVE_TEXTURE = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/ico_side_mission.ico_side_mission"
_ICO = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/"
_COWBELL_ICO = "/Game/DLC/Cowbell/uiresources/discovery_icons/"
_EVENT, _ACTIVITY = "ico_world_events", "ico_activity_waypoint"
_RIFT_BOSS = "/Game/DLC/Cowbell/uiresources/discovery_icons/ico_rift_boss.ico_rift_boss"
_DIGI_RIFT = "/Game/DLC/Harmonica/uiresources/discovery_icons/ico_dlc_rift_boss_digispace.ico_dlc_rift_boss_digispace"
# A live objective's mission type (objectives.MISSION_TYPES, lowercase) -> its icon. Main story
# missions have no type of their own (none in the NCS): they keep the main/side rule.
MISSION_TYPE_ICONS = {
    "contract": _ICO + "ico_contract_mission.ico_contract_mission",
    "sidemission": SIDE_OBJECTIVE_TEXTURE,
    "micromission": SIDE_OBJECTIVE_TEXTURE,
    "dlc_sidemission": _COWBELL_ICO + "ico_side_mission_dlc.ico_side_mission_dlc",
    "dlc_micromission": _COWBELL_ICO + "ico_side_mission_dlc.ico_side_mission_dlc",
    "dlc_mainstory": _COWBELL_ICO + "ico_main_mission_dlc.ico_main_mission_dlc",
    "factionmission": _ICO + "ico_faction_mission.ico_faction_mission",
    "colosseum": _ICO + "ico_world_event_colosseum.ico_world_event_colosseum",
    "airship": _ICO + "ico_world_event_airship.ico_world_event_airship",
    "meteor": _ICO + "ico_world_event_meteors.ico_world_event_meteors",
    "riftboss": _RIFT_BOSS,
    "digispaceboss": _DIGI_RIFT,
    **{t: f"{_ICO}{_EVENT}.{_EVENT}" for t in ("dynamicevent", "event", "takedown", "takedown_hardcore",
                                                "takedown_timer", "raid")},
    **{t: f"{_ICO}{_ACTIVITY}.{_ACTIVITY}" for t in (
        "safehouse", "crawler", "silo", "drillsite", "orderbunker", "mine", "volcano", "pearlgenerator",
        "tikibar", "fuelsiphon", "tedioregrotto", "tediorelockdown", "dahlbunker")},
}  # fmt: skip
# Mission types of rift events (a world boss "Rift Champion" is an Event, like the other world events)
RIFT_TYPES = frozenset({"riftboss", "digispaceboss", "event"})
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
        " time you open it. Mission objectives and mission givers update live."
    ),
)
route_option = BoolOption(
    "Route line",
    True,
    description=(
        "Shows the route to your waypoint or tracked mission as a dotted line, like the ECHO map's."
        " It follows a new waypoint or objective at once, and takes the ECHO map's exact way once you"
        " open the map; inside buildings and caves it's taken from the ECHO map when you open it."
    ),
)
contracts_option = BoolOption(
    "Always show contracts",
    False,
    description=(
        "Off: only the contract (bounty) you're tracking shows on the minimap. On: every contract"
        " you've accepted shows, like on the ECHO map."
    ),
)
rifts_option = BoolOption(
    "Always show rift events",
    False,
    description=(
        "Shows rift events (Rift Champions, rift bosses, Digi-Rifts) and the other world events"
        " whenever there are any, even when you aren't tracking them, on the rim when they're out of"
        " range. A tracked one always shows."
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
        " your fog from the ECHO map: open the map once on each world, then it follows your"
        " exploring as you go. Remembered per character."
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
    giver_key: tuple[Any, ...] = ()  # (live givers, map data version) `givers` was made for
    givers: list[dict[str, Any]] = []  # noqa: RUF012  # live mission givers as map icons
    covered: set[str] = set()  # noqa: RUF012  # ECHO-map giver textures the live list stands for
    route_color = mapdata.DEFAULT_ROUTE_COLOR  # the colour `route` is drawn in (`_route` picks it)
    events_version = -1  # the map data version `events` was made from
    events: list[dict[str, Any]] = []  # noqa: RUF012  # the ECHO map's rift / world event icons


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
    The route dots near you, to your map pin, else to the nearest live objective (where the ECHO map
    routes). In order:
    - the ECHO map's own route (copied while it was open), when it ends at that goal: it's the game's
      own way there. Our plan over the same graph can differ: the game doesn't take the shortest
      way (World_P, 2026-10-28: its route was 2413 m where the shortest over the graph is 1744 m,
      from the same node), and we don't know its costs. It starts where the map was opened, so
      once you're off it, the way from you to it is planned over the graph (`MapData.joined`);
    - planned live over the level's breadcrumb graph, when it gets there (a new goal, until the map
      is opened again);
    - the ECHO map's route when it leads to that same goal another way: the graph doesn't cover
      interiors (the Speakeasy has no nodes: the map's route leads to its door);
    - the planned route as far as the graph goes (then straight to the goal).
    Vaults have no graph and the ECHO map draws no route there either.
    """
    if not route_option.value:
        return []
    data = mapdata.MapData
    spacing, reach = radius / ROUTE_DOTS_PER_RANGE, radius * 1.5
    pins = [m for m in State.markers if m.kind == "waypoint"]
    # The map colours its line by where it goes (a bounty's was orange, the pin's white-blue,
    # 2026-10-29). The map's own line keeps its colour; a line we plan gets the map's last colour for
    # that kind of goal
    planned_color = data.pin_color if pins else data.mission_color
    State.route_color = data.route_color
    goal = None
    if pins:
        goal = (pins[0].x, pins[0].y, pins[0].z if pins[0].z is not None else loc.Z)
    elif live:
        goal = _echo_goal(live) or min(live, key=lambda p: (p[0] - loc.X) ** 2 + (p[1] - loc.Y) ** 2)
    if goal is None:
        # Nowhere to go; with live objectives broken the map's copy is all we know
        return data.route_dots(loc.X, loc.Y, spacing, reach) if live is None else []
    me = (loc.X, loc.Y, loc.Z)
    graph = breadcrumbs.Breadcrumbs
    to_goal = [r for r in data.routes if r and (r[-1][0] - goal[0]) ** 2 + (r[-1][1] - goal[1]) ** 2 <= ROUTE_END_SLACK**2]
    if to_goal:
        if data.on_route(loc.X, loc.Y):
            return data.route_dots(loc.X, loc.Y, spacing, reach)
        # Off the game's line: the way back onto it ahead of you (a small search, redone as you go)
        way = graph.reconnect(me, to_goal[0])
        if way:
            return data.route_dots(loc.X, loc.Y, spacing, reach, [way])
        # Too far from it for that: a whole plan from you, joined to the game's line where they meet
        planned = graph.update(pc, world_name, me, goal)
        joined = data.joined(planned) if planned else None
        if joined:
            return data.route_dots(loc.X, loc.Y, spacing, reach, [joined])
        if planned and graph.reaches_goal:
            State.route_color = planned_color
            return data.route_dots(loc.X, loc.Y, spacing, reach, [planned])
        return data.route_dots(loc.X, loc.Y, spacing, reach)
    planned = graph.update(pc, world_name, me, goal)
    if planned and graph.reaches_goal:
        State.route_color = planned_color
        # Stepped off it: back onto it ahead (a whole new plan only once you're 40 m off)
        way = graph.reconnect(me, planned) if not mapdata.near_line(planned, loc.X, loc.Y) else None
        return data.route_dots(loc.X, loc.Y, spacing, reach, [way or planned])
    if data.routes and _copy_leads_to(goal, pinned=bool(pins), live=live):
        return data.route_dots(loc.X, loc.Y, spacing, reach)
    State.route_color = planned_color
    return data.route_dots(loc.X, loc.Y, spacing, reach, [planned]) if planned else []


def _echo_goal(live: list[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    """
    The live objective the ECHO map's route (copied without a pin) ends at, if any: the one the game
    routes to. A bounty has two live points, the bounty itself (it walks around) and its spawner;
    the map routed to the spawner, and "the nearest one" flipped between them and shrank the line
    to nothing once the bounty was next to you (2026-10-28).
    """
    data = mapdata.MapData
    if data.route_pin is not None or not data.routes:
        return None
    best, best_d = None, ROUTE_END_SLACK**2
    for route in data.routes:
        if not route:
            continue
        end = route[-1]
        for p in live:
            d = (p[0] - end[0]) ** 2 + (p[1] - end[1]) ** 2
            if d <= best_d:
                best, best_d = p, d
    return best


def _copy_leads_to(goal: tuple[float, float, float], *, pinned: bool,
                   live: list[tuple[float, float, float]] | None) -> bool:  # fmt: skip
    """
    Whether the ECHO map's route (copied when the map was last open) still leads to `goal`. It does
    when it ends there. Otherwise only if it was copied for the same goal: from an interior it ends
    at the exit door. For your pin: the map was open with this pin set. For objectives: no pin was
    set then, and the objective icons the map showed with it are where a live objective is now
    (when the objective moves on, the old route is dropped at once instead of at the next opening).
    """
    data = mapdata.MapData
    slack = ROUTE_END_SLACK**2

    def near(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 <= slack

    ends = [route[-1] for route in data.routes if route]
    if any(near(end, goal) for end in ends):
        return True
    if pinned:
        return data.route_pin is not None and near(data.route_pin, goal)
    if data.route_pin is not None:
        return False  # copied on the way to a pin you've since removed
    if live is None:
        return True  # no live objectives to check it against
    shown = [(i["x"], i["y"]) for i in data.icons if mapdata.is_objective(i["texture"])]
    return any(near(a, b) for a in ends + shown for b in live)


def _live_objectives(live: type[objectives.Objectives]) -> tuple[list[tuple[float, float, float, str]],
                                                                    list[tuple[float, float, float]]]:  # fmt: skip
    """
    The live objective points to draw (x, y, z, mission type) and the tracked ones (where the route
    goes). The game lists the waypoints of missions you aren't tracking too (every contract you've
    accepted): those show only with "Always show contracts" / "Always show rift events"; other
    untracked kinds show as before. With nothing tracked, the route goes to what's shown.
    """
    n = len(live.points)
    types = live.types if len(live.types) == n else [""] * n
    untracked = live.untracked if len(live.untracked) == n else [False] * n
    shown, tracked = [], []
    for (x, y, z), kind, other in zip(live.points, types, untracked, strict=True):
        if not other:
            tracked.append((x, y, z))
        elif (kind == "contract" and not contracts_option.value) or (kind in RIFT_TYPES and not rifts_option.value):
            continue
        shown.append((x, y, z, kind))
    return shown, tracked or [(x, y, z) for x, y, z, _ in shown]


def _objective_texture(kind: int, mission_type: str = "") -> str:
    """
    A live objective's icon. Its mission type (read live: "contract", "sidemission", ...) decides
    when we know it. Otherwise: the ECHO map's own objective icon gets the variant right (_dlc,
    _optnl) but is from its last opening, maybe for another mission, and some snapshots have none
    (a World_P one had no mission icons at all: everything showed as main). So the live waypoint's
    kind picks main or side, and the map's icon is used only when it's the same kind.
    """
    if mission_type in MISSION_TYPE_ICONS:
        return MISSION_TYPE_ICONS[mission_type]
    shown = mapdata.MapData.objective_texture()
    if shown and not re.search(r"ico_(main|side)_mission", shown, re.IGNORECASE):
        shown = None  # (a bounty's or an activity's icon says nothing about main vs side)
    if kind not in (objectives.KIND_SIDE, objectives.KIND_MAIN):
        return shown or OBJECTIVE_TEXTURE
    side = kind == objectives.KIND_SIDE
    if shown and ("side_mission" in shown.lower()) == side:
        return shown
    return SIDE_OBJECTIVE_TEXTURE if side else OBJECTIVE_TEXTURE


def _givers(icons: list[dict[str, Any]], x: float, y: float, reach: float) -> list[dict[str, Any]]:
    """
    `icons` (the ECHO map's, near you) with its mission givers swapped for the live ones: the map's
    copy only changes when it's opened, so a mission you took kept its giver's icon and a new giver
    showed nowhere. The live list (objectives.py, kind 4) has every giver on the map plus the
    contract posters by bounty boards (left out: the board's icon stands for them). Each live giver
    takes the icon the map gave it; new ones the map's most common giver icon. Giver icons with no
    live match at all stay as the map drew them (a kind of giver the live list may not have).
    """
    data = mapdata.MapData
    live = objectives.Objectives.givers
    key = (tuple(live), data.version)
    if key != State.giver_key:
        State.giver_key = key
        saved = [i for i in data.icons if mapdata.is_giver(i["texture"])]
        boards = [(i["x"], i["y"]) for i in data.icons if "bounty_board" in i["texture"].lower()]
        side = [i["texture"] for i in saved if "side_mission_npc" in i["texture"].lower()]
        default = max(set(side), key=side.count) if side else GIVER_TEXTURE
        covered = {i["texture"] for i in saved if "side_mission_npc" in i["texture"].lower()}
        grid: dict[tuple[int, int], list[dict[str, Any]]] = {}  # cells GIVER_SLACK wide (World_P: ~90 x ~180)
        for i in saved:
            grid.setdefault((int(i["x"] // GIVER_SLACK), int(i["y"] // GIVER_SLACK)), []).append(i)
        givers = []
        for gx, gy, gz in live:
            if any((gx - bx) ** 2 + (gy - by) ** 2 <= POSTER_SLACK**2 for bx, by in boards):
                continue
            cx, cy = int(gx // GIVER_SLACK), int(gy // GIVER_SLACK)
            close = [i for dx in (-1, 0, 1) for dy in (-1, 0, 1) for i in grid.get((cx + dx, cy + dy), ())]
            match = min(close, key=lambda i: (i["x"] - gx) ** 2 + (i["y"] - gy) ** 2, default=None)
            texture = default
            if match is not None and (match["x"] - gx) ** 2 + (match["y"] - gy) ** 2 <= GIVER_SLACK**2:
                texture = match["texture"]
                covered.add(texture)
            givers.append({"x": gx, "y": gy, "z": gz, "texture": texture})
        State.givers, State.covered = givers, covered
    r2 = reach * reach
    kept = [i for i in icons if i["texture"] not in State.covered]
    return kept + [i for i in State.givers if (i["x"] - x) ** 2 + (i["y"] - y) ** 2 <= r2]


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
        shown, tracked = _live_objectives(live) if not live.broken else ([], [])
        icons = data.icons_near(loc.X, loc.Y, radius * 1.5) if icons_option.value else []
        rifts = bool(rifts_option.value)
        icons = [
            i for i in icons
            if not mapdata.is_objective(i["texture"]) and not mapdata.is_live(i["texture"])
            and not mapdata.is_rift(i["texture"])
        ]  # fmt: skip
        if rifts:
            # Rift / world events aren't in the game's live waypoint list (World_P 2026-10-29: two
            # ico_world_events on the map, only contracts listed), so the map's icons become markers
            # that stay on the rim like objectives (they showed only within range before). Not the
            # ones a live marker already stands for.
            if State.events_version != data.version:
                State.events_version = data.version
                State.events = [i for i in data.icons if mapdata.is_rift(i["texture"])]
            slack = ROUTE_END_SLACK**2
            State.markers += [
                markers.Marker("event", i["x"], i["y"], True, i.get("z"), i["texture"])
                for i in State.events
                if not any((i["x"] - x) ** 2 + (i["y"] - y) ** 2 <= slack for x, y, _, _ in shown)
            ]  # fmt: skip
        if icons_option.value and not live.broken and live.read_ok:
            icons = _givers(icons, loc.X, loc.Y, radius * 1.5)
        if not live.broken:
            State.markers += [
                markers.Marker("objective", x, y, True, z, _objective_texture(live.mission_kind, t))
                for x, y, z, t in shown
            ]  # fmt: skip
        else:
            State.markers += [
                markers.Marker("objective", i["x"], i["y"], True, i.get("z"), i["texture"])
                for i in data.icons if mapdata.is_objective(i["texture"])
            ]  # fmt: skip
        State.icons = icons
        if icons_option.value:
            services.Services.scan(pc, str(world.Name), paused)
            State.icons = State.icons + services.Services.near(loc.X, loc.Y, radius * 1.5, State.icons)
        graph = breadcrumbs.Breadcrumbs
        before = (graph.world, graph.loaded)
        State.route = _route(pc, str(world.Name), loc, radius, None if live.broken else tracked)
        if route_option.value and (graph.world, graph.loaded) == before:
            graph.preload(pc, str(world.Name))  # one part per update, not on top of what _route read

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

    color = State.route_color
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
        texture = marker.texture
        if texture and Minimap._texture(texture) is None:  # noqa: SLF001  # (a DLC's art not installed)
            texture = None
            if marker.kind == markers.FRIEND_CLASS:
                path, base, tint = MARKER_LOOKS[markers.FRIEND]
        where = place(marker.x, marker.y)
        items = marker_items
        if where is None and marker.clamp:
            # Out of range: on the ring itself, so it doesn't look close
            where, items = place(marker.x, marker.y, True), edge_items
        if where is not None:
            items.append((texture or path, base, tint, where[0], where[1], vertical(marker.z)))
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
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
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
    State.giver_key = ()
    State.givers = []
    State.covered = set()
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
        toggle_key, size_option, range_option, icon_size_option, icons_option, route_option, contracts_option,
        rifts_option, loot_option,
        vertical_option, square_option, rotate_option, north_option, rate_option, fog_option, fog_strength_option,
        margin_x_option, margin_y_option, saved_on_option,
    ],
    hooks=[on_frame, on_teleport],
    on_enable=_enable,
    on_disable=_disable,
)
