"""
Putting a player on the nearest place the game would respawn them.

Respawn points are travel station actors (`TravelStationObject`) whose type can respawn a player
and be their checkpoint (`TypeDef.bCanDoPlayerRespawn` and `bCanBePlayerCheckpoint`): fast travel
stations, respawn stations (`ResurrectTravelStationObject`, `RS_*`) and level exits; not holding,
cinematic or send-only stations. Only stations streamed in have an actor, and the server streams
around every player, so on the host the nearest one to any player is usually loaded.

Every station has `CharacterSpawnComponents`: four spots in a row in front of it, one per player
(Instant Fast Travel's land.py measured them). The player goes on the first one nobody stands on.

With no respawn station loaded, the game's own trip to the player's respawn station
(`OakPlayerController.ResurrectStation`) with the portal.
"""

from __future__ import annotations

import math
from typing import Any

import unrealsdk
from mods_base import ENGINE
from unrealsdk import logging
from unrealsdk.unreal import UObject, WrappedStruct

SPOT_TAKEN = 60.0  # another player's pawn this close to a spot has it

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_station_class = unrealsdk.find_class("TravelStationObject")


def _vec(x: float, y: float, z: float) -> Any:
    return unrealsdk.make_struct("Vector", X=x, Y=y, Z=z)


def _can_respawn(actor: UObject) -> bool:
    try:
        kind = actor.NexusData.TypeDef
        return bool(kind.bCanDoPlayerRespawn and kind.bCanBePlayerCheckpoint
                    and not kind.bIsHoldingStation and not kind.bSendOnly)  # fmt: skip
    except Exception:  # noqa: BLE001 - unresolved def: not one we can judge
        return False


def nearest_station(pawn: UObject) -> UObject | None:
    """The closest loaded respawn station to the character (~0.1-0.3 ms)."""
    here = pawn.K2_GetActorLocation()
    best, best_d = None, math.inf
    _, actors = _statics.GetAllActorsOfClass(ENGINE.GameViewport.World, _station_class, [])
    for actor in actors:
        if actor is None or not _can_respawn(actor):
            continue
        where = actor.K2_GetActorLocation()
        d = (where.X - here.X) ** 2 + (where.Y - here.Y) ** 2 + (where.Z - here.Z) ** 2
        if d < best_d:
            best, best_d = actor, d
    return best


def _others(pawn: UObject) -> list[Any]:
    out = []
    state = ENGINE.GameViewport.World.GameState
    for ps in list(state.PlayerArray) if state is not None else []:
        other = ps.PawnPrivate if ps is not None else None
        if other is not None and other != pawn:
            out.append(other.K2_GetActorLocation())
    return out


def _free_spot(pawn: UObject, actor: UObject) -> tuple[float, float, float, float] | None:
    spots = []
    for comp in actor.CharacterSpawnComponents:
        if comp is None:
            continue
        where, rot = comp.K2_GetComponentLocation(), comp.K2_GetComponentRotation()
        spots.append((where.X, where.Y, where.Z, rot.Yaw))
    if not spots:
        return None
    others = _others(pawn)
    for spot in spots:
        if all(math.hypot(o.X - spot[0], o.Y - spot[1]) > SPOT_TAKEN or abs(o.Z - spot[2]) > 200 for o in others):
            return spot
    return spots[0]


def _put(pc: UObject, pawn: UObject, spot: tuple[float, float, float, float]) -> bool:
    x, y, z, yaw = spot
    facing = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=yaw, Roll=0)
    pawn.CharacterMovement.StopMovementImmediately()
    if not pawn.K2_TeleportTo(_vec(x, y, z), facing):
        return False
    try:
        if pc.IsLocalController():
            pc.SetControlRotation(facing, False)
        else:
            pc.ClientSetRotation(facing, True)
    except Exception as ex:  # noqa: BLE001 - facing is a nicety
        logging.dev_warning(f"[Unstuck] facing: {ex!r}")
    return True


def game_respawn_trip(pc: UObject) -> str | None:
    """The game's own trip to the player's respawn station (portal and all). Its name, or None."""
    station = pc.ResurrectStation
    name = str(getattr(station, "_name", "") or "")
    if not name or name.lower() == "none":
        return None
    fn = pc.RequestTeleportToStation
    args = WrappedStruct(fn.func)
    args.station = station
    args.bForRespawn = False
    args.bLocalOnly = False
    fn(args)
    return name


def place(pc: UObject) -> dict[str, Any]:
    """
    Puts pc's character on the nearest respawn station. Runs where positions belong (solo / host).
    Returns {"ok": True, "station": name, "how": "spot" | "trip"} or {"ok": False, "error": ...}.
    """
    pawn = pc.Pawn
    if pawn is None or getattr(pawn, "CharacterMovement", None) is None:
        return {"ok": False, "error": "no character"}
    actor = nearest_station(pawn)
    if actor is not None:
        spot = _free_spot(pawn, actor)
        if spot is not None and _put(pc, pawn, spot):
            return {"ok": True, "station": str(actor.NexusData._name), "how": "spot"}
    name = game_respawn_trip(pc)
    if name is None:
        return {"ok": False, "error": "no respawn station"}
    return {"ok": True, "station": name, "how": "trip"}
