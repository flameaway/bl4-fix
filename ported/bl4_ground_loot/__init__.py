"""
Ground Loot Helpers for Borderlands 4: one key brings the loot lying around to you (pickups at
your feet, gear in rings around you), another clears it away.

A port of Yeti's Ground Loot Helpers (GPL-3.0, see ported/README.md).

Only loot the game has loaded can move, and that doesn't reach very far. Item of the Day displays
in vending machines are left alone. Clearing drops the loot far below the map; it can leave a
marker on the radar for a while.

Co-op: loot is per player, and each key only moves the loot of the player who pressed it (gear
whose Owner is their controller, plus shared pickups). Loot lives in the host's world: a client
moving its own copy would only fool its screen, so a client's press asks the host's copy of this
mod to move it (BL4 Direct's co-op channel). So the host needs the mod for anyone to use it, and
each player who wants the keys needs it too.
"""

from __future__ import annotations

import math
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, SliderOption, build_mod, get_pc, keybind
from unrealsdk import logging, make_struct
from unrealsdk.unreal import IGNORE_STRUCT, UObject

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host behavior only
    coop = None

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

FEATURE = "ground_loot"
SERVICE = "ground_loot.move"

RING_FIRST = 200.0  # cm from you to the first ring of gear
RING_GAP = 150.0
RING_SPACING = 40.0  # cm of ring per item
FEET = 40.0  # cm below the pawn's origin
# Material name parts of pickups without body data (ammo, health, money, ...)
PICKUP_MATERIALS = ("Health", "ArmorShard", "Ammo", "ammo", "Money", "Eridium", "ShieldBooster")

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_pickup_class = unrealsdk.find_class("InventoryPickup")
_vendor_class = unrealsdk.find_class("OakVendingMachine")


def log(msg: str) -> None:
    logging.info(f"[Ground Loot] {msg}")


pickups_option = BoolOption(
    "Pickups",
    True,
    description=(
        "Both keys include ammo, health, shield boosters, money and Eridium (brought to your feet)."
    ),
)
gear_option = BoolOption(
    "Gear",
    True,
    description="Both keys include weapons and gear (brought in rings around you).",
)
range_option = SliderOption(
    "Range (m)",
    0,
    0,
    300,
    10,
    description="Only loot this close moves (both keys). 0 = all loot the game has loaded.",
)


def _is_pickup(item: UObject) -> bool | None:
    """True for ammo/health/money pickups, False for gear, None for anything else."""
    body = str(item.BodyData)
    if "Pickups" in body:
        return True
    if item.BodyData is None:
        prim = item.RootPrimitiveComponent
        for i in range(prim.GetNumMaterials()):
            material = prim.GetMaterial(i)
            if material is not None and any(part in material.Name for part in PICKUP_MATERIALS):
                return True
    if "Gear" in body:
        return False
    return None


def _ground_loot(pc: UObject, origin: Any, options: dict[str, Any]) -> tuple[list[UObject], list[UObject]]:
    """(pickups, gear) lying around for this player, nearest first, as the options choose."""
    world = ENGINE.GameViewport.World
    shown: set[int] = set()
    _, vendors = _statics.GetAllActorsOfClass(world, _vendor_class, [])
    for vendor in vendors:
        if vendor is not None and (item := vendor.GetIOTDForPlayer(pc)) is not None:
            shown.add(item._get_address())

    max_dist = float(options.get("range", 0)) * 100.0
    found: list[tuple[float, bool, UObject]] = []
    _, items = _statics.GetAllActorsOfClass(world, _pickup_class, [])
    for item in items:
        if item is None or item.RootPrimitiveComponent is None or item._get_address() in shown:
            continue
        # In co-op the host has every player's loot: gear drops for one player (its Owner is
        # their controller); ammo, health and money have no owner
        owner = item.Owner
        if owner is not None and owner != pc:
            continue
        where = item.K2_GetActorLocation()
        dist = math.dist((where.X, where.Y, where.Z), (origin.X, origin.Y, origin.Z))
        if max_dist and dist > max_dist:
            continue
        kind = _is_pickup(item)
        if kind is not None:
            found.append((dist, kind, item))
    found.sort(key=lambda entry: entry[0])
    pickups = [i for _, k, i in found if k] if options.get("pickups", True) else []
    gear = [i for _, k, i in found if not k] if options.get("gear", True) else []
    return pickups, gear


def _move(item: UObject, x: float, y: float, z: float, yaw: float | None = None) -> None:
    item.RootPrimitiveComponent.SetSimulatePhysics(True)  # so it settles on the ground
    rotation = IGNORE_STRUCT if yaw is None else make_struct("Rotator", Pitch=0, Yaw=yaw, Roll=0)
    item.K2_TeleportTo(make_struct("Vector", X=x, Y=y, Z=z), rotation)


def _bring(pc: UObject, options: dict[str, Any]) -> dict[str, Any]:
    pawn = pc.Pawn
    if pawn is None:
        return {"ok": False, "error": "no character"}
    here = pawn.K2_GetActorLocation()
    facing = math.radians(pawn.K2_GetActorRotation().Yaw)
    pickups, gear = _ground_loot(pc, here, options)
    for item in pickups:
        _move(item, here.X, here.Y, here.Z - FEET)

    # Rings around you, starting straight ahead; each ring holds as many as fit its length
    placed, ring = 0, 0
    while placed < len(gear):
        radius = RING_FIRST + ring * RING_GAP
        count = min(len(gear) - placed, max(1, int(2 * math.pi * radius / RING_SPACING)))
        for slot in range(count):
            angle = facing + 2 * math.pi * slot / count
            x, y = here.X + radius * math.cos(angle), here.Y + radius * math.sin(angle)
            # Faces away from you, like the game lays out a boss's drops
            _move(gear[placed], x, y, here.Z - FEET, math.degrees(angle))
            placed += 1
        ring += 1
    return {"ok": True, "text": f"brought {len(pickups)} pickup(s) and {len(gear)} item(s)"}


def _clear(pc: UObject, options: dict[str, Any]) -> dict[str, Any]:
    pawn = pc.Pawn
    if pawn is None:
        return {"ok": False, "error": "no character"}
    pickups, gear = _ground_loot(pc, pawn.K2_GetActorLocation(), options)
    for item in pickups + gear:
        _move(item, 100000.0, 100000.0, -1.0e9)
    return {"ok": True, "text": f"cleared {len(pickups) + len(gear)} item(s)"}


def _serve(req: Any) -> dict[str, Any]:
    """Host side (also our own presses): moves the asking player's loot."""
    if not mod.is_enabled:
        return {"ok": False, "error": "Ground Loot is off on the host"}
    action = _bring if req.data.get("do") == "bring" else _clear
    return action(req.pc, req.data)


def _options(do: str) -> dict[str, Any]:
    return {"do": do, "pickups": pickups_option.value, "gear": gear_option.value, "range": range_option.value}


def _done(reply: dict[str, Any]) -> None:
    if reply.get("ok"):
        log(reply.get("text", "done"))
    else:
        log(f"couldn't move the loot: {reply.get('error')}")


def _press(do: str) -> None:
    options = _options(do)
    if coop is None:
        pc = get_pc()
        if pc is not None:
            _done((_bring if do == "bring" else _clear)(pc, options))
        return
    # Solo or host: right here. Co-op client: loot belongs to the host's world, so the host's
    # mod moves it (moving it here would only move our copy)
    if not coop.run(SERVICE, options, _done, feature_name=FEATURE):
        log("in co-op the host needs Ground Loot to move your loot")


@keybind("Bring loot to me", description="Brings the loot lying around to you.")
def bring_key() -> None:
    try:
        _press("bring")
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Ground Loot] {ex!r}")


@keybind("Clear loot", description="Sends the loot lying around far below the map.")
def clear_key() -> None:
    try:
        _press("clear")
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Ground Loot] {ex!r}")


mod = build_mod(
    options=[pickups_option, gear_option, range_option],
    keybinds=[bring_key, clear_key],
    hooks=[],
)

if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
    coop.handle(SERVICE, _serve)
