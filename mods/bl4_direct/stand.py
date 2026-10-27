"""
Where to put a player who travels to a Big Encore machine (Item Cards, UVH Boss Teleport,
Kairosdex): in front of it, facing it, never inside a wall. Runs where the player's position
belongs: in solo / as host for anyone, through coop.teleport_near for a client.

Why it's two steps: a same-map trip can land far from anything loaded. World partition hasn't
streamed the arena in yet, so nothing there collides and any spot "fits"; the old ring of spots
around the machine took the first one (world east of it, often behind it), and the rock streamed
in around the player. So:

1. At once: the spot in front of the machine, from its baked facing (`yaw`, the marker's actor
   rotation; its front, with the name label, is its +X axis), checked like step 2 if the area is
   already loaded.
2. Then, a few times a second for up to SETTLE_S (retrying while more of the area streams in):
   once the machine actor (found by the UAID in its marker name) has streamed in, check where the
   player stands with the pawn's collision: a clear line from the machine, a walkable floor under
   them, room for their capsule. If that fails, try spots around the front (then the sides, the
   back, further out) and move them to the first good one. Until the area loads, a player who
   falls more than FALL_RESET below the spot is put back on it.

The machine actor is the "loaded" signal, not a floor trace: 49 km from a machine the terrain
under it already collides while the rocks around it don't exist yet (measured 2026-10-27).
A check costs ~0.23 ms per spot tried (3 traces); the front spot usually passes, all 56 failing
would be ~13 ms once, at the moment of a teleport.
"""

from __future__ import annotations

import math
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

# Spots tried, in order: degrees off the machine's front, then distance from it
ANGLES = (0, 25, -25, 50, -50, 75, -75, 100, -100, 130, -130, 155, -155, 180)
DISTANCES = (170, 250, 340, 450)
NO_YAW_DISTANCES = (300, 450, 600)  # data without a facing: the old rings
EYE = 90.0  # height above the floor of the line checked from the machine
CLEAR_OF_MACHINE = 70.0  # the line starts this far out from the machine's centre, outside its body
FLOOR_UP, FLOOR_DOWN = 250.0, 400.0  # floor search around the machine's base height
MIN_FLOOR_NORMAL = 0.7  # ~45 degrees: steeper isn't floor
LIFT = 5.0  # capsule bottom above the floor
FIT_TO = 40.0  # a teleport the game moved further than this didn't land where we checked
SETTLE_S = 12.0
CHECK_EVERY_S = 0.2
FALL_RESET = 300.0
PROFILE = "Pawn"

_kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_interactive = unrealsdk.find_class("OakInteractiveObject")


class _Settle:
    """A player we put somewhere before the area loaded."""

    def __init__(self, pawn: UObject, pc: UObject, target: dict[str, Any], spot: Any) -> None:
        self.pawn = WeakPointer(pawn)
        self.pc = WeakPointer(pc)
        self.target = target
        self.spot = spot  # (x, y, z, yaw) we put them at
        self.until = time.monotonic() + SETTLE_S


class S:
    settling: list[_Settle] = []
    next_check = 0.0


def _vec(x: float, y: float, z: float) -> Any:
    return unrealsdk.make_struct("Vector", X=x, Y=y, Z=z)


def _color() -> Any:
    return unrealsdk.make_struct("LinearColor")


def _line(start: Any, end: Any, ignore: list[UObject]) -> Any | None:
    """The blocking hit between two points for a pawn, or None."""
    hit = unrealsdk.make_struct("HitResult")
    ret = _kismet.LineTraceSingleByProfile(ENGINE.GameViewport.World, start, end, PROFILE, False, ignore, 0, hit,
                                           True, _color(), _color(), 0)  # fmt: skip
    ok, hit = (ret[0], ret[-1]) if isinstance(ret, tuple) else (ret, hit)
    return hit if ok and hit.bBlockingHit else None


def _capsule_free(pawn: UObject, x: float, y: float, z: float, ignore: list[UObject]) -> bool:
    capsule = pawn.CapsuleComponent
    hit = unrealsdk.make_struct("HitResult")
    ret = _kismet.CapsuleTraceSingleByProfile(ENGINE.GameViewport.World, _vec(x, y, z), _vec(x, y, z + 1),
                                              capsule.GetScaledCapsuleRadius(), capsule.GetScaledCapsuleHalfHeight(),
                                              PROFILE, False, ignore, 0, hit, True, _color(), _color(), 0)  # fmt: skip
    ok = ret[0] if isinstance(ret, tuple) else ret
    return not ok


def _floor(x: float, y: float, base_z: float, ignore: list[UObject]) -> float | None:
    hit = _line(_vec(x, y, base_z + FLOOR_UP), _vec(x, y, base_z - FLOOR_DOWN), ignore)
    if hit is None or hit.ImpactNormal.Z < MIN_FLOOR_NORMAL:
        return None
    return float(hit.ImpactPoint.Z)


def _machine(target: dict[str, Any]) -> UObject | None:
    """The machine's actor if it's loaded: matched by the UAID its marker name ends with."""
    location = str(target.get("location") or "").lower()
    at = location.find("uaid_")
    if at < 0:
        return None
    uaid = location[at:]
    _, actors = _statics.GetAllActorsOfClass(ENGINE.GameViewport.World, _interactive, [])
    return next((a for a in actors if str(a.Name).lower().endswith(uaid)), None)


def _facing(target: dict[str, Any], machine: UObject | None) -> tuple[float, float, float, float | None]:
    """Machine position and facing: the live actor's when loaded, else the baked data."""
    if machine is not None:
        where, rot = machine.K2_GetActorLocation(), machine.K2_GetActorRotation()
        return where.X, where.Y, where.Z, rot.Yaw
    yaw = target.get("yaw")
    return float(target["x"]), float(target["y"]), float(target["z"]), None if yaw is None else float(yaw)


def _candidates(mx: float, my: float, yaw: float | None) -> list[tuple[float, float]]:
    if yaw is None:
        return [(mx + r * math.cos(a * math.pi / 4), my + r * math.sin(a * math.pi / 4))
                for r in NO_YAW_DISTANCES for a in range(8)]  # fmt: skip
    out = []
    for off in ANGLES:
        a = math.radians(yaw + off)
        out += [(mx + d * math.cos(a), my + d * math.sin(a)) for d in DISTANCES]
    return out


def _good_spot(pawn: UObject, target: dict[str, Any], machine: UObject | None, x: float, y: float,
               ignore: list[UObject]) -> float | None:  # fmt: skip
    """The capsule centre height to stand at (x, y), or None if it's not a good spot."""
    mx, my, mz, _ = _facing(target, machine)
    floor = _floor(x, y, mz, ignore)
    if floor is None:
        return None
    half = pawn.CapsuleComponent.GetScaledCapsuleHalfHeight()
    # Nothing between the machine and the spot: no wall, cliff face or fence in between
    away = math.hypot(x - mx, y - my) or 1.0
    sx, sy = mx + (x - mx) / away * CLEAR_OF_MACHINE, my + (y - my) / away * CLEAR_OF_MACHINE
    if _line(_vec(sx, sy, mz + EYE), _vec(x, y, floor + EYE), ignore) is not None:
        return None
    z = floor + half + LIFT
    return z if _capsule_free(pawn, x, y, z, ignore) else None


def _face(pc: UObject, pawn: UObject, x: float, y: float, z: float, mx: float, my: float) -> bool:
    yaw = math.degrees(math.atan2(my - y, mx - x))
    facing = unrealsdk.make_struct("Rotator", Pitch=0, Yaw=yaw, Roll=0)
    if not pawn.K2_TeleportTo(_vec(x, y, z), facing):
        return False
    try:
        if pc.IsLocalController():
            pc.SetControlRotation(facing)
        else:
            pc.ClientSetRotation(facing, True)
    except Exception as ex:  # noqa: BLE001 - facing is a nicety
        logging.dev_warning(f"[Stand] facing: {ex!r}")
    return True


def _place_checked(pc: UObject, pawn: UObject, target: dict[str, Any], machine: UObject | None) -> bool:
    """Moves the player to the first good spot by the machine. False if there's none."""
    mx, my, _, yaw = _facing(target, machine)
    ignore = [pawn] + ([machine] if machine is not None else [])
    for x, y in _candidates(mx, my, yaw):
        z = _good_spot(pawn, target, machine, x, y, ignore)
        if z is None or not _face(pc, pawn, x, y, z, mx, my):
            continue
        now = pawn.K2_GetActorLocation()
        if math.hypot(now.X - x, now.Y - y) <= FIT_TO and abs(now.Z - z) <= FIT_TO:
            return True
    return False


def _standing_well(pawn: UObject, target: dict[str, Any], machine: UObject | None) -> bool:
    now = pawn.K2_GetActorLocation()
    ignore = [pawn] + ([machine] if machine is not None else [])
    mx, my, mz, _ = _facing(target, machine)
    if math.hypot(now.X - mx, now.Y - my) > max(DISTANCES) + 100 or abs(now.Z - mz) > FLOOR_DOWN:
        return False
    return _good_spot(pawn, target, machine, now.X, now.Y, ignore) is not None


def place(pc: UObject, target: dict[str, Any]) -> dict[str, Any]:
    """
    Puts pc's character in front of target's machine ({"x", "y", "z", "yaw"?, "location"?}).
    Returns {"ok": True} or {"ok": False, "error": "no character" | "no room"}.
    """
    pawn = pc.Pawn
    if pawn is None:
        return {"ok": False, "error": "no character"}
    S.settling = [s for s in S.settling if s.pawn() is not None and s.pawn() != pawn]
    machine = _machine(target)
    if machine is not None:
        return {"ok": True} if _place_checked(pc, pawn, target, machine) else {"ok": False, "error": "no room"}

    # Nothing loaded there yet: the front spot by the data, checked again once the area streams in
    mx, my, mz, yaw = _facing(target, None)
    x, y = _candidates(mx, my, yaw)[0]
    z = mz + pawn.CapsuleComponent.GetScaledCapsuleHalfHeight() + 20
    if not _face(pc, pawn, x, y, z, mx, my):
        return {"ok": False, "error": "no room"}
    S.settling.append(_Settle(pawn, pc, target, (x, y, z)))
    return {"ok": True}


def tick() -> None:
    """Call every frame (cheap when nobody is settling): finishes placements once areas load."""
    if not S.settling:
        return
    now = time.monotonic()
    if now < S.next_check:
        return
    S.next_check = now + CHECK_EVERY_S
    keep = []
    for job in S.settling:
        pawn, pc = job.pawn(), job.pc()
        if pawn is None or pc is None or pc.Pawn != pawn:
            continue
        try:
            machine = _machine(job.target)
            if machine is None:
                if now < job.until:
                    x, y, z = job.spot
                    if pawn.K2_GetActorLocation().Z < z - FALL_RESET:
                        pawn.CharacterMovement.StopMovementImmediately()
                        mx, my, _, _ = _facing(job.target, None)
                        _face(pc, pawn, x, y, z, mx, my)
                    keep.append(job)
                else:
                    logging.warning(f"[Stand] {job.target.get('location')} never loaded: left at its front")
                continue
            if _standing_well(pawn, job.target, machine) or _place_checked(pc, pawn, job.target, machine):
                continue
            if now < job.until:
                keep.append(job)  # more of the area may still be streaming in
            else:
                logging.warning(f"[Stand] no good spot found by {job.target.get('location')}")
        except Exception as ex:  # noqa: BLE001 - never break the caller's tick
            logging.error(f"[Stand] {ex!r}")
    S.settling = keep


def cancel() -> None:
    S.settling = []
