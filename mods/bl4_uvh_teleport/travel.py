"""
Getting the player to a boss: the same trip the item cards' Big Encore travel makes
(bl4_item_cards/encore_waypoint.py), step for step.

- Same map: at once, a teleport next to the boss's Big Encore machine (it stands at the arena),
  facing it, however far away it is.
- Another map: the game's own fast travel to the station nearest the machine (bosses.json, made by
  tools/gen_uvh_teleport.py), then that teleport once the map has loaded and had a moment.
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
from unrealsdk.unreal import FGameDataHandle, WrappedStruct

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: no trips as a co-op client
    coop = None

PIN_CUSTOM_WAYPOINT, PIN_LOCATION = 1, 2
STATION_TYPE = 24576  # def handle type of travel stations (RequestTeleportToStation's argument)
# Where to stand: rings around the machine, tried until a spot is free
STAND_RADII = (300, 450, 600)
STAND_HEIGHT = 120
# After a map loads, give it a moment before teleporting within it
ARRIVAL_SETTLE = 3.0
TRAVEL_TIMEOUT = 180.0


class Refused(Exception):
    """Why a trip can't start, said to the player."""


class S:
    bosses: list[dict[str, Any]] | None = None
    # A cross-map trip in progress: {"boss", "started", "arrived_at"}
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

    if not coop.teleport_near(boss["x"], boss["y"], boss["z"], done, radii=STAND_RADII, height=STAND_HEIGHT):
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


def _teleport_next_to(boss: dict[str, Any]) -> bool:
    """Puts the player beside the machine, facing it. False if no spot around it was free."""
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
    S.trip = {"boss": boss, "started": time.monotonic(), "arrived_at": None}
    logging.info(f"[UVH Teleport] Travelling to {boss['station']} for {boss['boss']} (\"{boss['title']}\")")
    return False


def _end(message: str, *, error: bool = False) -> None:
    S.trip = None
    (logging.error if error else logging.info)(f"[UVH Teleport] {message}")


def tick() -> None:
    """Every frame: finishes a cross-map trip once its map has loaded."""
    trip = S.trip
    if trip is None:
        return
    boss = trip["boss"]
    now = time.monotonic()
    if now - trip["started"] > TRAVEL_TIMEOUT:
        _end(f"The trip to {boss['boss']} didn't arrive; follow the waypoint.", error=True)
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None or current_map().lower() != boss["map"].lower():
        return
    if trip["arrived_at"] is None:
        trip["arrived_at"] = now
        return
    if now - trip["arrived_at"] < ARRIVAL_SETTLE:
        return
    if is_client():
        S.trip = None
        if _client_ready():
            _hop_as_client(boss)
        else:
            _end(f"In co-op the host needs BL4 Direct to move you; follow the waypoint to {boss['boss']}.", error=True)
        return
    if _teleport_next_to(boss):
        _end(f"At {boss['boss']} ({boss['place']}).")
    else:
        _end(f"No room to stand next to {boss['boss']}'s Big Encore machine; follow the waypoint.", error=True)


def cancel() -> None:
    S.trip = None
    S.hop = None


# endregion
