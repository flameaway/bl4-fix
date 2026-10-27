"""
Moxxi's Big Encore from item cards: hover a legendary that a boss drops and press

- the waypoint key: the game's own custom waypoint (with its route on the map and compass) and a
  map pin go to the Big Encore machine for that boss;
- the travel key: the same, then you're taken there. On the same map that's a short teleport next
  to the machine; on another map we use the game's fast travel to the nearest station on that map,
  then teleport to the machine once it has loaded. A co-op client's position belongs to the host,
  so a client's teleport is done by the host's copy of our mods (BL4 Direct's co-op channel); with
  an unmodded host a client gets the waypoint only.

Neither keys nor messages flow from the game's menus to us directly, so:

- We poll the keys from Windows (only while the game window is focused) and count presses in a
  loose file the card's script polls (uiresources/bl4direct/hotkey.json).
- When a count changes while a legendary card with a Big Encore source is showing, the script
  answers through a UI sound event: ScriptEvent_Spatial(name, x, y) calls the page's ui_script
  function `name` with position {x, y}. It sends to every widget on its page (only the one owning
  the function reacts). We hook those functions, pick out our messages by a very negative x, and
  swallow them. x = -(MARK + op * 0x10000 + machine), y = the key press number, so we act once per
  press. Both arrive as 32-bit floats, so values stay below 2^24.
- We report back in hotkey.json so the card can confirm with a popup and a sound.

card_page/encore.json (made by tools/gen_item_cards.py) lists the machines with their location
and nearest fast travel station; the card's drop data refers to them by index.
"""

from __future__ import annotations

import ctypes
import json
import math
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Block
from unrealsdk.unreal import FGameDataHandle, WrappedStruct

from . import card_page

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: no travel as a co-op client
    coop = None
try:
    # In front of the machine, never in a wall (an older BL4 Direct lacks it: the rings below)
    from bl4_direct import stand
except Exception:  # noqa: BLE001
    stand = None

HOTKEY_PATH = "bl4direct/hotkey.json"
# Sound events we carry messages on: one the inventory page has, one the HUD page has
CHANNEL_FUNCS = (
    "/Game/UI/Scripts/ui_script_backpack.ui_script_backpack_C:ClickPistol",
    "/Game/UI/Scripts/ui_script_hud_skills.ui_script_hud_skills_C:FailToUseGadget",
)
# Our messages have position.X = -(MARK + op * 0x10000 + arg); real sounds sit on screen
MARK = 1 << 23
OP_PIN_MACHINE, OP_TRAVEL = 1, 2

PIN_CUSTOM_WAYPOINT, PIN_LOCATION = 1, 2
STATION_TYPE = 24576  # def handle type of travel stations (RequestTeleportToStation's argument)
# Where to stand without BL4 Direct's stand.py (older copies): rings around the machine
STAND_RADII = (300, 450, 600)
STAND_HEIGHT = 120
# After a map loads, give it a moment before teleporting within it
ARRIVAL_SETTLE = 3.0
TRAVEL_TIMEOUT = 180.0

# ctypes.windll, not PyDLL: these calls must let go of the GIL (GetAsyncKeyState can take ~100 ms,
# e.g. while the desktop is streamed). Keys go through BL4 Mod Menu's key thread when it's loaded.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


class _Key:
    def __init__(self) -> None:
        self.name: str | None = None
        self.down = False
        self.presses = 0


_waypoint_key = _Key()
_travel_key = _Key()
_machines: list[dict] | None = None
_pinned: dict[str, Any] | None = None
_travel: dict[str, Any] | None = None
_last_seq: dict[int, int] = {}
_our_location_pin: str | None = None
# A cross-map trip in progress: {"index", "started", "arrived_at"}
_pending: dict[str, Any] | None = None


def _game_focused() -> bool:
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
    return _game_focused() and bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def virtual_key(name: str | None) -> int | None:
    """Windows key code for an Unreal key name: letters, number row ("One"), F1-F12."""
    if not name:
        return None
    digits = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in digits:
        return ord("0") + digits.index(name)
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return 0x70 + int(name[1:]) - 1
    return None


def _write_status(game_dir: Path) -> None:
    target = game_dir / card_page.UI_PREFIX / HOTKEY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    status = {
        "key": _waypoint_key.name,
        "presses": _waypoint_key.presses,
        "travel_key": _travel_key.name,
        "travel_presses": _travel_key.presses,
        "pinned": _pinned,
        "travel": _travel,
    }
    tmp.write_text(json.dumps(status), encoding="utf-8")
    tmp.replace(target)


def _poll(key: _Key, name: str | None) -> bool:
    """Updates one key; True if the status file needs writing."""
    changed = name != key.name
    key.name = name
    vk = virtual_key(name)
    down = vk is not None and _async_down(vk)
    if down and not key.down:
        key.presses += 1
        changed = True
    key.down = down
    return changed


def poll_keys(game_dir: Path, waypoint_key: str | None, travel_key: str | None) -> None:
    """Call every frame or so: counts key presses for the card's script, and finishes trips."""
    if _poll(_waypoint_key, waypoint_key) | _poll(_travel_key, travel_key):
        _write_status(game_dir)
    if _pending:
        _continue_trip(game_dir)


# region Machines and pins


def _machine(index: int) -> dict | None:
    global _machines
    if _machines is None:
        _machines = json.loads((resources.files(card_page) / "encore.json").read_text(encoding="utf-8"))
    return _machines[index] if 0 <= index < len(_machines) else None


def _current_map() -> str:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    return str(world.Name) if world else ""


def _create_pin(ps: Any, setup: Any) -> None:
    fn = ps.Server_CreateDiscoveryPin
    args = WrappedStruct(fn.func)
    pin = args.InPinData
    pin.OwningPlayer = ps
    setup(pin)
    fn(args)


def pin_machine(game_dir: Path, index: int, seq: int) -> None:
    global _pinned, _our_location_pin
    machine = _machine(index)
    pc = get_pc()
    if machine is None or pc is None or pc.PlayerState is None:
        return
    ps = pc.PlayerState

    # The custom waypoint: one per player, the one the game routes you to
    for pinned in list(ps.DiscoveryPinningState.PinnedDatas):
        if pinned.pintype == PIN_CUSTOM_WAYPOINT:
            ps.Server_RemoveDiscoveryPin(PIN_CUSTOM_WAYPOINT, pinned.pintypeindex)

    def waypoint(pin: Any) -> None:
        pin.pintype = PIN_CUSTOM_WAYPOINT
        pin.PinnedCustomWaypointWorldName = machine["map"]
        where = pin.PinnedCustomWaypointLocation
        where.X, where.Y, where.Z = machine["x"], machine["y"], machine["z"]

    _create_pin(ps, waypoint)

    # And highlight the machine itself (replacing the one we pinned last time, if any)
    for pinned in list(ps.DiscoveryPinningState.PinnedDatas):
        name = str(pinned.pinneddiscoverylocationmetadataname).lower()
        if pinned.pintype == PIN_LOCATION and name in (_our_location_pin, machine["location"]):
            ps.Server_RemoveDiscoveryPin(PIN_LOCATION, pinned.pintypeindex)

    def location(pin: Any) -> None:
        pin.pintype = PIN_LOCATION
        pin.pinneddiscoverylocationmetadataname = machine["location"]

    _create_pin(ps, location)
    _our_location_pin = machine["location"]

    here = _current_map()
    _pinned = {
        "id": index,
        "boss": machine["boss"],
        "place": machine["place"],
        "elsewhere": bool(here) and here.lower() != machine["map"].lower(),
        "seq": seq,
    }
    _write_status(game_dir)
    logging.info(f"[Item Cards] Waypoint set on the Big Encore machine for {machine['boss']}")


# endregion
# region Travel


def _is_client() -> bool:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    driver = world.NetDriver if world else None
    return bool(driver and driver.ServerConnection)


def _stand_args(target: dict[str, Any]) -> dict[str, Any]:
    """teleport_near's arguments for where to stand: the machine's facing, or the old rings."""
    if stand is not None:
        return {"yaw": target.get("yaw"), "location": target.get("location")}
    return {"radii": STAND_RADII, "height": STAND_HEIGHT}


def _teleport_next_to(machine: dict) -> bool:
    """Puts the player beside the machine, facing it. False if no spot around it was free."""
    if stand is not None:
        return bool(stand.place(get_pc(), machine).get("ok"))
    pawn = get_pc().Pawn
    mx, my, mz = machine["x"], machine["y"], machine["z"]
    for radius in STAND_RADII:
        for step in range(8):
            angle = step * math.pi / 4
            x, y = mx + radius * math.cos(angle), my + radius * math.sin(angle)
            where = unrealsdk.make_struct("Vector", X=x, Y=y, Z=mz + STAND_HEIGHT)
            facing = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=math.degrees(math.atan2(my - y, mx - x)), Roll=0)
            if pawn.K2_TeleportTo(where, facing):
                return True
    return False


def _set_travel(game_dir: Path, machine: dict, index: int, state: str, seq: int) -> None:
    global _travel
    _travel = {"id": index, "boss": machine["boss"], "place": machine["place"], "state": state, "seq": seq}
    _write_status(game_dir)


def travel_to_machine(game_dir: Path, index: int, seq: int) -> None:
    global _pending
    machine = _machine(index)
    pc = get_pc()
    if machine is None or pc is None or pc.Pawn is None:
        return
    client = _is_client()
    if client and (coop is None or not coop.host_has(coop.TELEPORT)):
        pin_machine(game_dir, index, seq)  # a server call: works for clients too
        _set_travel(game_dir, machine, index, "host_only", seq)
        return
    pin_machine(game_dir, index, seq)

    if _current_map().lower() == machine["map"].lower():
        if client:
            _set_travel(game_dir, machine, index, "travelling", seq)
            _hop_as_client(game_dir, machine, index, seq)
            return
        ok = _teleport_next_to(machine)
        _set_travel(game_dir, machine, index, "arrived" if ok else "blocked", seq)
        return

    # Another map: the game's own fast travel there, then the last stretch once it has loaded
    fn = pc.RequestTeleportToStation
    args = WrappedStruct(fn.func)
    args.station = FGameDataHandle(STATION_TYPE, machine["station"])
    args.bForRespawn = False
    args.bLocalOnly = False
    fn(args)
    _pending = {"index": index, "seq": seq, "started": time.monotonic(), "arrived_at": None}
    _set_travel(game_dir, machine, index, "travelling", seq)
    logging.info(f"[Item Cards] Travelling to {machine['station']} for the Big Encore machine of {machine['boss']}")


def _continue_trip(game_dir: Path) -> None:
    global _pending
    pending = _pending
    machine = _machine(pending["index"])
    now = time.monotonic()
    if machine is None or now - pending["started"] > TRAVEL_TIMEOUT:
        _pending = None
        if machine:
            _set_travel(game_dir, machine, pending["index"], "failed", pending["seq"])
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None or _current_map().lower() != machine["map"].lower():
        return
    if pending["arrived_at"] is None:
        pending["arrived_at"] = now
        return
    if now - pending["arrived_at"] < ARRIVAL_SETTLE:
        return
    _pending = None
    if _is_client():
        _hop_as_client(game_dir, machine, pending["index"], pending["seq"])
        return
    ok = _teleport_next_to(machine)
    _set_travel(game_dir, machine, pending["index"], "arrived" if ok else "blocked", pending["seq"])


def _hop_as_client(game_dir: Path, machine: dict, index: int, seq: int) -> None:
    """As a co-op client: the host's mod moves us next to the machine."""

    def done(reply: dict) -> None:
        state = "arrived" if reply.get("ok") else ("blocked" if reply.get("error") == "no room" else "host_only")
        _set_travel(game_dir, machine, index, state, seq)

    if coop is None or not coop.teleport_near(machine["x"], machine["y"], machine["z"], done, **_stand_args(machine)):  # fmt: skip
        _set_travel(game_dir, machine, index, "host_only", seq)


# endregion


def on_channel(game_dir: Path, args: WrappedStruct) -> type[Block] | None:
    """PRE hook body for CHANNEL_FUNCS: handles our messages, lets real sounds through."""
    x = args.position.X
    if x > -MARK + 0.5:
        return None
    code = round(-x) - MARK
    op, arg, seq = code >> 16, code & 0xFFFF, round(args.position.Y)
    # The page sends each request to all its widgets; act once per key press
    if _last_seq.get(op) == seq:
        return Block
    _last_seq[op] = seq
    try:
        if op == OP_PIN_MACHINE:
            pin_machine(game_dir, arg, seq)
        elif op == OP_TRAVEL:
            travel_to_machine(game_dir, arg, seq)
    except Exception as ex:  # noqa: BLE001 - never let our handler break the menu
        logging.error(f"[Item Cards] Big Encore: {ex!r}")
    return Block
