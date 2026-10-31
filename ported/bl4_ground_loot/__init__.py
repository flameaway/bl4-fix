"""
Ground Loot Helpers for Borderlands 4: one key brings the loot lying around to you (pickups at
your feet, gear in rings around you), another clears it away.

A port of Yeti's Ground Loot Helpers (GPL-3.0, see ported/README.md). Modified by flameaway from
2026-10-24 (changes since in git history).

Only loot the game has loaded can move, and that doesn't reach very far. Item of the Day displays
in vending machines are left alone. Clearing drops the loot far below the map; it can leave a
marker on the radar for a while.

Co-op: loot isn't replicated. Every player's game spawns its own loot (gear and pickups) and only
that game can move it, so each key moves this game's loot right here, whatever the role. With
"Co-op partners' loot" on, Bring also asks the other players' copies of this mod to bring their
loot to the same spot (the host passes a friend's ask on to everyone else, over BL4 Direct's
co-op channel). Players without the mod keep their loot where it fell.

The game destroys its loot when it comes to rest more than ~150 m from that game's own character,
so loot only goes to a spot within SAFE_M of this game's character (a partner farther away gets a
"come closer" answer instead), and moved gear is watched for a few seconds afterwards.
"""

from __future__ import annotations

import math
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, SliderOption, build_mod, get_pc, keybind
from unrealsdk import logging, make_struct
from unrealsdk.unreal import IGNORE_STRUCT, UObject, WeakPointer

from . import frametick

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host behavior only
    coop = None

__version__ = "0.2.1"
__version_info__ = (0, 2, 1)

# v0.1 had "ground_loot" + "ground_loot.move" (a client asked the host to move loot the host
# doesn't have); new names so the two versions ignore each other
FEATURE = "ground_loot.share"
SERVICE = "ground_loot.gather"  # a friend pressed Bring: host brings its loot, tells everyone else
PUSH = "ground_loot.bring_to"  # host -> a friend: bring your loot to this spot

RING_FIRST = 200.0  # cm from you to the first ring of gear
RING_GAP = 150.0
RING_SPACING = 40.0  # cm of ring per item
FEET = 40.0  # cm below the pawn's origin
PUSH_NEAR = 1000.0  # cm: a pushed spot must be this close to some player's character
# m: the farthest from this game's own character that its loot is brought. Measured 2026-10-30
# (host, World_P, CLAUDE.md): loot that comes to rest more than ~148 m from the game's own player
# is destroyed by the game itself within ~1.5 s (147.5 m survived, 150 m and beyond never did),
# even right next to another player. 120 m leaves room for the rings and for items rolling.
SAFE_M = 120.0
WATCH_S = 4.0  # s to keep watching moved gear after the last of it moved (it settles in ~0.5-2 s)
WATCH_MAX_S = 30.0  # ... but never longer than this after a Bring
WATCH_KEY = "bl4_ground_loot.watch"
REACH = 2000.0  # cm: gear that vanishes farther than this from our character wasn't picked up
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


class _Watch:
    """Gear we moved, watched for WATCH_S: the game destroying one is a failure (logged), and then
    this game stops bringing its loot to other players' spots for the session."""

    items: list[list[Any]] = []  # [WeakPointer, where it was, where it went, last seen]
    until = 0.0  # watching ends then, pushed back while some of it still moves
    cap = 0.0
    failed = False


def _watch_add(item: UObject, target: tuple[float, float, float]) -> None:
    where = item.K2_GetActorLocation()
    spot = (where.X, where.Y, where.Z)
    _Watch.items.append([WeakPointer(item), spot, target, target])


def _watch_tick() -> None:
    try:
        pawn = _pawn()
        mine = pawn.K2_GetActorLocation() if pawn is not None else None
        now = time.monotonic()
        keep, lost = [], []
        for entry in _Watch.items:
            item = entry[0]()
            if item is not None and not item.bActorIsBeingDestroyed:
                where = item.K2_GetActorLocation()
                spot = (where.X, where.Y, where.Z)
                if math.dist(spot, entry[3]) > 20.0:  # still falling or rolling
                    _Watch.until = min(_Watch.cap, max(_Watch.until, now + WATCH_S))
                entry[3] = spot
                keep.append(entry)
            elif mine is not None and math.dist(entry[3], (mine.X, mine.Y, mine.Z)) > REACH:
                lost.append(entry)  # gone, and not where our character could have picked it up
        _Watch.items = keep
        if lost:
            _Watch.failed = True
            spots = "; ".join(
                f"from {tuple(round(v) for v in e[1])} to {tuple(round(v) for v in e[2])}" for e in lost
            )
            logging.error(
                f"[Ground Loot] the game destroyed {len(lost)} moved item(s) ({spots}). Loot now only"
                " comes to your own feet this session; please report this."
            )
        if not keep or now > _Watch.until:
            _Watch.items = []
            frametick.remove(WATCH_KEY)
    except Exception as ex:  # noqa: BLE001
        _Watch.items = []
        frametick.remove(WATCH_KEY)
        logging.error(f"[Ground Loot] {ex!r}")


def _too_far(x: float, y: float, z: float) -> float | None:
    """Metres from this game's own character to the spot when that's beyond SAFE_M, else None."""
    pawn = _pawn()
    if pawn is None:
        return None
    here = pawn.K2_GetActorLocation()
    dist = math.dist((here.X, here.Y, here.Z), (x, y, z)) / 100.0
    return dist if dist > SAFE_M else None


def _bring_to(x: float, y: float, z: float, facing_deg: float, partner: bool = False) -> str:
    """Brings this game's loot (lying around our own character) to the spot x, y, z. Never to a
    spot the game would destroy it at (farther than SAFE_M from our own character)."""
    pawn = _pawn()
    if pawn is None:
        return "no character"
    if (far := _too_far(x, y, z)) is not None:
        return f"left the loot where it is: that spot is {far:.0f} m away ({SAFE_M:.0f} m max)"
    if partner and _Watch.failed:
        return "left the loot where it is (the game destroyed moved loot earlier this session)"
    pickups, gear = _ground_loot(pawn.K2_GetActorLocation(), float(range_option.value))
    if gear:
        _Watch.until = time.monotonic() + WATCH_S
        _Watch.cap = _Watch.until - WATCH_S + WATCH_MAX_S
        frametick.add(WATCH_KEY, _watch_tick, every=0.25)
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
            _watch_add(gear[placed], (gx, gy, z - FEET))
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
    # The asker's own character, as the host sees it: never a spot the client sends (a modified
    # client could aim every player's loot anywhere)
    pawn = req.pc.Pawn
    if pawn is None:
        return {"ok": False, "error": "no character"}
    here = pawn.K2_GetActorLocation()
    spot = {"x": here.X, "y": here.Y, "z": here.Z, "yaw": pawn.K2_GetActorRotation().Yaw}
    others = _share(spot, skip=req.pc)  # each of them checks its own distance
    if (far := _too_far(spot["x"], spot["y"], spot["z"])) is not None:
        log(f"left the loot where it is: a partner asked from {far:.0f} m away ({SAFE_M:.0f} m max)")
        return {
            "ok": False,
            "error": (
                f"you're {far:.0f} m from the host: come within {SAFE_M:.0f} m to get the host's loot"
                f" (the game destroys loot farther than ~150 m from its owner); asked {others} other"
                " player(s)"
            ),
        }
    text = _bring_to(spot["x"], spot["y"], spot["z"], spot["yaw"], partner=True)
    log(f"{text} (a partner's Bring)")
    return {"ok": True, "text": f"the host {text}; asked {others} other player(s)"}


def _on_push(data: dict[str, Any]) -> None:
    """Friend: a partner pressed Bring; bring our loot to their spot."""
    if mod.is_enabled and partners_option.value:
        x, y, z, yaw = (float(data[k]) for k in ("x", "y", "z", "yaw"))
        if not all(math.isfinite(v) for v in (x, y, z, yaw)) or not _near_a_player(x, y, z):
            log("ignored a partner's spot that isn't by any player")
            return
        log(f"{_bring_to(x, y, z, yaw, partner=True)} (a partner's Bring)")


def _near_a_player(x: float, y: float, z: float) -> bool:
    """Whether (x, y, z) is within PUSH_NEAR of some player's character. Also True when some
    player's character isn't known here (not replicated to us, e.g. far away), so a real
    partner's spot is never refused for that."""
    world = ENGINE.GameViewport.World
    state = world.GameState if world is not None else None
    if state is None:
        return True
    players = state.PlayerArray
    unknown = False
    for i in range(len(players)):
        ps = players[i]
        pawn = ps.PawnPrivate if ps is not None else None
        if pawn is None:
            unknown = True
            continue
        where = pawn.K2_GetActorLocation()
        if math.dist((where.X, where.Y, where.Z), (x, y, z)) <= PUSH_NEAR:
            return True
    return unknown


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
