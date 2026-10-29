"""
Getting the player to a boss: the same trip the item cards' Big Encore travel makes
(bl4_item_cards/encore_waypoint.py), step for step.

- Same map: at once, a teleport next to the boss's Big Encore machine (it stands at the arena),
  facing it, however far away it is.
- Another map: the game's own fast travel to the station nearest the machine (bosses.json, made by
  tools/gen_uvh_teleport.py), then that teleport once the game has put the player on the station
  (it does that a few seconds after the map loads, and it undid a hop made before; how the wait
  works: bl4_weapondex/travel.py, the same code).
- A co-op client's position belongs to the host: a client's teleports are done by the host's copy
  of BL4 Direct (its co-op channel, coop.teleport_near), so the host needs BL4 Direct and a client
  needs this mod. With an unmodded host a client gets the waypoint only.

Also sets the game's custom waypoint on the boss (with its route on the map and compass) and
highlights the fight on the map, so the way back is marked too.

Plain module state, so a hot reload (dev/reload_uvh_teleport.py) drops any trip in progress.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from importlib import resources
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type, add_hook, remove_hook
from unrealsdk.unreal import FGameDataHandle, WrappedStruct

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: no trips as a co-op client
    coop = None
try:
    # In front of the machine, never in a wall (an older BL4 Direct lacks it: the rings below)
    from bl4_direct import stand
except Exception:  # noqa: BLE001
    stand = None

PIN_CUSTOM_WAYPOINT, PIN_LOCATION = 1, 2
_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject

STATION_TYPE = 24576  # def handle type of travel stations (RequestTeleportToStation's argument)
# Where to stand without BL4 Direct's stand.py (older copies): rings around the machine
STAND_RADII = (300, 450, 600)
STAND_HEIGHT = 120
# Cross-map trips: the game's "putting you somewhere" call, fired on arrival and when it puts the
# player on the station (a client RPC, so it reaches a co-op client's hook too)
NOTIFY_FUNC = "/Script/OakGame.OakPlayerController:ClientNotifyTeleporting"
NOTIFY_HOOK = "bl4_uvh_teleport_trip_notify"
PLACE_DELAY = 1.0  # after the game's last move, before ours
LATE_NOTIFY = 0.5  # a notify this long after the pawn appeared is the move onto the station
PLACED_MOVE = 1000.0  # the pawn moved this far from where it appeared: the game has placed it
PLACE_FALLBACK = 10.0  # neither sign this long after arriving: hop anyway (the guard still watches)
GUARD_S = 15.0  # after our hop, a game move within this long gets our hop again
MAX_HOPS = 3
TRAVEL_TIMEOUT = 180.0


class Refused(Exception):
    """Why a trip can't start, said to the player."""


class S:
    bosses: list[dict[str, Any]] | None = None
    # A cross-map trip in progress: {"boss", "arrived_at", "first_spot",
    # "notified_at", "hops", "hopped_at"} (see tick()); its times are on "clock", the trip's
    # unpaused seconds ("ticked" = monotonic time of the last tick)
    trip: dict[str, Any] | None = None
    our_location_pin: str | None = None
    # A client's teleport the host is making: (boss, reply or None while waiting)
    hop: list[Any] | None = None


def bosses() -> list[dict[str, Any]]:
    if S.bosses is None:
        data = json.loads((resources.files(__package__) / "bosses.json").read_text(encoding="utf-8"))
        S.bosses = data["bosses"]
    return S.bosses


def _plain(text: Any) -> str:
    return "".join(c for c in str(text or "").lower() if c.isalnum())


def find(text: str) -> dict[str, Any] | None:
    """A boss by its challenge's title (as the menu shows it), its boss or its challenge name."""
    want = _plain(text)
    if not want:
        return None
    for key in ("title", "boss", "challenge"):
        for boss in bosses():
            if _plain(boss[key]) == want:
                return boss
    hits = [b for b in bosses() if want in _plain(b["boss"]) or want in _plain(b["title"])]
    return hits[0] if len(hits) == 1 else None


def current_map() -> str:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    return str(world.Name) if world else ""


def is_client() -> bool:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    driver = world.NetDriver if world else None
    return bool(driver and driver.ServerConnection)


def busy() -> bool:
    return S.trip is not None or (S.hop is not None and S.hop[1] is None)


def take_hop_result() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A client's finished hop: (boss, the host's reply {"ok": ..., "error": ...}), once."""
    if S.hop is None or S.hop[1] is None:
        return None
    boss, reply = S.hop
    S.hop = None
    return boss, reply


def _client_ready() -> bool:
    return coop is not None and coop.host_has(coop.TELEPORT)


def _hop_as_client(boss: dict[str, Any]) -> None:
    hop: list[Any] = [boss, None]
    S.hop = hop

    def done(reply: dict[str, Any]) -> None:
        hop[1] = reply

    if not coop.teleport_near(boss["x"], boss["y"], boss["z"], done, **_stand_args(boss)):
        hop[1] = {"ok": False, "error": "host"}


# region Pins


def _create_pin(ps: Any, setup: Callable[[Any], None]) -> None:
    fn = ps.Server_CreateDiscoveryPin
    args = WrappedStruct(fn.func)
    pin = args.InPinData
    pin.OwningPlayer = ps
    setup(pin)
    fn(args)


def pin(boss: dict[str, Any]) -> None:
    """The game's custom waypoint on the boss, and the fight highlighted on the map."""
    pc = get_pc()
    if pc is None or pc.PlayerState is None:
        return
    ps = pc.PlayerState

    # One custom waypoint per player: the one the game routes you to
    for pinned in list(ps.DiscoveryPinningState.PinnedDatas):
        if pinned.pintype == PIN_CUSTOM_WAYPOINT:
            ps.Server_RemoveDiscoveryPin(PIN_CUSTOM_WAYPOINT, pinned.pintypeindex)

    def waypoint(data: Any) -> None:
        data.pintype = PIN_CUSTOM_WAYPOINT
        data.PinnedCustomWaypointWorldName = boss["map"]
        where = data.PinnedCustomWaypointLocation
        where.X, where.Y, where.Z = boss["x"], boss["y"], boss["z"]

    _create_pin(ps, waypoint)

    # Replace the fight we highlighted last time, if any
    for pinned in list(ps.DiscoveryPinningState.PinnedDatas):
        name = str(pinned.pinneddiscoverylocationmetadataname).lower()
        if pinned.pintype == PIN_LOCATION and name in (S.our_location_pin, boss["location"]):
            ps.Server_RemoveDiscoveryPin(PIN_LOCATION, pinned.pintypeindex)

    def location(data: Any) -> None:
        data.pintype = PIN_LOCATION
        data.pinneddiscoverylocationmetadataname = boss["location"]

    _create_pin(ps, location)
    S.our_location_pin = boss["location"]


# endregion
# region Trips


def _stand_args(target: dict[str, Any]) -> dict[str, Any]:
    """teleport_near's arguments for where to stand: the machine's facing, or the old rings."""
    if stand is not None:
        return {"yaw": target.get("yaw"), "location": target.get("location")}
    return {"radii": STAND_RADII, "height": STAND_HEIGHT}


def _teleport_next_to(boss: dict[str, Any]) -> bool:
    """Puts the player beside the machine, facing it. False if no spot around it was free."""
    if stand is not None:
        return bool(stand.place(get_pc(), boss).get("ok"))
    pawn = get_pc().Pawn
    mx, my, mz = boss["x"], boss["y"], boss["z"]
    for radius in STAND_RADII:
        for step in range(8):
            angle = step * math.pi / 4
            x, y = mx + radius * math.cos(angle), my + radius * math.sin(angle)
            where = unrealsdk.make_struct("Vector", X=x, Y=y, Z=mz + STAND_HEIGHT)
            facing = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=math.degrees(math.atan2(my - y, mx - x)), Roll=0)
            if pawn.K2_TeleportTo(where, facing):
                return True
    return False


def start(boss: dict[str, Any]) -> bool | None:
    """
    Sets off for a boss. Raises Refused when it can't. True if the player is already there (same
    map), False if a fast travel to another map has started (the menus close for its loading
    screen), None if the host is moving us (a co-op client; see take_hop_result()).
    """
    pc = get_pc()
    if pc is None or pc.Pawn is None:
        raise Refused("You need to be in game, on your feet.")
    client = is_client()
    if client and not _client_ready():
        pin(boss)  # server calls: a client's waypoint works without the host's mods
        raise Refused("In co-op the host needs BL4 Direct to move you; the waypoint is set.")
    if busy():
        raise Refused(f"Already on the way to {(S.trip or {'boss': S.hop[0]})['boss']['boss']}.")
    here = current_map().lower() == boss["map"].lower()
    if not here and boss.get("station") is None:
        raise Refused(f"No fast travel station known for {boss['boss']}.")
    pin(boss)
    if here and client:
        _hop_as_client(boss)
        return None
    if here:
        if not _teleport_next_to(boss):
            raise Refused(f"No room to stand next to {boss['boss']}'s Big Encore machine; follow the waypoint.")
        logging.info(f"[UVH Teleport] At {boss['boss']} ({boss['place']}) for \"{boss['title']}\"")
        return True

    # Another map: the game's own fast travel there, then the last stretch once it has loaded
    fn = pc.RequestTeleportToStation
    args = WrappedStruct(fn.func)
    args.station = FGameDataHandle(STATION_TYPE, boss["station"])
    args.bForRespawn = False
    args.bLocalOnly = False
    fn(args)
    S.trip = {"boss": boss, "clock": 0.0, "ticked": time.monotonic(),
              "arrived_at": None, "first_spot": None, "notified_at": 0.0, "hops": 0, "hopped_at": None}  # fmt: skip
    _watch_notify(True)
    logging.info(f"[UVH Teleport] Travelling to {boss['station']} for {boss['boss']} (\"{boss['title']}\")")
    return False


def _on_notify(_obj: Any, _args: Any, _ret: Any, _func: Any) -> None:
    """The game is moving the player (arrival in the map, then onto the station)."""
    try:
        if S.trip is not None:
            S.trip["notified_at"] = S.trip["clock"]
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[UVH Teleport] trip notify: {ex!r}")


def _watch_notify(on: bool) -> None:
    """The notify hook, only while a cross-map trip is on (fixed id: a reload replaces it)."""
    try:
        if on:
            add_hook(NOTIFY_FUNC, Type.POST, NOTIFY_HOOK, _on_notify)
        else:
            remove_hook(NOTIFY_FUNC, Type.POST, NOTIFY_HOOK)
    except Exception as ex:  # noqa: BLE001 - a renamed function: the position check still works
        logging.error(f"[UVH Teleport] trip notify hook: {ex!r}")


def _end(message: str, *, error: bool = False) -> None:
    S.trip = None
    _watch_notify(False)
    (logging.error if error else logging.info)(f"[UVH Teleport] {message}")


def _placed_by_game(trip: dict[str, Any], pawn: Any, now: float) -> bool:
    """Whether the game has put the player on the station (and had a moment since)."""
    if now - max(trip["arrived_at"], trip["notified_at"]) < PLACE_DELAY:
        return False
    if trip["notified_at"] - trip["arrived_at"] >= LATE_NOTIFY or now - trip["arrived_at"] >= PLACE_FALLBACK:
        return True
    where = pawn.K2_GetActorLocation()
    fx, fy, fz = trip["first_spot"]
    return math.dist((where.X, where.Y, where.Z), (fx, fy, fz)) > PLACED_MOVE


def _hop(trip: dict[str, Any], now: float) -> bool:
    """Our move to the machine at the end of a cross-map trip. False when the trip is over."""
    target = trip["boss"]
    trip["hops"] += 1
    trip["hopped_at"] = now
    first = trip["hops"] == 1
    if is_client():
        if not _client_ready():
            _end(f"In co-op the host needs BL4 Direct to move you; follow the waypoint to {target['boss']}.", error=True)
            return False
        _hop_as_client(target)
        return True
    if not _teleport_next_to(target):
        _end(f"No room to stand next to {target['boss']}'s Big Encore machine; follow the waypoint.", error=True)
        return False
    if first:
        logging.info(f"[UVH Teleport] At {target['boss']} ({target['place']}).")
    else:
        logging.info(f"[UVH Teleport] The game moved you after the trip: back at {target['boss']}.")
    return True


def tick() -> None:
    """
    Every frame: finishes a cross-map trip. Once the map has loaded and the game has put the
    player on the station, the hop to the machine; then GUARD_S of watching for the game moving
    them again (a notify after our hop), which gets the hop again.
    """
    trip = S.trip
    if trip is None:
        return
    target = trip["boss"]
    real = time.monotonic()
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is not None and _statics.IsGamePaused(world):
        trip["ticked"] = real
        return
    trip["clock"] += real - trip["ticked"]
    trip["ticked"] = real
    now = trip["clock"]
    if now > TRAVEL_TIMEOUT:
        _end(f"The trip to {target['boss']} didn't arrive; follow the waypoint.", error=True)
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None or current_map().lower() != target["map"].lower():
        return
    if trip["arrived_at"] is None:
        where = pc.Pawn.K2_GetActorLocation()
        trip["arrived_at"] = now
        trip["first_spot"] = (where.X, where.Y, where.Z)
        return
    hopped_at = trip["hopped_at"]
    if hopped_at is None:
        if _placed_by_game(trip, pc.Pawn, now):
            _hop(trip, now)
        return
    if trip["notified_at"] > hopped_at:  # the game moved the player after our hop
        if trip["hops"] >= MAX_HOPS:
            _end(f"The game keeps moving you away from {target['boss']}; follow the waypoint.", error=True)
        elif now - trip["notified_at"] >= PLACE_DELAY:
            _hop(trip, now)
        return
    if now - hopped_at >= GUARD_S:
        S.trip = None
        _watch_notify(False)


def cancel() -> None:
    S.trip = None
    S.hop = None
    _watch_notify(False)


# endregion
