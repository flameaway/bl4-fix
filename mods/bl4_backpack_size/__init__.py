"""
Backpack Size for Borderlands 4: set your backpack's item limit to any number you like.

Twice a second we set the backpack's limit (see limits.py) to the number in the options, so it
stays put when the game works its own number out again (new map, SDU upgrade). Turning the mod off
puts the game's number back; items over it show as overflow, nothing is lost.

Co-op: the host checks pickups, so the host's mod sets every player's limit (friends don't need the
mod). The limit is part of the replicated player state; a friend who also runs the mod holds the
host's number on their side too (sent over BL4 Direct's co-op channel), in case their game checks
before asking the host. A client never applies its own slider: the host's number is the one that
counts.
"""

from __future__ import annotations

import time
from typing import Any

from mods_base import SliderOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import limits

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host behavior only
    coop = None

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
INTERVAL = 0.5  # s
FEATURE = "backpack_size"
PUSH_KIND = "backpack_size.size"


class _Co:
    host_size: int | None = None  # client: the size the host's mod sent
    told: tuple[int, frozenset[int]] | None = None  # host: what we last sent, and to whom


def _apply(size: int) -> None:
    try:
        limits.apply(size)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Backpack Size] {ex!r}")


def _size_changed(_option: SliderOption, value: float) -> None:
    if coop is not None and coop.is_client():
        return  # the host's number counts
    _apply(int(value))


size_option = SliderOption(
    "Backpack size",
    200,
    10,
    1000,
    5,
    description=(
        "How many items your backpack holds (equipped gear doesn't count). The game's own limit"
        " is 16 plus your SDU upgrades. If you lower it below what you carry, the extra items"
        " stay, but you can't pick anything up until you're under it. In co-op the host's number"
        " counts for everyone."
    ),
    on_change_while_enabled=_size_changed,
)

_next_check = 0.0


def _tell_clients(size: int) -> None:
    """As host: sends our size to friends running the mod, when it or the list of them changed."""
    with_mod = coop.players_with(FEATURE)
    told = (size, frozenset(pc._get_address() for pc in with_mod))
    if told != _Co.told:
        _Co.told = told
        for pc in with_mod:
            coop.push(pc, PUSH_KIND, {"size": size})


def _on_host_size(data: dict[str, Any]) -> None:
    global _next_check
    size = data.get("size")
    _Co.host_size = int(size) if isinstance(size, int) and size > 0 else None
    _next_check = 0.0


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _next_check
    now = time.monotonic()
    if now < _next_check:
        return
    _next_check = now + INTERVAL
    try:
        if coop is not None and coop.is_client():
            if coop.host_has(FEATURE) and _Co.host_size is not None:
                limits.apply(_Co.host_size)
            else:
                limits.restore()  # an unmodded host decides with its own number
            return
        _Co.host_size = None
        size = int(size_option.value)
        limits.apply(size)
        if coop is not None:
            _tell_clients(size)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Backpack Size] {ex!r}")


def _enable() -> None:
    global _next_check
    _next_check = 0.0
    _Co.told = None


if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
    coop.on_push(PUSH_KIND, _on_host_size)


mod = build_mod(
    options=[size_option],
    hooks=[tick],
    on_enable=_enable,
    on_disable=limits.restore,
)
