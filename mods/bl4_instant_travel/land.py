"""
Putting a player on a fast travel station of the map they're on, without the portal.

Where the game puts you: every travel station actor (`TravelStationObject`) has
`CharacterSpawnComponents`, four `TravelPointCapsuleComponent`s in a row 150 units in front of the
station, ~95 apart, their centres 105 above its base, facing the station's way (measured on two
World_P stations, 2026-10-27). One per player, so friends arriving together don't stack.

A station across the map isn't streamed in yet when we jump: its actor doesn't exist and the ground
there may not collide. Same approach as BL4 Direct's stand.py: jump at once to where its first spot
will be (stations.json, from the game data), then a few times a second
until the station actor streams in, move the player onto a free spawn spot (usually a no-op: the
spot was right), and put them back if they fall before the ground loads.
"""

from __future__ import annotations

import math
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

# Where the first spawn spot is relative to the station's base, from its facing
SPOT_AHEAD = 150.0
SPOT_UP = 105.0
SPOT_STEP = 95.0  # sideways, between spots
SPOT_TAKEN = 60.0  # another player's pawn this close to a spot has it
SETTLE_S = 12.0
CHECK_EVERY_S = 0.2
FALL_RESET = 300.0
ON_SPOT = 150.0  # already this close to a spawn spot: leave them be

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_station_class = unrealsdk.find_class("TravelStationObject")


class Target:
    """A station to land on: its def name and where it stands (stations.json)."""

    def __init__(self, name: str, x: float, y: float, z: float, yaw: float) -> None:
        self.name = name
        self.x, self.y, self.z, self.yaw = x, y, z, yaw

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "x": self.x, "y": self.y, "z": self.z, "yaw": self.yaw}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Target:
        return Target(str(data["name"]), float(data["x"]), float(data["y"]), float(data["z"]), float(data["yaw"]))


class _Settle:
    def __init__(self, pc: UObject, pawn: UObject, target: Target, spot: tuple[float, float, float]) -> None:
        self.pc = WeakPointer(pc)
        self.pawn = WeakPointer(pawn)
        self.target = target
        self.spot = spot
        self.until = time.monotonic() + SETTLE_S


class S:
    settling: list[_Settle] = []
    next_check = 0.0


def _vec(x: float, y: float, z: float) -> Any:
    return unrealsdk.make_struct("Vector", X=x, Y=y, Z=z)


def station_actor(name: str) -> UObject | None:
    """The station's actor if it's streamed in (~0.15 ms)."""
    want = name.lower()
    _, actors = _statics.GetAllActorsOfClass(ENGINE.GameViewport.World, _station_class, [])
    for actor in actors:
        handle = actor.NexusData
        if handle is not None and str(handle._name).lower() == want:
            return actor
    return None


def _others(pawn: UObject) -> list[Any]:
    """Where the other players' characters are."""
    out = []
    state = ENGINE.GameViewport.World.GameState
    for ps in list(state.PlayerArray) if state is not None else []:
        other = ps.PawnPrivate if ps is not None else None
        if other is not None and other != pawn:
            out.append(other.K2_GetActorLocation())
    return out


def _free_spot(pawn: UObject, actor: UObject) -> tuple[float, float, float, float] | None:
    """The first spawn spot of a loaded station no other player stands on: (x, y, z, yaw)."""
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


def _guess_spot(target: Target, slot: int) -> tuple[float, float, float, float]:
    """Where spawn spot `slot` will be once the station streams in, from its data."""
    a = math.radians(target.yaw)
    side = (1.5 - slot) * SPOT_STEP
    x = target.x + SPOT_AHEAD * math.cos(a) - side * math.sin(a)
    y = target.y + SPOT_AHEAD * math.sin(a) + side * math.cos(a)
    return x, y, target.z + SPOT_UP, target.yaw


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
        logging.dev_warning(f"[Instant Travel] facing: {ex!r}")
    return True


def place(pc: UObject, target: Target) -> dict[str, Any]:
    """
    Puts pc's character on the station. Runs where positions belong (solo / host).
    Returns {"ok": True} or {"ok": False, "error": "no character" | "no room"}.
    """
    pawn = pc.Pawn
    if pawn is None:
        return {"ok": False, "error": "no character"}
    S.settling = [s for s in S.settling if s.pawn() is not None and s.pawn() != pawn]
    actor = station_actor(target.name)
    if actor is not None:
        spot = _free_spot(pawn, actor)
        if spot is not None:
            return {"ok": True} if _put(pc, pawn, spot) else {"ok": False, "error": "no room"}

    # Not streamed in yet: where a free spot will be, checked again once it loads
    others = _others(pawn)
    slot = next((i for i in range(4) if all(math.hypot(o.X - _guess_spot(target, i)[0], o.Y - _guess_spot(target, i)[1])
                                             > SPOT_TAKEN for o in others)), 0)  # fmt: skip
    spot = _guess_spot(target, slot)
    if not _put(pc, pawn, spot):
        return {"ok": False, "error": "no room"}
    S.settling.append(_Settle(pc, pawn, target, spot[:3]))
    return {"ok": True}


def _settle(job: _Settle, now: float) -> bool:
    """One check of a player we landed before the station loaded. True to keep checking."""
    pawn, pc = job.pawn(), job.pc()
    if pawn is None or pc is None or pc.Pawn != pawn:
        return False
    actor = station_actor(job.target.name)
    if actor is None:
        if now >= job.until:
            logging.warning(f"[Instant Travel] {job.target.name} never streamed in: left at its front")
            return False
        x, y, z = job.spot
        if pawn.K2_GetActorLocation().Z < z - FALL_RESET:
            _put(pc, pawn, (x, y, z, job.target.yaw))
        return True
    spot = _free_spot(pawn, actor)
    if spot is None:
        return False
    here = pawn.K2_GetActorLocation()
    if math.hypot(here.X - spot[0], here.Y - spot[1]) > ON_SPOT or abs(here.Z - spot[2]) > ON_SPOT:
        _put(pc, pawn, spot)
    return False


def tick() -> None:
    """Every frame (cheap with nobody settling): finishes landings once stations stream in."""
    if not S.settling:
        return
    now = time.monotonic()
    if now < S.next_check:
        return
    S.next_check = now + CHECK_EVERY_S
    keep = []
    for job in S.settling:
        try:
            if _settle(job, now):
                keep.append(job)
        except Exception as ex:  # noqa: BLE001 - never break the caller's tick
            logging.error(f"[Instant Travel] {ex!r}")
    S.settling = keep


def cancel() -> None:
    S.settling = []
