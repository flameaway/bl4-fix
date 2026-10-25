"""
Notices when the player beats a boss.

Two ways in, since neither covers everything on its own:
- OakBossFight.BossDied(DeadActor): arena fights call it (through a delegate, so a hook sees it)
  when their boss dies. See boss_died() in __init__.py.
- The boss health bars: PlayerController.BossState.VisibleBosses lists the bosses whose bar
  shows. We keep a weak pointer to each and check IsDead() every frame. That covers bosses
  outside arenas and the client side of co-op, where the arena's events may not run. With no boss
  watched, the list is only looked at SCAN_S apart (reading it cost ~0.1 ms a frame in a hook).

A boss is matched to the dex by its object name, which is its def (Char_GrassBoss_2147...), or
else by the UI name it shows (data.boss_for). Never keep a plain reference to a game actor across
frames: touching a freed one crashes the game, so it's WeakPointers only.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

from . import data

_watched: list[tuple[WeakPointer, str]] = []
SCAN_S = 0.2
_scanned_at = 0.0


def identify(actor: UObject) -> str | None:
    """The dex boss this actor is, if any."""
    display = None
    try:
        display = actor.UXNameRepData.DisplayDef._name  # noqa: SLF001
    except (AttributeError, ValueError):
        pass
    return data.boss_for(str(actor.Name), display)


def _is_dead(actor: UObject) -> bool:
    try:
        return bool(actor.IsDead())
    except (AttributeError, ValueError, TypeError):
        return False


def tick(beaten: Callable[[str], None]) -> None:
    """Call every frame. Calls beaten(boss id) when a watched boss dies."""
    global _scanned_at
    now = time.monotonic()
    if not _watched and now - _scanned_at < SCAN_S:
        return
    _scanned_at = now
    pc = get_pc(possibly_loading=True)
    if pc is None:
        _watched.clear()
        return
    try:
        visible = list(pc.BossState.VisibleBosses)
    except AttributeError:
        visible = []
    for actor in visible:
        if actor is None or any(wp() is actor for wp, _ in _watched):
            continue
        boss = identify(actor)
        if boss is not None:
            _watched.append((WeakPointer(actor), boss))
            logging.dev_warning(f"[Weapondex] watching {actor.Name} ({boss})")
    for entry in list(_watched):
        actor = entry[0]()
        if actor is None:
            _watched.remove(entry)  # gone (left the area, despawned) without dying
        elif _is_dead(actor):
            _watched.remove(entry)
            beaten(entry[1])


def forget() -> None:
    _watched.clear()
