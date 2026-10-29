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
from .mapdata import pin_location

SCAN_INTERVAL = 0.25  # s between searches for enemies
HOSTILE = 2  # ETeamAttitude
LOOT_REACH = 30000.0  # legendaries further than this (300 m) aren't shown, not even on the rim

ENEMY, BOSS, FRIEND, WAYPOINT = "enemy", "boss", "friend", "waypoint"
FRIEND_CLASS = "friend_class"  # a teammate drawn with their vault hunter's icon

# A player's class -> the ECHO map's icon for them (asset paths checked in the registry, 2026-10-28)
_VH = "/Game/uiresources/_shared/assets/ico_ui_art_vault_hunters/"
CLASS_ICONS = {
    "darksiren": _VH + "player_class_dark_siren.player_class_dark_siren",
    "exo": _VH + "player_class_exo_soldier.player_class_exo_soldier",
    "gravitar": _VH + "player_class_gravitar.player_class_gravitar",
    "paladin": _VH + "player_class_paladin.player_class_paladin",
    "robodealer": "/Game/DLC/Cowbell/uiresources/portrait_icons/player_class_robodealer.player_class_robodealer",
    "corpohacker": "/Game/DLC/Harmonica/uiresources/corpo_hacker_icons/portrait/player_class_loveless.player_class_loveless",
}
# Character def ("Char_ExoSoldier", lowercased, no prefix/underscores) -> CLASS_ICONS key
CHAR_DEFS = {"darksiren": "darksiren", "exosoldier": "exo", "gravitar": "gravitar", "paladin": "paladin",
             "robodealer": "robodealer", "corpohacker": "corpohacker", "loveless": "corpohacker"}  # fmt: skip
SPEC_PREFIX = "GbxSkillNetSpecializationComponent_"
CLASS_RETRY = 1.0  # s before asking a pawn whose class wasn't known yet again


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
    spec_class: Any = None
    # (pawn address, InternalIndex) -> class icon path, or the time to look again
    class_icons: dict[tuple[int, int], str | float] = {}  # noqa: RUF012


def _class_icon(ps: UObject, pawn: UObject) -> str | None:
    """
    The vault hunter icon for another player, or None while their class isn't known.

    The PlayerState's ReplicatedCharacterDef is the first choice, but on a client the host's reads
    None (seen 2026-10-28), so a teammate flipped between their icon and the plain dot. Every
    player pawn has one class-specific skill-net component (`..._Exo`, `..._Paladin`, ...) on host
    and client alike: that's the fallback. Remembered per pawn once known.
    """
    key = (pawn._get_address(), pawn.InternalIndex)  # noqa: SLF001
    known = Scan.class_icons.get(key)
    if isinstance(known, str):
        return known
    if known is not None and time.monotonic() < known:
        return None
    found = None
    def_name = str(ps.ReplicatedCharacterDef or "")
    if def_name and def_name != "None":
        found = CHAR_DEFS.get(def_name.lower().removeprefix("char_").replace("_", ""))
    if found is None:
        if Scan.spec_class is None:
            Scan.spec_class = unrealsdk.find_class("GbxSkillNetSpecializationComponent")
        for comp in pawn.K2_GetComponentsByClass(Scan.spec_class):
            name = str(comp.Class.Name)
            if name.startswith(SPEC_PREFIX):
                found = name[len(SPEC_PREFIX):].lower()
                if found in CLASS_ICONS:
                    break
                found = None
    if len(Scan.class_icons) > 64:
        Scan.class_icons.clear()
    if found is None:
        Scan.class_icons[key] = time.monotonic() + CLASS_RETRY
        return None
    Scan.class_icons[key] = CLASS_ICONS[found]
    return CLASS_ICONS[found]


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
            icon = _class_icon(ps, other)
            out.append(Marker(FRIEND_CLASS if icon else FRIEND, loc.X, loc.Y, True, loc.Z, icon))

    pin = pin_location(pc, str(world.Name))
    if pin is not None:
        out.append(Marker(WAYPOINT, pin[0], pin[1], True))
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
