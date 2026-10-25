"""
The backpack's item limit, and changing it.

Each player's limit is `PlayerState.BackpackContainer.MaxSize` (a GbxAttributeInteger). The game
works out `Value` from `BaseValue` (16) plus the SDU upgrades, and the inventory screen's "16 / 70"
and the pickup check both read `Value`. Setting it takes effect right away, and nothing resets it
until the game works it out again (a new map, a new SDU, a player joining), so `apply` runs twice
a second and puts our number back.

We remember the game's own number for each player, and `restore` puts that back. Items over the
limit aren't lost: the game shows them as overflow and you can't pick up more until you're under.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

from dataclasses import dataclass

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

_kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject


@dataclass
class _Changed:
    player_state: WeakPointer
    game_value: int
    ours: int


_changed: list[_Changed] = []
_failed: set[str] = set()  # players whose limit we couldn't set, logged once each


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


def _record(ps: UObject) -> _Changed | None:
    for entry in _changed:
        if entry.player_state() == ps:
            return entry
    return None


def _apply_one(ps: UObject, size: int) -> None:
    if ps.BackpackContainer is None:
        return  # a joining player whose state isn't ready yet
    max_size = ps.BackpackContainer.MaxSize
    value = max_size.Value
    entry = _record(ps)
    if entry is None:
        if value == size:
            return
        entry = _Changed(WeakPointer(ps), value, size)
        _changed.append(entry)
    elif value != entry.ours:
        # The game worked its number out again (new SDU, new map, ...), even if it came out at
        # our number
        entry.game_value = value
    entry.ours = size
    if value == size:
        return
    max_size.Value = size
    log(f"{ps.PlayerNamePrivate or ps.Name}: backpack size {value} -> {size}")


def apply(size: int) -> None:
    for ps in _player_states():
        try:
            _apply_one(ps, size)
        except Exception as ex:  # noqa: BLE001 - one player mustn't stop the others
            if ps.Name not in _failed:
                _failed.add(ps.Name)
                logging.error(f"[Backpack Size] {ps.Name}: {ex!r}")
    # Players who left or maps we've moved on from
    _changed[:] = [e for e in _changed if e.player_state() is not None]


def restore() -> None:
    for entry in _changed:
        ps = entry.player_state()
        if ps is None or ps.BackpackContainer is None:
            continue
        max_size = ps.BackpackContainer.MaxSize
        if max_size.Value == entry.ours:
            max_size.Value = entry.game_value
            log(f"{ps.PlayerNamePrivate or ps.Name}: backpack size back to {entry.game_value}")
    _changed.clear()


def game_size() -> int | None:
    """The game's own limit for the local player (with no mod), if we know it."""
    pc = get_pc()
    ps = pc.PlayerState if pc is not None else None
    if ps is None:
        return None
    entry = _record(ps)
    if entry is not None:
        return entry.game_value
    return ps.BackpackContainer.MaxSize.Value if ps.BackpackContainer is not None else None
