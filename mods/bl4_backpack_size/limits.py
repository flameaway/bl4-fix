"""
The item limits of the backpack, the bank and Lost Loot, and changing them.

- Backpack: `PlayerState.BackpackContainer.MaxSize` (a GbxAttributeInteger). The game works out
  `Value` from `BaseValue` (16) plus the SDU upgrades; the inventory screen's "16 / 70" and the
  pickup check both read `Value`.
- Bank: `PlayerState.BankContainer.MaxSize`, the same kind of thing (BaseValue 25).
- Lost Loot: how many items the machine holds is the attribute `lost_loot_storage_maxsize`, which
  `OakLostLootValueResolver` reads from a GbxAttributeInteger inside the player's
  `OakLostLootSubsystem` (`PlayerState.LostLootManager`). The subsystem has no reflected
  properties, so we write the int at LOST_LOOT_VALUE ourselves, after checking it against the
  attribute (see _lost_loot_verified). `PlayerState.ReplicatedLostLootSlots` carries the same
  number to the player's own game in co-op; we set it too.

Setting any of them takes effect right away, and nothing resets it until the game works it out
again (a new map, a new SDU, a player joining), so `apply` runs twice a second and puts our
number back.

We remember the game's own number for each player and limit, and `restore` puts that back. Items
over the limit aren't lost: the game shows them as overflow and you can't add more until you're
under.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

_kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
_attributes = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject

BACKPACK = "backpack"
BANK = "bank"
LOST_LOOT = "lost_loot"
KINDS = (BACKPACK, BANK, LOST_LOOT)
_NAMES = {BACKPACK: "backpack size", BANK: "bank size", LOST_LOOT: "Lost Loot size"}

# OakLostLootSubsystem (unreflected, measured 2026-10-27): a GbxAttributeInteger {id, Value,
# BaseValue} at +0x30, the items TArray at +0x40. Value (11 with upgrades) is what
# lost_loot_storage_maxsize resolves to; BaseValue is 6, the class default of
# ReplicatedLostLootSlots.
LOST_LOOT_CLASS = "OakLostLootSubsystem"
LOST_LOOT_CLASS_SIZE = 0x150
LOST_LOOT_VALUE = 0x34
LOST_LOOT_BASE = 0x38
LOST_LOOT_ATTRIBUTE = "lost_loot_storage_maxsize"


@dataclass
class _Changed:
    kind: str
    player_state: WeakPointer
    game_value: int
    ours: int


_changed: list[_Changed] = []
_failed: set[tuple[str, str]] = set()  # (kind, player) we couldn't set, logged once each
_lost_loot_ok: list[WeakPointer] = []  # subsystems whose layout we checked
_lost_loot_bad: list[WeakPointer] = []  # ... and found not to match: left alone
_lost_loot_misses: list[list] = []  # [WeakPointer, ticks it didn't match yet]
LOST_LOOT_TRIES = 20  # ticks (10 s) a loaded subsystem may mismatch before we give up on it


def log(msg: str) -> None:
    logging.info(f"[Backpack Size] {msg}")


def _player_states() -> list[UObject]:
    """Everyone's on the host (it decides pickups for all players), only our own on a client."""
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return []
    if _kismet.IsServer(world) and world.GameState is not None:
        return [ps for ps in world.GameState.PlayerArray if ps is not None]
    pc = get_pc()
    return [pc.PlayerState] if pc is not None and pc.PlayerState is not None else []


# region Lost Loot


def _in(pointers: list[WeakPointer], obj: UObject) -> bool:
    pointers[:] = [p for p in pointers if p() is not None]
    return any(p() == obj for p in pointers)


def _lost_loot_bad_add(sub: UObject, why: str) -> None:
    _lost_loot_bad.append(WeakPointer(sub))
    logging.warning(f"[Backpack Size] Lost Loot left alone: its memory doesn't look as expected (a game patch?): {why}")


def _lost_loot_verified(ps: UObject, sub: UObject) -> bool:
    """
    Whether this subsystem's memory looks like the one we measured: class and size, a sane base,
    and the value at LOST_LOOT_VALUE is what the attribute resolves to right now. Checked per
    subsystem (a new one each map), before our first write to it.

    Right after a map load the subsystem is all zeros for a few seconds (seen 2026-10-27): that's
    "not loaded yet", checked again on the next tick. A mismatch also gets LOST_LOOT_TRIES ticks to
    settle before we give up on that subsystem.
    """
    if _in(_lost_loot_ok, sub):
        return True
    if _in(_lost_loot_bad, sub):
        return False
    if sub.Class.Name != LOST_LOOT_CLASS or sub.Class.PropertySize != LOST_LOOT_CLASS_SIZE:
        _lost_loot_bad_add(sub, f"{sub.Class.Name}, {sub.Class.PropertySize:#x} bytes")
        return False
    address = sub._get_address()
    value = ctypes.c_int32.from_address(address + LOST_LOOT_VALUE).value
    base = ctypes.c_int32.from_address(address + LOST_LOOT_BASE).value
    if value == 0 and base == 0:
        return False  # not loaded yet
    resolved = _attributes.GetValueOfAttribute(LOST_LOOT_ATTRIBUTE, ps, -1.0)
    if 0 < base <= value <= 10000 and resolved == value:
        _lost_loot_ok.append(WeakPointer(sub))
        _lost_loot_misses[:] = [m for m in _lost_loot_misses if m[0]() not in (None, sub)]
        return True
    entry = next((m for m in _lost_loot_misses if m[0]() == sub), None)
    if entry is None:
        _lost_loot_misses[:] = [m for m in _lost_loot_misses if m[0]() is not None]
        entry = [WeakPointer(sub), 0]
        _lost_loot_misses.append(entry)
    entry[1] += 1
    if entry[1] >= LOST_LOOT_TRIES:
        _lost_loot_misses.remove(entry)
        _lost_loot_bad_add(sub, f"value {value}, base {base}, attribute {resolved}")
    return False


def _lost_loot_get(ps: UObject) -> int | None:
    sub = ps.LostLootManager
    if sub is None:
        # A client has no subsystem (the host keeps it); its menu reads the replicated number
        return None if _kismet.IsServer(ENGINE.GameViewport.World) else ps.ReplicatedLostLootSlots
    if not _lost_loot_verified(ps, sub):
        return None
    return ctypes.c_int32.from_address(sub._get_address() + LOST_LOOT_VALUE).value


def _lost_loot_set(ps: UObject, size: int) -> None:
    sub = ps.LostLootManager
    if sub is not None:
        if not _lost_loot_verified(ps, sub):
            return
        ctypes.c_int32.from_address(sub._get_address() + LOST_LOOT_VALUE).value = size
    if ps.ReplicatedLostLootSlots != size:
        ps.ReplicatedLostLootSlots = size


# endregion


def _get(kind: str, ps: UObject) -> int | None:
    """The limit's current value, or None if this player doesn't have it (yet)."""
    if kind == LOST_LOOT:
        return _lost_loot_get(ps)
    container = ps.BackpackContainer if kind == BACKPACK else ps.BankContainer
    return None if container is None else container.MaxSize.Value


def _set(kind: str, ps: UObject, size: int) -> None:
    if kind == LOST_LOOT:
        _lost_loot_set(ps, size)
    else:
        container = ps.BackpackContainer if kind == BACKPACK else ps.BankContainer
        container.MaxSize.Value = size


def _record(kind: str, ps: UObject) -> _Changed | None:
    for entry in _changed:
        if entry.kind == kind and entry.player_state() == ps:
            return entry
    return None


def _apply_one(kind: str, ps: UObject, size: int) -> None:
    value = _get(kind, ps)
    if value is None:
        return  # a joining player whose state isn't ready yet, or Lost Loot we leave alone
    entry = _record(kind, ps)
    if entry is None:
        if value == size:
            return
        entry = _Changed(kind, WeakPointer(ps), value, size)
        _changed.append(entry)
    elif value != entry.ours:
        # The game worked its number out again (new SDU, new map, ...), even if it came out at
        # our number
        entry.game_value = value
    entry.ours = size
    if value == size:
        return
    _set(kind, ps, size)
    log(f"{ps.PlayerNamePrivate or ps.Name}: {_NAMES[kind]} {value} -> {size}")


def apply(sizes: dict[str, int | None]) -> None:
    """Sets each limit in `sizes` for every player; None (or 0) puts the game's own number back."""
    for kind in KINDS:
        if not sizes.get(kind):
            restore(kind)
    states = _player_states()
    for kind in KINDS:
        size = sizes.get(kind)
        if not size:
            continue
        for ps in states:
            try:
                _apply_one(kind, ps, size)
            except Exception as ex:  # noqa: BLE001 - one player mustn't stop the others
                if (kind, ps.Name) not in _failed:
                    _failed.add((kind, ps.Name))
                    logging.error(f"[Backpack Size] {ps.Name} {_NAMES[kind]}: {ex!r}")
    # Players who left or maps we've moved on from
    _changed[:] = [e for e in _changed if e.player_state() is not None]


def restore(kind: str | None = None) -> None:
    """Puts the game's own number back (for one limit, or all of them)."""
    keep = []
    for entry in _changed:
        if kind is not None and entry.kind != kind:
            keep.append(entry)
            continue
        ps = entry.player_state()
        if ps is None:
            continue
        try:
            if _get(entry.kind, ps) == entry.ours:
                _set(entry.kind, ps, entry.game_value)
                log(f"{ps.PlayerNamePrivate or ps.Name}: {_NAMES[entry.kind]} back to {entry.game_value}")
        except Exception as ex:  # noqa: BLE001 - put back the rest anyway
            logging.error(f"[Backpack Size] {ps.Name} {_NAMES[entry.kind]}: {ex!r}")
    _changed[:] = keep


def game_size(kind: str = BACKPACK) -> int | None:
    """The game's own limit for the local player (with no mod), if we know it."""
    pc = get_pc()
    ps = pc.PlayerState if pc is not None else None
    if ps is None:
        return None
    entry = _record(kind, ps)
    return entry.game_value if entry is not None else _get(kind, ps)
