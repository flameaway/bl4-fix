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

from . import discovery, echoloc, fog, frametick, gamekeys, health, hudsway, loot, mapdata, mapwaypoints, markers, objectives, page, scene, services, spread, tiles, uilink
from .widget import DOT_TEXTURE, EDGE_LAYER, MARKER_LOOKS, OVERSCAN, RING_WIDTH, Minimap

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

# Fires several times a frame (the bridge and Third Person use it too); we act once per frame
# Map data, fog, objectives, the map's icons and the route: none of these move fast, so they update
# at this pace (5/s); enemies and other moving markers at the "Marker updates per second" option
SLOW_UPDATE_S = 0.2
ICON_SIZE = 36.0  # the map's icons, UI units at 100% icon size
ROUTE_DOT_SIZE = 14.0
ROUTE_DOTS_PER_RANGE = 12  # dots between the player and the minimap's edge
# Film a new picture a little before the view would reach the old one's edge (in minimap radii)
REFILM_MARGIN = 0.1
DEFAULT_NORTH_YAW = 270.0  # the compass's north (world -Y) if we can't read it
VERTICAL_GAP = 800.0  # world units (8 m) above/below you before a marker gets a chevron
ROUTE_END_SLACK = 3000.0  # world units: a route ending this close to a live objective still leads there
# The game's route (echoloc, on a worker thread: up to 60 ms a search) is asked for again: at once
# for a new goal (moved this far, world units), at most this often once you're off it, and this
# often anyway (mission progress opens and closes ways)
ROUTE_GOAL_MOVE = 500.0
ROUTE_OFF_S = 0.5
ROUTE_REPLAN_S = 2.0
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
# Missions: shown from the HUD's list (tracked / untracked, "Always show contracts"). The other
# types the ECHO map shows (activities: crawlers, ...; world events: airships, ...) are its icons.
MISSIONS = frozenset({"contract", "sidemission", "micromission", "dlc_sidemission", "dlc_micromission",
                           "dlc_mainstory", "factionmission"})  # fmt: skip
ACTIVITY_TYPES = frozenset(MISSION_TYPE_ICONS) - MISSIONS - RIFT_TYPES
TELEPORT_GRACE_S = 3.0  # a teleport notice counts this long even before movement input stops
# A shown cursor means a menu is up for this long; after that only while BL4 Mod Menu's page (it
# runs only while one of the game's menus shows, polling every 1 s) has polled within MENU_POLL_S
CURSOR_GRACE_S = 2.0
MENU_POLL_S = 1.5
# The hide checks that cost a call or a chain of reads (pause, cutscene, HUD off, portal) run at
# most this far apart; the cheap per-frame cues (cursor, full-screen menu, a teleport notice) run
# them at once when they change, so a menu still hides us in the frame it comes up
HIDE_CHECK_S = 0.1
# Hidden (a menu, the pause, the portal, the HUD off, loading): the whole check runs this often, not
# every frame (it cost ~0.1 ms a frame behind a menu: property reads at 15-20 us each in the frame
# tick, game 22832, 2026-10-30). Showing again after a menu closes waits up to this long.
HIDDEN_POLL_S = 0.05
UI_SCALE_S = 1.0  # the viewport's UI scale (a UFunction call) only changes when the window is resized
# tiles.py's loader: asked when its next action is due, at most every TILES_MIN_GAP_S, and at least
# every TILES_GAP_S (fog mask reads, blur checks, a composed picture to swap in)
TILES_MIN_GAP_S = 0.02
TILES_GAP_S = 0.1
TILES_IDLE_GAP_S = 0.25  # every tile around you loaded and sharp: a new one is wanted only after a long walk

# ctypes.windll, not PyDLL: these calls must let go of the GIL. Only the focus check uses them; keys
# come from the game's own input (gamekeys).
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def log(msg: str) -> None:
    logging.info(f"[Minimap] {msg}")


# region Options

toggle_key = KeybindOption("Show/hide key", "N", description="Hides or shows the minimap.")
zoom_in_key = KeybindOption(
    "Zoom in key",
    "Equals",
    description="Zooms the minimap in (shorter range); hold it to keep zooming. The keypad's + works too.",
)
zoom_out_key = KeybindOption(
    "Zoom out key",
    "Hyphen",
    description="Zooms the minimap out (longer range); hold it to keep zooming. The keypad's - works too.",
)
size_option = SliderOption(
    "Size (px)", 480, 120, 960, 10, True, description="How wide the minimap is on screen.",
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
    150,
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
        "Shows the route to your waypoint or tracked mission, the same way the ECHO map routes you"
        " (the game's own route, asked for live), and it follows a new waypoint or objective at once."
    ),
)
route_top_option = BoolOption(
    "Route line over icons",
    False,
    description=(
        "Draws the route line over the map's icons (vendors, chests and the like) instead of under"
        " them. Enemies, objectives and your arrow stay on top. In the HUD minimap only."
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
    True,
    description=(
        "Off: rift events (Rift Champions, rift bosses, Digi-Rifts) and the other world events show"
        " like any map icon, within range. On: they always show, on the rim when they're out of range."
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
spread_option = BoolOption(
    "Spread out stacked icons",
    False,
    description=(
        "Icons that would sit on top of each other are moved apart so none overlap, each with a thin"
        " line back to where it really is, like the ECHO map does when you hover a stack of icons."
        " Enemies and teammates stay where they are."
    ),
)
# Markers that spread with the map's icons (they stand still); enemies and teammates move about
SPREAD_KINDS = frozenset(("objective", "waypoint", "event", "legendary", "pearl"))
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
page_option = BoolOption(
    "Draw in the HUD (lighter)",
    True,  # (the default for new installs since 0.1.13: the user's pick; saved settings keep theirs)
    description=(
        "Draws the minimap in the game's own HUD instead of a widget of ours: the same map, icons and"
        " settings, but no camera filming the map as you move and nothing redrawn by the mod each"
        " frame. The map is put together from saved map tiles; new areas are filmed once, the first"
        " time you're there."
    ),
)


def _still_hud_changed(_: BoolOption, value: bool) -> None:
    # Settings load inside build_mod, before `mod` exists (issue #6: a saved "on" raised NameError
    # there, so the module never finished loading); _enable applies it then
    if State.enabled:
        hudsway.set_wanted(value)


still_hud_option = BoolOption(
    "Still HUD (no sway)",
    False,
    description=(
        "Stops the whole HUD (every element, the minimap too) zooming and shaking when you sprint,"
        " boost a vehicle, slide, glide, grapple or ground-slam."
    ),
    on_change=_still_hud_changed,
)
saved_on_option = BoolOption("Minimap on", True, is_hidden=True)

# endregion
# region Keys

_NAMED_KEYS = {
    "Tab": 0x09, "CapsLock": 0x14, "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "Tilde": 0xC0, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
    "Semicolon": 0xBA, "LeftBracket": 0xDB, "RightBracket": 0xDD, "Backslash": 0xDC, "Apostrophe": 0xDE,
    "Equals": 0xBB, "Hyphen": 0xBD, "Add": 0x6B, "Subtract": 0x6D,
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
    Whether a key is held, from the game's own input (gamekeys, shared by our mods; False while the
    game isn't in front). Until 2026-10-30 this read Windows (GetAsyncKeyState, through BL4 Mod
    Menu's key thread when it was loaded).
    """
    return gamekeys.vk_down(vk)

class Keys:
    was_down: dict[int, bool] = {}  # noqa: RUF012

    vk_for: tuple[Any, int | None] = (None, None)  # (key name, its key code): parsed once per name

    @classmethod
    def pressed(cls, option: KeybindOption) -> bool:
        """True once per press of the option's key."""
        name = option.value
        if cls.vk_for[0] != name:
            cls.vk_for = (name, virtual_key(name))
        vk = cls.vk_for[1]
        if vk is None:
            return False
        down = _async_down(vk)
        was = cls.was_down.get(vk, True)  # a key held when we start doesn't count
        cls.was_down[vk] = down
        return down and not was


# Zoom keys: the range moves at ZOOM_RATE from the moment a key goes down until it's let go (a tap
# too: no jump on the press). While zooming the option holds the exact range, not whole metres, so
# every frame moves by the same amount; letting go rounds it and saves.
ZOOM_RATE = 300.0  # metres of range a second while held
_VK_ADD, _VK_SUBTRACT = 0x6B, 0x6D  # the keypad's + and -: zoom too, whatever the options say


class Zoom:
    direction = 0  # -1 zooming in, 1 out, 0 neither key held
    last = 0.0  # the previous tick

    vk_for: dict[str, tuple[Any, int | None]] = {}  # noqa: RUF012  # option name: (key name, its key code)

    @classmethod
    def _held(cls, option: KeybindOption, keypad: int) -> bool:
        name = option.value
        cached = cls.vk_for.get(option.identifier)
        if cached is None or cached[0] != name:
            cached = cls.vk_for[option.identifier] = (name, virtual_key(name))
        vk = cached[1]
        return (vk is not None and _async_down(vk)) or _async_down(keypad)

    @classmethod
    def tick(cls, now: float) -> bool:
        """Zooms while a zoom key is held; True when the range changed."""
        zoom_in = cls._held(zoom_in_key, _VK_ADD)
        zoom_out = cls._held(zoom_out_key, _VK_SUBTRACT)
        direction = 0 if zoom_in == zoom_out else (-1 if zoom_in else 1)
        elapsed = min(now - cls.last, 0.1)  # (a long hitch doesn't jump the whole way)
        cls.last = now
        if direction != cls.direction:
            was, cls.direction = cls.direction, direction
            if was and not direction:  # let go: whole metres again, saved once
                range_option.value = round(range_option.value)
                mod.save_settings()
                return True
            return False  # (just pressed: it moves from the next frame, by that frame's time)
        if not direction:
            return False
        value = min(range_option.max_value, max(range_option.min_value, range_option.value + direction * ZOOM_RATE * elapsed))
        if value == range_option.value:
            return False
        range_option.value = value
        return True


# endregion
# region Frame


class State:
    view_key: tuple[Any, ...] = ()  # the view the icons were last placed for
    last_update = 0.0  # the last full update (moving markers)
    last_slow = 0.0  # the last slow update (map data, fog, objectives, icons, route)
    collected: list[markers.Marker] = []  # noqa: RUF012  # markers.collect's, from the last full update
    extra: list[markers.Marker] = []  # noqa: RUF012  # objectives and events, from the last slow update
    ui = 1.0  # the viewport's UI scale (slow update)
    markers: list[markers.Marker] = []  # noqa: RUF012  # from the last full update
    icons: list[dict[str, Any]] = []  # noqa: RUF012  # the map's icons near the player (last full update)
    route: list[tuple[float, float]] = []  # noqa: RUF012  # route dots near the player (last full update)
    route_path: list[tuple[float, float]] = []  # noqa: RUF012  # the game's route to route_goal (_game_route)
    route_goal: tuple[float, float, float] | None = None
    route_at = 0.0  # when it was asked for
    route_ticket = 0  # the request it's the answer to (echoloc.Echoloc.ask)
    error_logged = False
    enabled = False  # between _enable and _disable
    north_yaw: float | None = None
    statics: Any = None  # GameplayStatics' default object, looked up once
    layout: Any = None  # WidgetLayoutLibrary's
    turn_ref: tuple[float, float, float] | None = None  # (x, y, radius): the route dots' fixed frame
    viewport = WeakPointer()  # ENGINE.GameViewport (through mods_base's ENGINE that's ~6 us a frame)
    pc = WeakPointer()  # get_pc's answer (on_frame)
    name_world = WeakPointer()  # the level `world_name` is the name of
    world_name = ""
    ui_next = 0.0  # when the UI scale is read again (UI_SCALE_S)
    shown_content: tuple[Any, ...] = ()  # (markers, icons, route, route colour) last placed
    tiles_next = 0.0  # when tick_tiles is due again (TILES_GAP_S / the tile loader's next action)
    fog_next = 0.0  # when the live fog grid is read again (FOG_S)
    fog_drawn: tuple[Any, ...] | None = None  # widget mode: (grid, strength, square) its fog texture shows
    teleport_since = 0.0  # when the game said a teleport (fast travel portal) starts; 0: none
    cursor_since = 0.0  # when the mouse cursor came up; 0: it's hidden
    hide_next = 0.0  # when the hide checks are due again (HIDE_CHECK_S)
    hide_cue: tuple[Any, ...] = ()  # (cursor, full-screen menu, teleport notice) at the last check
    paused = False  # the last check's verdicts
    hidden = False
    world = WeakPointer()  # the level of the last full update
    giver_key: tuple[Any, ...] = ()  # (live givers, map data version) `givers` was made for
    givers: list[dict[str, Any]] = []  # noqa: RUF012  # live mission givers as map icons
    covered: set[str] = set()  # noqa: RUF012  # ECHO-map giver textures the live list stands for
    route_color = mapdata.DEFAULT_ROUTE_COLOR  # the colour `route` is drawn in (`_route` picks it)
    # (route list, (turn_ref, north), each dot's place in the turned layer's fixed frame): those
    # places change only with the route (5/s at most) or the frame, not each frame you move or turn
    route_cache: tuple[Any, Any, list[tuple[float, float]]] = (None, None, [])  # noqa: RUF012
    hidden_until = 0.0  # hidden: on_frame skips the frames until then (HIDDEN_POLL_S)
    events_version = -1  # the map data version `events` was made from
    events: list[dict[str, Any]] = []  # noqa: RUF012  # rift / world events: live (mapwaypoints.py), or the ECHO map's copies
    # Page mode (page.py): the minimap drawn in the HUD page
    page_on = False
    page_vis = False  # the last sample told the page to show it
    page_force = True  # the next frame takes the whole pass (a zoom step: the new range goes out)
    page_pawn = WeakPointer()  # the pawn the last whole pass sampled (_page_sample, the frames between)
    page_last: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # (x, y, z, yaw) last sent
    page_tiles_rev = -1  # tiles.Tiles.page_rev the page's tile list was made from
    # Widget mode, "Spread out stacked icons": the spots laid out, for which (range, half-size, icon
    # scale), and the groups that spread: (spot, world x, y, moved x, y, base size, edge) each
    spread_spots: list[tuple[float, float, float, str]] = []  # noqa: RUF012
    spread_zoom: tuple[float, ...] = ()
    spread_groups: list[list[tuple[Any, ...]]] = []  # noqa: RUF012


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
    if ident.TokenCount > 1 or (
        (ident.TokenCount > 0 or str(ident.InlineTokens.Name) != "None")
        and str(ident.InlineTokens.Name).lower() not in HUD_CINEMATIC_MODES
    ):  # fmt: skip
        return True
    hud = pc.MyHUD
    return hud is not None and not hud.bShowHUD


# Cinematic modes that keep the game's HUD up (cinematic_mode NCS: `bhidehud` false, not
# `bisfullscreencutscene`; children inherit). Downed (Fight For Your Life) is one: the minimap
# vanished every time the player went down (2026-11-01). Any other mode, or several at once, hides
# it as before. The UI state BASE_HUD is no test: it stayed up through a full-screen `Cutscene` too.
HUD_CINEMATIC_MODES = frozenset((
    "cinematicenterffyl", "cinematicfakedownstate", "cinematicinteract", "cinematicstructuredinteraction",
    "cinematicarjaybreakout", "cinematicarjaybreakout_mute", "elpiswakeup",
))  # fmt: skip


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


def _route(pc: UObject, world: UObject, loc: Any, radius: float,
           live: list[tuple[float, float, float]] | None) -> list[tuple[float, float]]:  # fmt: skip
    """
    The route dots near you, to your map pin, else to the nearest live objective (where the ECHO map
    routes): the game's own route (echoloc: the planner the ECHO map draws its line with), asked
    for from where you are. Where it finds none, the ECHO map's route copied while the map was open,
    when that still leads to the goal. Vaults have no route and the ECHO map draws none there either.
    Until 2026-11-05 the minimap planned its own shortest way over the game's breadcrumb graph, but
    the game doesn't take the shortest way (it weighs roads and more: World_P, 2026-10-28, 2413 m
    where the shortest was 1744 m), so a new goal showed a different line until the map was opened.
    """
    if not route_option.value:
        return []
    data = mapdata.MapData
    spacing, reach = radius / ROUTE_DOTS_PER_RANGE, radius * 1.5
    pins = [m for m in State.markers if m.kind == "waypoint"]
    State.route_color = data.route_color
    goal = None
    if pins:
        goal = (pins[0].x, pins[0].y, pins[0].z if pins[0].z is not None else loc.Z)
    elif live:
        goal = _echo_goal(live) or min(live, key=lambda p: (p[0] - loc.X) ** 2 + (p[1] - loc.Y) ** 2)
    if goal is None:
        # Nowhere to go; with live objectives broken the map's copy is all we know
        State.route_path, State.route_goal = [], None
        return data.route_dots(loc.X, loc.Y, spacing, reach) if live is None else []
    # The map colours its line by where it goes (a bounty's was orange, the pin's white-blue,
    # 2026-10-29): its copy's colour when the copy ends at this goal, else the map's last colour for
    # that kind of goal
    if not any(r and (r[-1][0] - goal[0]) ** 2 + (r[-1][1] - goal[1]) ** 2 <= ROUTE_END_SLACK**2 for r in data.routes):
        State.route_color = data.pin_color if pins else data.mission_color
    path = _game_route(pc, world, loc, goal)
    if path:
        return data.route_dots(loc.X, loc.Y, spacing, reach, [path])
    if data.routes and _copy_leads_to(goal, pinned=bool(pins), live=live):
        State.route_color = data.route_color
        return data.route_dots(loc.X, loc.Y, spacing, reach)
    return []


def _game_route(pc: UObject, world: UObject, loc: Any, goal: tuple[float, float, float]) -> list[tuple[float, float]]:
    """
    The game's route to `goal`: the worker's last answer for it, asked for again when the goal
    moves, you're off it, or it's old. For a new goal there's none until its answer comes (the next
    update or so): the ECHO map's copy shows meanwhile where it leads there (_route).
    """
    answer = echoloc.Echoloc.answer
    if answer is not None and answer[0] == State.route_ticket:
        echoloc.Echoloc.answer = None
        State.route_path = answer[1] or []
    now = time.monotonic()
    old = State.route_goal
    moved = old is None or (old[0] - goal[0]) ** 2 + (old[1] - goal[1]) ** 2 + (old[2] - goal[2]) ** 2 > ROUTE_GOAL_MOVE**2
    if moved:
        State.route_path = []
    off = bool(State.route_path) and not mapdata.near_line(State.route_path, loc.X, loc.Y)
    if moved or now - State.route_at >= (ROUTE_OFF_S if off or not State.route_path else ROUTE_REPLAN_S):
        rot = pc.ControlRotation
        State.route_ticket = echoloc.Echoloc.ask(
            world._get_address(), (loc.X, loc.Y, loc.Z), goal, (rot.Pitch, rot.Yaw, rot.Roll))  # fmt: skip
        State.route_goal, State.route_at = goal, now
    return State.route_path


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


def _world_name(world: UObject) -> str:
    """The level's name, read once per level (str(world.Name) is a property read and a conversion)."""
    if State.name_world() is not world:
        State.name_world, State.world_name = WeakPointer(world), str(world.Name)
    return State.world_name


class _Point:
    """A location as _route reads it (X, Y, Z), without a struct."""

    __slots__ = ("X", "Y", "Z")

    def __init__(self, x: float, y: float, z: float) -> None:
        self.X, self.Y, self.Z = x, y, z


def _paused(pc: UObject) -> bool:
    if State.statics is None:
        State.statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    return bool(State.statics.IsGamePaused(pc))


def _gather(pc: UObject, pawn: UObject, world: UObject, full: bool, slow: bool, paused: bool,
            lx: float, ly: float, lz: float, radius: float) -> None:  # fmt: skip
    """What the minimap shows (both modes): moving markers when `full`, the rest when `slow`."""
    if full:
        State.collected = markers.collect(pc, pawn, world, loot_on=bool(loot_option.value), me=(lx, ly, lz))
        State.markers = State.collected + State.extra
    if slow:
        extra: list[markers.Marker] = []
        data = mapdata.MapData
        new_world = world is not State.world()
        State.world = WeakPointer(world)
        live = objectives.Objectives
        live.update(pawn, paused=paused, new_world=new_world)
        # Objectives are markers, so they stay on the rim when out of range. Live ones replace the
        # ECHO map's copies (those only change when the map is opened); the copies are the fallback.
        shown, tracked = _live_objectives(live) if not live.broken else ([], [])
        icons = data.icons_near(lx, ly, radius * 1.5) if icons_option.value else []
        # The map's fixed icons (fast travel, echo logs, collectables, ...) are read live
        # (discovery.py): its saved copy kept an echo log blue after you picked it up. The saved
        # icons standing on a live entry are dropped; the rest (givers, vendors, ...) stay.
        places = discovery.Discovery
        if new_world:
            places.clear()  # (its cached positions are by address: another level's)
        if icons_option.value:
            places.update(lx, ly, radius * 1.5, paused=paused, new_world=new_world)
            if not places.broken and places.read_ok:
                icons = [i for i in icons if not places.covers(i)] + places.near(lx, ly, radius * 1.5)
            # Where you last died, live (the saved skull stayed where you died before the map last opened)
            death = discovery.LastDeath
            death.update(pc, _world_name(world))
            if death.read_ok:
                icons = [i for i in icons if i["texture"] != discovery.DEATH_TEXTURE]
                skull = death.icon
                if skull is not None and (skull["x"] - lx) ** 2 + (skull["y"] - ly) ** 2 <= (radius * 1.5) ** 2:
                    icons.append(skull)
        rifts = bool(rifts_option.value)
        slack = ROUTE_END_SLACK**2

        def doubled(i: dict[str, Any]) -> bool:
            """A rift / world event icon a live objective marker already stands for."""
            return any((i["x"] - x) ** 2 + (i["y"] - y) ** 2 <= slack for x, y, _, _ in shown)

        # Rift / world events (Rift Champions: ico_world_events) are read live, with the ECHO map's
        # own rules (mapwaypoints.py: the HUD's waypoint list has only the ones near you). The map's
        # saved copies are only the fallback: they stay until the map is opened again, long after
        # the event is over. Off, events are ordinary icons shown within range; on, markers below.
        waypoints = mapwaypoints.MapWaypoints
        waypoints.update(pawn, paused=paused, new_world=new_world)
        live_events = not waypoints.broken and waypoints.read_ok
        icons = [
            i for i in icons
            if not mapdata.is_objective(i["texture"]) and not mapdata.is_live(i["texture"])
            and not (mapdata.is_rift(i["texture"]) and (live_events or rifts or doubled(i)))
        ]  # fmt: skip
        if live_events:
            # The map's own picture for each type (mapwaypoints.py: the game's rule; a Cowbell_P Rift
            # Champion is a totem, not ico_rift_boss); the table only until the game has filled it in
            State.events_version = -1
            State.events = [
                {"x": x, "y": y, "z": z, "texture": waypoints.pictures.get(t) or MISSION_TYPE_ICONS[t]}
                for x, y, z, t in waypoints.points
                if t in MISSION_TYPE_ICONS and mapdata.is_rift(MISSION_TYPE_ICONS[t])
            ]  # fmt: skip
        elif State.events_version != data.version:
            State.events_version = data.version
            State.events = [i for i in data.icons if mapdata.is_rift(i["texture"])]
        if live_events and icons_option.value:
            # Activities and world events as the map shows them (the HUD's list has only those
            # near you), within range like its other icons
            r2 = (radius * 1.5) ** 2
            activities = [
                {"x": x, "y": y, "z": z, "texture": waypoints.pictures.get(t) or MISSION_TYPE_ICONS[t]}
                for x, y, z, t in waypoints.points
                if t in ACTIVITY_TYPES and (x - lx) ** 2 + (y - ly) ** 2 <= r2
            ]  # fmt: skip
            icons += [i for i in activities if not doubled(i)]
            if not rifts:
                icons += [i for i in State.events if (i["x"] - lx) ** 2 + (i["y"] - ly) ** 2 <= r2 and not doubled(i)]
        if rifts:
            # Markers that stay on the rim like objectives. Not the ones a live objective marker
            # already stands for.
            extra += [
                markers.Marker("event", i["x"], i["y"], True, i.get("z"), i["texture"])
                for i in State.events
                if not doubled(i)
            ]  # fmt: skip
        if icons_option.value and not live.broken and live.read_ok:
            icons = _givers(icons, lx, ly, radius * 1.5)
        if not live.broken:
            extra += [
                markers.Marker("objective", x, y, True, z, _objective_texture(live.mission_kind, t))
                for x, y, z, t in shown
            ]  # fmt: skip
        else:
            extra += [
                markers.Marker("objective", i["x"], i["y"], True, i.get("z"), i["texture"])
                for i in data.icons if mapdata.is_objective(i["texture"])
            ]  # fmt: skip
        State.extra = extra
        State.markers = State.collected + extra
        State.icons = icons
        if icons_option.value:
            services.Services.scan(pc, _world_name(world), paused)
            State.icons = State.icons + services.Services.near(lx, ly, radius * 1.5, State.icons)
        State.route = _route(pc, world, _Point(lx, ly, lz), radius, None if live.broken else tracked)


def _spread_places(update: bool, cx: float, cy: float, yaw: float, radius: float,
                   square: bool) -> dict[int, tuple[float, float]]:  # fmt: skip
    """
    Widget mode, "Spread out stacked icons": where the moved icons and markers go (-1..1 of the
    half-size; keyed by their place in State.icons, then among the spreading markers of
    State.markers), and their lines (Minimap.set_leaders). Laid out again (spread.layout) only when
    they or the zoom changed: looked at when an update brought new ones (`update`), not each frame
    you move or turn. Each frame only the groups that spread are projected (ui/minimap.js: spreadPlace).
    """
    half, scale = Minimap.size / 2, Minimap.icon_scale
    if half <= 0:
        return {}
    zoom = (radius, half, scale)
    if update or zoom != State.spread_zoom:
        foot = spread.FOOTPRINT / 2 * scale * radius / half  # world units per UI unit of base size
        spots = [(i["x"], i["y"], ICON_SIZE * foot, i["texture"]) for i in State.icons]
        spots += [(m.x, m.y, MARKER_LOOKS[m.kind][1] * foot, m.texture or m.kind)
                  for m in State.markers if m.kind in SPREAD_KINDS]  # fmt: skip
        if spots != State.spread_spots or zoom != State.spread_zoom:
            State.spread_spots, State.spread_zoom = spots, zoom
            offsets, groups = spread.layout(spots)
            State.spread_groups = []
            for group in groups:
                members = []
                for k in group:
                    x, y, r, _ = spots[k]
                    base = r / foot
                    edge = max(0.0, half - base * scale / 2 - RING_WIDTH) / half  # (how far out it fits)
                    members.append((k, x, y, x + offsets[k][0], y + offsets[k][1], base, edge))
                State.spread_groups.append(members)
    # markers.project, with the turn worked out once
    kc, ks = math.cos(math.radians(yaw)) / radius, math.sin(math.radians(yaw)) / radius
    moved: dict[int, tuple[float, float]] = {}
    lines = []
    for group in State.spread_groups:
        points, shown, out = [], [], False
        for k, x, y, mx, my, base, edge in group:
            dx, dy = x - cx, y - cy
            tu, tv = -dx * ks + dy * kc, -(dx * kc + dy * ks)
            if (max(abs(tu), abs(tv)) if square else tu * tu + tv * tv) > 1.0:
                continue  # (not shown)
            dx, dy = mx - cx, my - cy
            u, v = -dx * ks + dy * kc, -(dx * kc + dy * ks)
            out = out or (max(abs(u), abs(v)) > edge if square else u * u + v * v > edge * edge)
            points.append((u, v, edge))
            shown.append((k, tu, tv, base))
        if not points:
            continue
        tx, ty = spread.fit(points, square=square) if out else (0.0, 0.0)
        for (u, v, _), (k, tu, tv, base) in zip(points, shown, strict=True):
            if not out and u == tu and v == tv:
                continue  # (the one left on its spot: placed as usual, no line)
            moved[k] = (u + tx, v + ty)
            lines.append((u + tx, v + ty, tu, tv, base))
    Minimap.set_leaders(lines)
    return moved


def _frame(pc: UObject, full: bool, slow: bool) -> None:
    """
    Every frame: move and turn the picture and the markers. `full`: also look up the moving markers
    (enemies, teammates, loot); `slow`: the map data, fog, objectives, the map's icons and the route.
    """
    # (No focus check of our own: BL4 Mod Menu's key snapshot, and our fallback, read False while the
    # game isn't in front; it was 3 Win32 calls a frame, each handing the GIL away)
    if Keys.pressed(toggle_key):
        saved_on_option.value = not saved_on_option.value
        mod.save_settings()

    if not saved_on_option.value:  # switched off (N): before any read of the game
        State.hide_next = 0.0
        _hide_all(pc)
        return
    now = time.monotonic()
    pawn = pc.Pawn
    viewport = State.viewport()
    if viewport is None:
        viewport = ENGINE.GameViewport
        State.viewport = WeakPointer(viewport)
    world = viewport.World if viewport is not None else None
    if pawn is None or world is None:
        State.hide_next = 0.0
        State.hidden_until = now + HIDDEN_POLL_S
        _hide_all(pc)
        return
    # Cheap cues every frame; the pause check (a UFunction call) and the other hide checks only when
    # a cue changed or HIDE_CHECK_S passed (they cost ~25-60 us a frame when run every frame)
    suspended = bool(pc.bPlayerViewRenderingSuspended)  # full-screen menus (co-op ECHO menu too)
    cue = (bool(pc.bShowMouseCursor), suspended, State.teleport_since)
    # A check that's only due by time waits for a frame without a marker or slow update, but not
    # while hidden (nothing else runs then) nor for more than one more HIDE_CHECK_S: at 30 marker
    # updates a second nearly every frame is one, so a hide no cue ends (going down, a cutscene)
    # stayed until the next menu opened (2026-11-01, game 40268: the check 36 s overdue)
    due = now >= State.hide_next
    check = cue != State.hide_cue or (
        due and (State.hidden or not (full or slow) or now >= State.hide_next + HIDE_CHECK_S)
    )
    if check:
        State.paused = _paused(pc)
    paused = State.paused
    # The map model and its camera: looked at fully at the slow rate or when the level changed (a
    # level with no map leaves Scene.world empty, so it's asked every frame there, as before)
    if (slow or world is not scene.Scene.world()) and not scene.Scene.ready(pc, world, paused=paused):
        State.hide_next = 0.0
        State.hidden_until = now + HIDDEN_POLL_S
        _hide_all(pc)
        return
    # Before the pause check: the game pauses while the map is open, and that's when we read where
    # its fog goes
    if slow:
        mapdata.MapData.sync(pc, _world_name(world))
        if fog_option.value:
            fog.Fog.sync(pc, _world_name(world), paused)
    if mapdata.MapData.job is not None:
        mapdata.MapData.run()  # a read of the open ECHO map, a piece a frame (behind the paused menu)
    if check:
        State.hide_next, State.hide_cue = now + HIDE_CHECK_S, cue
        State.hidden = paused or _hud_hidden(pc) or _menu_up(pc) or _teleporting(pc, pawn)
    if State.hidden or suspended:
        if paused:
            tiles.Tiles.paused_tick(pc)  # saves finished map tiles behind the menu (a GPU readback each)
        State.hidden_until = now + HIDDEN_POLL_S
        _hide_all(pc)
        return

    lx, ly, lz = markers.location(pawn)  # (properties: no UFunction call a frame)
    # The property GetControlRotation returns (equal over 400 frames, game 25456): a property read
    # is ~4 us inside the frame tick, the UFunction call 14-25 us
    yaw = float(pc.ControlRotation.Yaw)
    if State.north_yaw is None:
        State.north_yaw = _north_yaw(pc)
    north = State.north_yaw
    map_yaw = yaw if rotate_option.value else north
    radius = range_option.value * 100
    picture = radius * scene.PICTURE_SCALE
    square = bool(square_option.value)
    view = radius * OVERSCAN  # world units from the centre to the side of the turned quad drawn (widget.py)
    if State.page_on:
        _page_frame(pc, pawn, world, full, slow, paused, now, (lx, ly, lz), yaw, north, radius)
        return

    # Where the player is in the last picture (north up): world units above / right of its centre
    shot = scene.Scene.shot
    up = right = 0.0
    c, s = math.cos(math.radians(north)), math.sin(math.radians(north))
    if shot is not None:
        dx, dy = lx - shot[0], ly - shot[1]
        up, right = dx * c + dy * s, -dx * s + dy * c
    room = picture - view * (1 + REFILM_MARGIN)  # how far the view can slide before it runs off
    if scene.Scene.fresh:
        scene.Scene.invalidate()  # (a new map)
    if (
        shot is None or shot[3] != picture or shot[2] != north or scene.Scene.needs_film(alive=slow)
        or abs(up) > room or abs(right) > room
    ):  # fmt: skip
        # The cached picture for the nearest grid point (one `room` apart, so the player is at most
        # half a room from its centre), filmed only if it isn't cached yet
        filmed = scene.Scene.use(pc, lx, ly, north, picture, room)
        shot = scene.Scene.shot
        dx, dy = lx - shot[0], ly - shot[1]
        up, right = dx * c + dy * s, -dx * s + dy * c
        filmed = filmed or scene.Scene.settle()
    else:
        filmed = scene.Scene.settle()
    # Map tiles (tiles.py): load or film one around the player; swap in a picture drawn from them.
    # The loader does at most one action per ACTION_GAP_S, so it's asked when that's due, else every
    # TILES_GAP_S for its other checks (it cost ~50-60 us every frame, game 25456)
    if now >= State.tiles_next or filmed:
        if scene.Scene.tick_tiles(pc, lx, ly, north, picture, room, paused=False):
            shot = scene.Scene.shot
            dx, dy = lx - shot[0], ly - shot[1]
            up, right = dx * c + dy * s, -dx * s + dy * c
            filmed = True
        if tiles.Tiles.idle:
            State.tiles_next = now + TILES_IDLE_GAP_S
        else:
            State.tiles_next = max(now + TILES_MIN_GAP_S, min(tiles.Tiles.next_action, now + TILES_GAP_S))
    turn = north - yaw if rotate_option.value else 0.0
    # Sizes are in screen pixels; UMG lays out in UI units (pixels / the viewport's UI scale)
    if (slow and now >= State.ui_next) or State.ui <= 0:
        State.ui, State.ui_next = _ui_scale(pc), now + UI_SCALE_S
    ui = State.ui
    margin = (float(margin_x_option.value) / ui, float(margin_y_option.value) / ui)
    Minimap.show(pc, True, float(size_option.value) / ui, margin, yaw - map_yaw, icon_size_option.value / 100,
                 square=square, pixel=1 / ui)  # fmt: skip
    if now >= State.fog_next:
        State.fog_next = now + FOG_S
        _widget_fog(pc, world, paused)  # (after show: a new widget has no fog texture yet)
    mu, mv = Minimap.snap(0.5 + right / (2 * picture), 0.5 - up / (2 * picture), radius / (2 * picture))
    Minimap.set_view(mu, mv, radius / (2 * picture), turn, force=filmed or scene.Scene.settling(), changed=filmed)
    # The point the map is drawn around: you, moved to the display pixel the map moved by. Icons,
    # markers, the route and the north badge are placed around it too, so they don't slide against
    # the map between its pixel steps
    cx, cy = lx, ly
    if shot is not None:
        s_right, s_up = (mu - 0.5) * 2 * picture, (0.5 - mv) * 2 * picture
        cx, cy = shot[0] + s_up * c - s_right * s, shot[1] + s_up * s + s_right * c
    north_at = None
    if north_option.value:
        # A point far to the north, pulled onto the rim
        far = radius * 4
        n = math.radians(north)
        north_at = markers.project(cx + far * math.cos(n), cy + far * math.sin(n), cx, cy, map_yaw, radius,
                                   clamp=True, square=square)  # fmt: skip
    Minimap.set_north(north_at)
    _gather(pc, pawn, world, full, slow, paused, lx, ly, lz, radius)

    # Icons only need moving when the view moved or what's shown changed (an update that found the
    # same markers, icons and route places nothing: the four layers cost ~50 us even with no call)
    view_key = (round(cx, 1), round(cy, 1), round(map_yaw, 1), radius, square, Minimap.size, Minimap.icon_scale,
                Minimap.builds)  # fmt: skip
    if view_key == State.view_key and not (full or slow):
        return
    content = (State.markers, State.icons, State.route, State.route_color, bool(vertical_option.value))
    if view_key == State.view_key and content == State.shown_content:
        return
    State.view_key, State.shown_content = view_key, content

    def place(x: float, y: float, clamp: bool = False) -> tuple[float, float] | None:
        return markers.project(x, y, cx, cy, map_yaw, radius, clamp=clamp, square=square)

    def vertical(z: float | None) -> int:
        """1 well above you, -1 well below, else 0 (the compass's chevrons)."""
        if z is None or not vertical_option.value:
            return 0
        dz = z - lz
        return 1 if dz > VERTICAL_GAP else -1 if dz < -VERTICAL_GAP else 0

    color = State.route_color
    # The route's dots also get a fixed place (north up around a point near you, kept while you stay
    # within range of it): the widget turns and moves them all at once (Minimap.set_turned), so a dot
    # costs a call only when it's new, not each frame the view moves
    ref = State.turn_ref
    if ref is None or ref[2] != radius or (lx - ref[0]) ** 2 + (ly - ref[1]) ** 2 > radius * radius:
        ref = State.turn_ref = (lx, ly, radius)
    route = State.route
    cache = State.route_cache
    if cache[0] is not route or cache[1] != (ref, north):
        cache = State.route_cache = (route, (ref, north), [markers.offset(x, y, ref[0], ref[1], north, radius) for x, y in route])
    # markers.project (unclamped) for each dot, with the turn's cosine and sine worked out once
    rc, rs = math.cos(math.radians(map_yaw)), math.sin(math.radians(map_yaw))
    route_items, route_fixed = [], []
    for (x, y), fixed in zip(route, cache[2]):
        dx, dy = x - cx, y - cy
        u, v = (-dx * rs + dy * rc) / radius, -(dx * rc + dy * rs) / radius
        if (max(abs(u), abs(v)) if square else math.hypot(u, v)) <= 1.0:
            route_items.append((DOT_TEXTURE, ROUTE_DOT_SIZE, color, u, v, 0))
            route_fixed.append(fixed)
    Minimap.set_turned("route", route_items, route_fixed, north - map_yaw,
                       markers.offset(ref[0], ref[1], cx, cy, map_yaw, radius))  # fmt: skip
    moved: dict[int, tuple[float, float]] = {}
    if spread_option.value:
        moved = _spread_places(full or slow, cx, cy, map_yaw, radius, square)
    else:
        State.spread_zoom = ()  # (laid out again when it's back on)
        if Minimap.leaders:
            Minimap.set_leaders([])
    icon_items = []
    for k, icon in enumerate(State.icons):
        where = moved.get(k) or place(icon["x"], icon["y"])
        if where is not None:
            icon_items.append((icon["texture"], ICON_SIZE, (1, 1, 1, 1), where[0], where[1], vertical(icon.get("z"))))
    Minimap.set_layer("icon", icon_items)
    marker_items, edge_items = [], []
    rank = len(State.icons)  # (the spreading markers' places in the layout)
    for marker in State.markers:
        path, base, tint = MARKER_LOOKS[marker.kind]
        texture = marker.texture
        if texture and not Minimap.has_texture(texture):  # (a DLC's art not installed)
            texture = None
            if marker.kind == markers.FRIEND_CLASS:
                path, base, tint = MARKER_LOOKS[markers.FRIEND]
        where = place(marker.x, marker.y)
        if marker.kind in SPREAD_KINDS:
            where = moved.get(rank, where)
            rank += 1
        items = marker_items
        if where is None and marker.clamp:
            # Out of range: on the ring itself, so it doesn't look close
            where, items = place(marker.x, marker.y, True), edge_items
        if where is not None:
            items.append((texture or path, base, tint, where[0], where[1], vertical(marker.z)))
    Minimap.set_layer("marker", marker_items)
    Minimap.set_layer(EDGE_LAYER, edge_items)


# region Page mode


# Tiles kept around you in page mode, in ranges: the turned square view reaches sqrt(2) ranges into
# its corners, plus a little to have the next tile before it shows
PAGE_TILE_REACH = 1.6
FOG_S = 0.5  # how often the live fog grid is read (16 KB, one memory read)


def _hide_all(pc: UObject) -> None:
    """Hides the minimap (both modes)."""
    Minimap.show(pc, False)
    if State.page_on and State.page_vis:
        State.page_vis = False
        x, y, z, yaw = State.page_last
        page.set_sample(time.time(), x, y, z, yaw, False)


def _page_switch() -> None:
    """The "Draw in the HUD" option changed: start or stop page mode."""
    on = bool(page_option.value)
    State.page_on = on
    if on:
        Minimap.remove()  # our widget goes; the map model stays (page tiles are filmed with it)
        page.start()
        page.register()
    else:
        page.stop()
        page.unregister()
    tiles.Tiles.drop()  # the other mode's tile session (film slots, the folder)
    State.view_key, State.shown_content = (), ()
    State.page_force, State.page_vis, State.page_tiles_rev = True, False, -1
    State.tiles_next = State.fog_next = 0.0
    State.fog_drawn = None


def _vertical(z: float | None, lz: float) -> int:
    """1 well above you, -1 well below, else 0 (the compass's chevrons)."""
    if z is None or not vertical_option.value:
        return 0
    dz = z - lz
    return 1 if dz > VERTICAL_GAP else -1 if dz < -VERTICAL_GAP else 0




def _page_sample() -> None:
    """
    Page mode, the frames between the whole passes: only where you are, read from the pawn and
    controller the last pass looked at (its hide checks still hold: the cues are looked at again by
    the next pass, a frame or two later, as before). A sample every frame, not every other: the page
    gets each one a frame after it's taken (Gameface hands an XHR's answer over at its next frame),
    so every other frame made the newest sample 20 ms old at draw time, 34 ms at p90, and a start, a
    stop or a strafe showed 2-3 frames late (game 33460, 2026-11-02).
    """
    # (A new pawn, a vehicle: the next whole pass, at most 1 / "Marker updates per second" away,
    # takes it; the old one sits in its seat meanwhile, and location() follows an attached actor)
    pawn, pc = State.page_pawn(), State.pc()
    if pawn is None or pc is None:
        State.page_force = True
        return
    lx, ly, lz = markers.location(pawn)
    yaw = float(pc.ControlRotation.Yaw)
    State.page_last = (lx, ly, lz, yaw)
    page.set_sample(time.time(), lx, ly, lz, yaw, True)


def _live_fog(world: UObject, paused: bool) -> tuple[bytes, tuple[float, ...]] | None:
    """
    Fog of war, live: the game's explored grid and the world square it covers (fog.py), drawn over
    the map (both modes; the map is filmed without fog). Before, fog was filmed into the map: a
    change showed once fog.py had written it (every 4 s), tiles.py had read it (2 s) and the tiles
    under it had been filmed again (up to 2.5 s each).
    """
    name = _world_name(world)
    key = fog.Fog.key
    if not fog_option.value or fog.Fog.placement is None or key is None or key[1] != name.lower():
        return None
    return fog._Live.read(name, paused, fog.Fog.placement)  # noqa: SLF001


def _widget_fog(pc: UObject, world: UObject, paused: bool) -> None:
    """Widget mode: the live grid as the minimap's fog texture, made again only when it changed
    (an in-memory PNG, ~0.25 ms, imported ~0.5 ms; the grid compare is a memcmp)."""
    found = _live_fog(world, paused)
    strength = fog_strength_option.value / 100
    drawn = (found[0], strength, found[1]) if found is not None else None
    if drawn == State.fog_drawn and (drawn is None or Minimap.fog() is not None):
        return
    State.fog_drawn = drawn
    texture = None
    if found is not None:
        size = fog.MASK_SIZE
        if Minimap.krl is None:
            Minimap.krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject  # (never unloaded)
        texture = Minimap.krl.ImportBufferAsTexture2D(pc, fog.layer_png(found[0], size, size, strength))[0]
    Minimap.set_fog(texture, found[1] if texture is not None else None)


def _page_frame(pc: UObject, pawn: UObject, world: UObject, full: bool, slow: bool, paused: bool, now: float,
                me: tuple[float, float, float], yaw: float, north: float, radius: float) -> None:  # fmt: skip
    """
    Page mode, a frame something is due (markers, the slow pass, tiles): a position sample, the
    tiles around you, and what to draw (only sent when it changed). The frames between only take a
    position sample (_page_sample). The page draws it all each frame.
    """
    lx, ly, lz = me
    State.page_force = False
    State.page_vis = True
    State.page_last = (lx, ly, lz, yaw)
    if State.page_pawn() is not pawn:
        State.page_pawn = WeakPointer(pawn)
    page.set_sample(time.time(), lx, ly, lz, yaw, True)
    if now >= State.fog_next:
        State.fog_next = now + FOG_S
        found = _live_fog(world, paused)
        size = fog.MASK_SIZE
        page.set_fog(found[0] if found else None, size, size, fog_strength_option.value / 100, found[1] if found else None)
    if now >= State.tiles_next:
        tiles.Tiles.page_tick(pc, lx, ly, radius * PAGE_TILE_REACH, north, paused=False)
        State.tiles_next = now + (TILES_IDLE_GAP_S if tiles.Tiles.idle else tiles.ACTION_GAP_S)
        page.Feed.folder = tiles.Tiles.folder
        if tiles.Tiles.page_rev != State.page_tiles_rev:
            State.page_tiles_rev = tiles.Tiles.page_rev
            listing = tiles.Tiles.page_list()
            listing["folder"] = tiles.Tiles.folder.name if tiles.Tiles.folder is not None else ""
            page.set_section("tiles", listing)
    if slow or State.ui <= 0 or Zoom.direction:  # (zooming: the new range each sample)
        if now >= State.ui_next or State.ui <= 0:
            State.ui, State.ui_next = _ui_scale(pc), now + UI_SCALE_S
        page.set_section("cfg", {
            "size": float(size_option.value), "mx": float(margin_x_option.value), "my": float(margin_y_option.value),
            "square": bool(square_option.value), "rotate": bool(rotate_option.value),
            "north": bool(north_option.value), "icon": icon_size_option.value / 100, "ui": State.ui,
            "radius": radius, "nyaw": north, "spread": bool(spread_option.value),
            "routeTop": bool(route_top_option.value),
        })  # fmt: skip
    if not (full or slow):
        return
    _gather(pc, pawn, world, full, slow, paused, lx, ly, lz, radius)
    if full or slow:
        items = []
        for marker in State.markers:
            path, base, tint = MARKER_LOOKS[marker.kind]
            texture = marker.texture
            if texture and not Minimap.has_texture(texture):  # (a DLC's art not installed)
                texture = None
                if marker.kind == markers.FRIEND_CLASS:
                    path, base, tint = MARKER_LOOKS[markers.FRIEND]
            items.append([
                page.art_url(texture or path), base, "" if tint == (1, 1, 1, 1) else page.css_color(tint),
                round(marker.x), round(marker.y), _vertical(marker.z, lz), 1 if marker.clamp else 0,
                1 if marker.kind in SPREAD_KINDS else 0,
            ])  # fmt: skip
        page.set_section("fast", items)
    if slow:
        page.set_section("slow", {
            "icons": [[page.art_url(i["texture"]), round(i["x"]), round(i["y"]), _vertical(i.get("z"), lz)]
                      for i in State.icons],
            "route": {"c": page.css_color(State.route_color), "d": [[round(x), round(y)] for x, y in State.route],
                      "s": round(radius / ROUTE_DOTS_PER_RANGE)},
        })  # fmt: skip


# endregion


@frametick.tick("bl4_minimap")
def on_frame() -> None:
    # Once per engine frame (shared/frametick: one hook for all our mods, the frame confirmed there)
    now = time.monotonic()
    if hudsway.HudSway.due():  # only until the start-up load is in and the option applied
        hudsway.HudSway.sync(now)
    if saved_on_option.value and Zoom.tick(now):
        State.page_force = True  # page mode: the whole pass (the new range) this frame
        State.hidden_until = 0.0
    try:
        if bool(page_option.value) != State.page_on:
            _page_switch()
        if State.page_on:
            uilink.tick()  # the page asks it where our feed is (does work at most once a frame overall)
            page.publish()  # dev: the page script, when its source changed (a clock compare most calls)
    except Exception as ex:  # noqa: BLE001
        if not State.error_logged:
            logging.error(f"[Minimap] page mode: {ex!r}")
            State.error_logged = True
    if (
        now < State.hidden_until and now - State.last_slow < SLOW_UPDATE_S
        and mapdata.MapData.job is None
    ):  # fmt: skip
        return  # hidden: looked at again every HIDDEN_POLL_S (the slow pass and the ECHO map's reads still run)
    if (
        State.page_on and State.page_vis and not State.page_force and now < State.tiles_next
        and now - State.last_slow < SLOW_UPDATE_S and now - State.last_update < 1 / max(1, rate_option.value)
        and mapdata.MapData.job is None
    ):  # fmt: skip
        # Page mode: the page draws every frame itself; the frames nothing else is due only send
        # where you are
        try:
            _page_sample()
        except Exception as ex:  # noqa: BLE001
            if not State.error_logged:
                logging.error(f"[Minimap] {ex!r}")
                State.error_logged = True
        return
    slow = now - State.last_slow >= SLOW_UPDATE_S
    if slow:
        State.last_slow = now
    # Not in the same frame as the slow update (both cost ~0.3-0.4 ms): a frame later instead
    full = not slow and now - State.last_update >= 1 / max(1, rate_option.value)
    if full:
        State.last_update = now
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        # The player controller, looked up again at the slow rate or once it's gone (get_pc walks
        # the engine's objects: ~10 us a frame)
        pc = State.pc()
        if pc is None or slow:
            pc = get_pc(possibly_loading=True)
            State.pc = WeakPointer(pc) if pc is not None else WeakPointer()
        if pc is not None:
            _frame(pc, full, slow)
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
    State.last_slow = 0.0
    State.markers = []
    State.collected = []
    State.extra = []
    State.ui = 0.0
    State.icons = []
    State.route = []
    State.error_logged = False
    State.north_yaw = None
    State.turn_ref = None
    State.viewport = WeakPointer()
    State.pc = WeakPointer()
    State.tiles_next = State.fog_next = 0.0
    State.fog_drawn = None
    State.name_world, State.world_name, State.ui_next, State.shown_content = WeakPointer(), "", 0.0, ()
    State.teleport_since = 0.0
    State.hide_next, State.hide_cue, State.paused, State.hidden = 0.0, (), False, False
    State.giver_key = ()
    State.spread_zoom = ()
    State.givers = []
    State.covered = set()
    Keys.was_down.clear()
    State.enabled = True
    hudsway.set_wanted(still_hud_option.value)


def _disable() -> None:
    State.enabled = False
    if State.page_on:
        page.stop()
        page.unregister()
        State.page_on = State.page_vis = False
    Minimap.remove()
    fog.Fog.clear()
    scene.Scene.destroy()
    loot.Loot.clear()
    objectives.Objectives.clear()
    mapwaypoints.MapWaypoints.clear()
    discovery.Discovery.clear()
    State.route_path, State.route_goal = [], None
    echoloc.Echoloc.clear()
    hudsway.set_wanted(False)


# A mod with no settings file loads switched off (a manual install, issue #5): start on
from .firston import ensure_on  # noqa: E402

ensure_on(__name__)

mod = build_mod(
    options=[
        toggle_key, zoom_in_key, zoom_out_key, size_option, range_option, icon_size_option, icons_option, route_option, route_top_option,
        contracts_option,
        rifts_option, loot_option,
        vertical_option, spread_option, square_option, rotate_option, north_option, rate_option, fog_option, fog_strength_option,
        margin_x_option, margin_y_option, page_option, still_hud_option, saved_on_option,
    ],
    hooks=[on_frame, on_teleport],
    on_enable=_enable,
    on_disable=_disable,
)
