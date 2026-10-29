"""
Ground Loot Helpers for Borderlands 4: one key brings the loot lying around to you (pickups at
your feet, gear in rings around you), another clears it away.

A port of Yeti's Ground Loot Helpers (GPL-3.0, see ported/README.md).

Only loot the game has loaded can move, and that doesn't reach very far. Item of the Day displays
in vending machines are left alone. Clearing drops the loot far below the map; it can leave a
marker on the radar for a while.

Co-op: loot isn't replicated. Every player's game spawns its own loot (gear and pickups) and only
that game can move it, so each key moves this game's loot right here, whatever the role. With
"Co-op partners' loot" on, Bring also asks the other players' copies of this mod to bring their
loot to the same spot (the host passes a friend's ask on to everyone else, over BL4 Direct's
co-op channel). Players without the mod keep their loot where it fell.
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

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

# v0.1 had "ground_loot" + "ground_loot.move" (a client asked the host to move loot the host
# doesn't have); new names so the two versions ignore each other
FEATURE = "ground_loot.share"
SERVICE = "ground_loot.gather"  # a friend pressed Bring: host brings its loot, tells everyone else
PUSH = "ground_loot.bring_to"  # host -> a friend: bring your loot to this spot

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
partners_option = BoolOption(
    "Co-op partners' loot",
    True,
    description=(
        "In co-op, Bring loot also brings the other players' loot to the same spot, and a partner's"
        " Bring brings yours to them (players who run Ground Loot). Clear loot only clears your own."
    ),
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


def _players(pc: UObject, partners: bool) -> list[UObject]:
    """The controllers whose loot moves: the presser, plus (on the host) everyone else."""
    players = [pc]
    if partners and coop is not None:
        host = get_pc()
        for other in ([host] if host is not None else []) + coop.remote_controllers():
            if all(other != p for p in players):
                players.append(other)
    return players


def _ground_loot(origin: Any, max_m: float) -> tuple[list[UObject], list[UObject]]:
    """(pickups, gear) lying around in this game, nearest first. All of it is ours: loot isn't
    replicated, every player's game spawns its own."""
    world = ENGINE.GameViewport.World
    pc = get_pc()
    shown: set[int] = set()
    _, vendors = _statics.GetAllActorsOfClass(world, _vendor_class, [])
    for vendor in vendors:
        if vendor is not None and pc is not None and (item := vendor.GetIOTDForPlayer(pc)) is not None:
            shown.add(item._get_address())

    max_dist = max_m * 100.0
    found: list[tuple[float, bool, UObject]] = []
    _, items = _statics.GetAllActorsOfClass(world, _pickup_class, [])
    for item in items:
        if item is None or item.RootPrimitiveComponent is None or item._get_address() in shown:
            continue
        where = item.K2_GetActorLocation()
        dist = math.dist((where.X, where.Y, where.Z), (origin.X, origin.Y, origin.Z))
        if max_dist and dist > max_dist:
            continue
        kind = _is_pickup(item)
        if kind is not None:
            found.append((dist, kind, item))
    found.sort(key=lambda entry: entry[0])
    pickups = [i for _, k, i in found if k] if pickups_option.value else []
    gear = [i for _, k, i in found if not k] if gear_option.value else []
    return pickups, gear


def _pawn() -> UObject | None:
    pc = get_pc()
    return pc.Pawn if pc is not None else None


def _move(item: UObject, x: float, y: float, z: float, yaw: float | None = None) -> None:
    item.RootPrimitiveComponent.SetSimulatePhysics(True)  # so it settles on the ground
    rotation = IGNORE_STRUCT if yaw is None else make_struct("Rotator", Pitch=0, Yaw=yaw, Roll=0)
    item.K2_TeleportTo(make_struct("Vector", X=x, Y=y, Z=z), rotation)


def _bring_to(x: float, y: float, z: float, facing_deg: float) -> str:
    """Brings this game's loot (lying around our own character) to the spot x, y, z."""
    pawn = _pawn()
    if pawn is None:
        return "no character"
    pickups, gear = _ground_loot(pawn.K2_GetActorLocation(), float(range_option.value))
    for item in pickups:
        _move(item, x, y, z - FEET)

    # Rings around the spot, starting straight ahead; each ring holds as many as fit its length
    facing = math.radians(facing_deg)
    placed, ring = 0, 0
    while placed < len(gear):
        radius = RING_FIRST + ring * RING_GAP
        count = min(len(gear) - placed, max(1, int(2 * math.pi * radius / RING_SPACING)))
        for slot in range(count):
            angle = facing + 2 * math.pi * slot / count
            gx, gy = x + radius * math.cos(angle), y + radius * math.sin(angle)
            # Faces away from the spot, like the game lays out a boss's drops
            _move(gear[placed], gx, gy, z - FEET, math.degrees(angle))
            placed += 1
        ring += 1
    return f"brought {len(pickups)} pickup(s) and {len(gear)} item(s)"


def _clear() -> str:
    pawn = _pawn()
    if pawn is None:
        return "no character"
    pickups, gear = _ground_loot(pawn.K2_GetActorLocation(), float(range_option.value))
    for item in pickups + gear:
        _move(item, 100000.0, 100000.0, -1.0e9)
    return f"cleared {len(pickups) + len(gear)} item(s)"


def _share(spot: dict[str, Any], skip: UObject | None = None) -> int:
    """Host: tells every other modded player to bring their loot to the spot. Returns how many."""
    sent = 0
    for pc in coop.players_with(FEATURE):
        if skip is None or pc != skip:
            coop.push(pc, PUSH, spot)
            sent += 1
    return sent


def _serve(req: Any) -> dict[str, Any]:
    """Host: a friend pressed Bring. Our own loot goes to them, and everyone else's too."""
    if not mod.is_enabled or not partners_option.value:
        return {"ok": False, "error": "the host's Ground Loot doesn't share loot"}
    spot = {k: float(req.data[k]) for k in ("x", "y", "z", "yaw")}
    text = _bring_to(spot["x"], spot["y"], spot["z"], spot["yaw"])
    others = _share(spot, skip=req.pc)
    log(f"{text} to a partner")
    return {"ok": True, "text": f"the host {text}; asked {others} other player(s)"}


def _on_push(data: dict[str, Any]) -> None:
    """Friend: a partner pressed Bring; bring our loot to their spot."""
    if mod.is_enabled and partners_option.value:
        log(f"{_bring_to(float(data['x']), float(data['y']), float(data['z']), float(data['yaw']))} to a partner")


def _done(reply: dict[str, Any]) -> None:
    if reply.get("ok"):
        log(reply.get("text", "done"))
    else:
        log(f"partners' loot: {reply.get('error')}")


def _press(do: str) -> None:
    if do == "clear":
        log(_clear())
        return
    pawn = _pawn()
    if pawn is None:
        return
    here = pawn.K2_GetActorLocation()
    spot = {"x": here.X, "y": here.Y, "z": here.Z, "yaw": pawn.K2_GetActorRotation().Yaw}
    log(_bring_to(spot["x"], spot["y"], spot["z"], spot["yaw"]))
    if coop is None or not partners_option.value:
        return
    role = coop.role()
    if role == "host":
        log(f"asked {_share(spot)} partner(s) to bring theirs")
    elif role == "client":
        # Only the host can reach the other players: it brings its own and passes the ask on
        if not coop.run(SERVICE, spot, _done, feature_name=FEATURE):
            log("the host doesn't run Ground Loot 0.2+, so only your own loot came")


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
    options=[pickups_option, gear_option, range_option, partners_option],
    keybinds=[bring_key, clear_key],
    hooks=[],
)

if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
    coop.handle(SERVICE, _serve)
    coop.on_push(PUSH, _on_push)
