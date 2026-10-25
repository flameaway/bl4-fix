"""
What the minimap marks, and where.

- Enemies: characters hostile to you (GbxTeamFunctionLibrary.GetAttitudeTowards == Hostile) and
  alive. The world is searched a few times a second; positions are read every frame.
- Bosses: the ones whose health bar shows (PlayerController.BossState.VisibleBosses), drawn with
  the radar's skull instead of a diamond.
- Teammates: the other players' pawns (GameState.PlayerArray).
- Your custom waypoint: the route pin (DiscoveryPinningState, pin type 1) when it's on this map.
- Legendary and pearlescent items on the ground (loot.py), when that option is on.

Enemies only show inside the minimap's range. Teammates and the waypoint stay on the rim when
they're further away, so you can tell which way to go.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject, WeakPointer

from .loot import Loot

SCAN_INTERVAL = 0.25  # s between searches for enemies
HOSTILE = 2  # ETeamAttitude
PIN_CUSTOM_WAYPOINT = 1
LOOT_REACH = 30000.0  # legendaries further than this (300 m) aren't shown, not even on the rim

ENEMY, BOSS, FRIEND, WAYPOINT = "enemy", "boss", "friend", "waypoint"


@dataclass
class Marker:
    kind: str
    x: float  # world position
    y: float
    clamp: bool  # stays on the rim when out of range
    z: float | None = None  # height, when it's worth an above/below chevron
    texture: str | None = None  # instead of the kind's own look (objectives: the mission's icon)


class Scan:
    next_scan = 0.0
    enemies: list[WeakPointer] = []  # noqa: RUF012
    character_class: Any = None


def _enemies(pc: UObject, pawn: UObject) -> list[UObject]:
    now = time.monotonic()
    if now >= Scan.next_scan:
        Scan.next_scan = now + SCAN_INTERVAL
        if Scan.character_class is None:
            Scan.character_class = unrealsdk.find_class("OakCharacter")
        gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
        found = gs.GetAllActorsOfClass(pc, Scan.character_class, [])
        found = found[1] if isinstance(found, tuple) else found
        teams = unrealsdk.find_class("GbxTeamFunctionLibrary").ClassDefaultObject
        Scan.enemies = [
            WeakPointer(c) for c in found
            if c is not None and c is not pawn and not c.bHidden and teams.GetAttitudeTowards(pawn, c) == HOSTILE
        ]  # fmt: skip
    alive = []
    for ptr in Scan.enemies:
        actor = ptr()
        if actor is None:
            continue
        try:
            if actor.IsDead():
                continue
        except (AttributeError, ValueError, TypeError):
            pass
        alive.append(actor)
    return alive


def collect(pc: UObject, pawn: UObject, world: UObject, *, loot_on: bool = False) -> list[Marker]:
    out: list[Marker] = []
    if loot_on:
        Loot.scan(pc)
        me = pawn.K2_GetActorLocation()
        out += [
            Marker(kind, x, y, True, z) for kind, x, y, z in Loot.items()
            if (x - me.X) ** 2 + (y - me.Y) ** 2 <= LOOT_REACH**2
        ]  # fmt: skip
    try:
        bosses = {b._get_address() for b in pc.BossState.VisibleBosses if b is not None}  # noqa: SLF001
    except AttributeError:
        bosses = set()
    for actor in _enemies(pc, pawn):
        loc = actor.K2_GetActorLocation()
        out.append(Marker(BOSS if actor._get_address() in bosses else ENEMY, loc.X, loc.Y, False))  # noqa: SLF001

    game_state = world.GameState
    for ps in game_state.PlayerArray if game_state is not None else []:
        if ps is None or ps is pc.PlayerState:
            continue
        other = ps.GetPawn()
        if other is not None:
            loc = other.K2_GetActorLocation()
            out.append(Marker(FRIEND, loc.X, loc.Y, True, loc.Z))

    world_name = str(world.Name).lower()
    for pin in pc.PlayerState.DiscoveryPinningState.PinnedDatas:
        if pin.pintype == PIN_CUSTOM_WAYPOINT and str(pin.PinnedCustomWaypointWorldName).lower() == world_name:
            loc = pin.PinnedCustomWaypointLocation
            out.append(Marker(WAYPOINT, loc.X, loc.Y, True))
    return out


def to_screen(marker: Marker, cx: float, cy: float, yaw: float, radius: float) -> tuple[float, float] | None:
    """
    Where a marker goes, in -1..1 of the minimap's half-size (x right, y down), with `yaw` up.
    None: out of range and not clamped.
    """
    return project(marker.x, marker.y, cx, cy, yaw, radius, clamp=marker.clamp)


def project(x: float, y: float, cx: float, cy: float, yaw: float, radius: float, *,
            clamp: bool = False, square: bool = False) -> tuple[float, float] | None:  # fmt: skip
    """
    World point (x, y) on the minimap centred on (cx, cy) with `yaw` up (see to_screen). A square
    minimap reaches `radius` to its sides (further into its corners).
    """
    dx, dy = x - cx, y - cy
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    forward = dx * c + dy * s
    right = -dx * s + dy * c
    u, v = right / radius, -forward / radius
    dist = max(abs(u), abs(v)) if square else math.hypot(u, v)
    if dist > 1.0:
        if not clamp:
            return None
        u, v = u / dist, v / dist
    return u, v
