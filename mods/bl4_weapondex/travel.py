"""
Waypoint / travel to a boss from the dex: the same trip as the item cards' Big Encore travel
(bl4_item_cards/encore_waypoint.py) and UVH Boss Teleport (bl4_uvh_teleport/travel.py), step for
step.

- Waypoint: the game's custom waypoint on the boss's Big Encore machine (with its route on the map
  and compass), and the fight highlighted on the map.
- Travel: the waypoint, then on the same map at once a teleport next to the machine, facing it,
  however far away it is; on another map the game's own fast travel to the station nearest the
  machine, then that teleport once the map has loaded and had a moment.
- A co-op client's position belongs to the host: a client's teleports are done by the host's copy
  of BL4 Direct (its co-op channel, coop.teleport_near). With an unmodded host a client gets the
  waypoint only.

Where each machine is: travel.json (tools/gen_weapondex_travel.py), keyed by dex boss id. Plain
module state, so a hot reload drops any trip in progress.
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

from . import data

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
    machines: dict[str, dict[str, Any]] | None = None
    # A cross-map trip in progress: {"boss", "started", "arrived_at"}
    trip: dict[str, Any] | None = None
    our_location_pin: str | None = None
    # A client's teleport the host is making: [boss, reply or None while waiting]
    hop: list[Any] | None = None


def machines() -> dict[str, dict[str, Any]]:
    """Boss id -> its Big Encore machine {map, location, x, y, z, station}."""
    if S.machines is None:
        try:
            loaded = json.loads((resources.files(__package__) / "travel.json").read_text(encoding="utf-8"))
            S.machines = loaded["bosses"]
        except (OSError, ValueError, KeyError) as ex:
            logging.error(f"[Kairosdex] travel.json: {ex!r}")
            S.machines = {}
    return S.machines


def boss(boss_id: str) -> dict[str, Any] | None:
    """A boss's machine with its name and place, or None if it has none."""
    machine = machines().get(boss_id)
    if machine is None:
        return None
    entry = next((b for b in data.dex()["bosses"] if b["id"] == boss_id), None)
    if entry is None:
        return None
    return {**machine, "id": boss_id, "boss": entry["name"], "place": entry["area"]}


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
    hopped, reply = S.hop
    S.hop = None
    return hopped, reply


def _client_ready() -> bool:
    return coop is not None and coop.host_has(coop.TELEPORT)


def _hop_as_client(target: dict[str, Any]) -> None:
    hop: list[Any] = [target, None]
    S.hop = hop

    def done(reply: dict[str, Any]) -> None:
        hop[1] = reply

    if not coop.teleport_near(target["x"], target["y"], target["z"], done, radii=STAND_RADII, height=STAND_HEIGHT):
        hop[1] = {"ok": False, "error": "host"}


# region Pins


def _create_pin(ps: Any, setup: Callable[[Any], None]) -> None:
    fn = ps.Server_CreateDiscoveryPin
    args = WrappedStruct(fn.func)
    pin_data = args.InPinData
    pin_data.OwningPlayer = ps
    setup(pin_data)
    fn(args)


def pin(target: dict[str, Any]) -> None:
    """The game's custom waypoint on the machine, and the fight highlighted on the map."""
    pc = get_pc()
    if pc is None or pc.PlayerState is None:
        raise Refused("You need to be in game.")
    ps = pc.PlayerState

    # One custom waypoint per player: the one the game routes you to
    for pinned in list(ps.DiscoveryPinningState.PinnedDatas):
        if pinned.pintype == PIN_CUSTOM_WAYPOINT:
            ps.Server_RemoveDiscoveryPin(PIN_CUSTOM_WAYPOINT, pinned.pintypeindex)

    def waypoint(pin_data: Any) -> None:
        pin_data.pintype = PIN_CUSTOM_WAYPOINT
        pin_data.PinnedCustomWaypointWorldName = target["map"]
        where = pin_data.PinnedCustomWaypointLocation
        where.X, where.Y, where.Z = target["x"], target["y"], target["z"]

    _create_pin(ps, waypoint)

    # Replace the fight we highlighted last time, if any
    for pinned in list(ps.DiscoveryPinningState.PinnedDatas):
        name = str(pinned.pinneddiscoverylocationmetadataname).lower()
        if pinned.pintype == PIN_LOCATION and name in (S.our_location_pin, target["location"]):
            ps.Server_RemoveDiscoveryPin(PIN_LOCATION, pinned.pintypeindex)

    def location(pin_data: Any) -> None:
        pin_data.pintype = PIN_LOCATION
        pin_data.pinneddiscoverylocationmetadataname = target["location"]

    _create_pin(ps, location)
    S.our_location_pin = target["location"]


def elsewhere(target: dict[str, Any]) -> bool:
    """The machine is on another map than the player's (its waypoint shows on that map)."""
    here = current_map()
    return bool(here) and here.lower() != target["map"].lower()


# endregion
# region Trips


def _teleport_next_to(target: dict[str, Any]) -> bool:
    """Puts the player beside the machine, facing it. False if no spot around it was free."""
    pawn = get_pc().Pawn
    mx, my, mz = target["x"], target["y"], target["z"]
    for radius in STAND_RADII:
        for step in range(8):
            angle = step * math.pi / 4
            x, y = mx + radius * math.cos(angle), my + radius * math.sin(angle)
            where = unrealsdk.make_struct("Vector", X=x, Y=y, Z=mz + STAND_HEIGHT)
            facing = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=math.degrees(math.atan2(my - y, mx - x)), Roll=0)
            if pawn.K2_TeleportTo(where, facing):
                return True
    return False


def start(target: dict[str, Any]) -> bool | None:
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
        pin(target)  # server calls: a client's waypoint works without the host's mods
        raise Refused("In co-op the host needs BL4 Direct to move you; the waypoint is set.")
    if busy():
        raise Refused(f"Already on the way to {(S.trip or {'boss': S.hop[0]})['boss']['boss']}.")
    here = not elsewhere(target)
    if not here and target.get("station") is None:
        raise Refused(f"No fast travel station known for {target['boss']}.")
    pin(target)
    if here and client:
        _hop_as_client(target)
        return None
    if here:
        if not _teleport_next_to(target):
            raise Refused(f"No room to stand next to {target['boss']}'s Big Encore machine; follow the waypoint.")
        logging.info(f"[Kairosdex] At {target['boss']} ({target['place']})")
        return True

    # Another map: the game's own fast travel there, then the last stretch once it has loaded
    fn = pc.RequestTeleportToStation
    args = WrappedStruct(fn.func)
    args.station = FGameDataHandle(STATION_TYPE, target["station"])
    args.bForRespawn = False
    args.bLocalOnly = False
    fn(args)
    S.trip = {"boss": target, "started": time.monotonic(), "arrived_at": None}
    logging.info(f"[Kairosdex] Travelling to {target['station']} for {target['boss']}")
    return False


def _end(message: str, *, error: bool = False) -> None:
    S.trip = None
    (logging.error if error else logging.info)(f"[Kairosdex] {message}")


def tick() -> None:
    """Every frame: finishes a cross-map trip once its map has loaded."""
    trip = S.trip
    if trip is None:
        return
    target = trip["boss"]
    now = time.monotonic()
    if now - trip["started"] > TRAVEL_TIMEOUT:
        _end(f"The trip to {target['boss']} didn't arrive; follow the waypoint.", error=True)
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None or current_map().lower() != target["map"].lower():
        return
    if trip["arrived_at"] is None:
        trip["arrived_at"] = now
        return
    if now - trip["arrived_at"] < ARRIVAL_SETTLE:
        return
    if is_client():
        S.trip = None
        if _client_ready():
            _hop_as_client(target)
        else:
            _end(f"In co-op the host needs BL4 Direct to move you; follow the waypoint to {target['boss']}.", error=True)
        return
    if _teleport_next_to(target):
        _end(f"At {target['boss']} ({target['place']}).")
    else:
        _end(f"No room to stand next to {target['boss']}'s Big Encore machine; follow the waypoint.", error=True)


def cancel() -> None:
    S.trip = None
    S.hop = None


# endregion
