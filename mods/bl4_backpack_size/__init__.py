"""
Backpack Size for Borderlands 4: set how many items your backpack, your bank and Lost Loot hold.

Twice a second we set each limit (see limits.py) to the number in the options, so it stays put
when the game works its own number out again (new map, SDU upgrade). A bank or Lost Loot size of 0
leaves that one to the game. Turning the mod off puts the game's numbers back; items over them
show as overflow, nothing is lost.

Co-op: the host checks pickups, so the host's mod sets every player's limits (friends don't need
the mod). The limits are part of the replicated player state; a friend who also runs the mod holds
the host's numbers on their side too (sent over BL4 Direct's co-op channel), in case their game
checks before asking the host. A client never applies its own sliders: the host's numbers are the
ones that count.
"""

from __future__ import annotations

from typing import Any

from mods_base import SliderOption, build_mod
from unrealsdk import logging

from . import frametick, health, limits

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host behavior only
    coop = None

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

INTERVAL = 0.5  # s
FEATURE = "backpack_size"
# {"size": backpack, "bank": n, "lost_loot": n}; 0 (or missing, from 0.1.0) = the game's own
PUSH_KIND = "backpack_size.size"


class _Co:
    host_sizes: dict[str, int] | None = None  # client: the sizes the host's mod sent
    told: tuple[tuple[int, int, int], frozenset[int]] | None = None  # host: what we sent, to whom


def _sizes() -> dict[str, int]:
    return {
        limits.BACKPACK: int(size_option.value),
        limits.BANK: int(bank_option.value),
        limits.LOST_LOOT: int(lost_loot_option.value),
    }


def _size_changed(_option: SliderOption, _value: float) -> None:
    if coop is not None and coop.is_client():
        return  # the host's numbers count
    frametick.soon("bl4_backpack_size")  # the option's value is set after this call: the next tick applies it


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

bank_option = SliderOption(
    "Bank size",
    0,
    0,
    2000,
    25,
    description=(
        "How many items your bank holds. 0 = the game's own number (25 plus your bank SDU"
        " upgrades). Below what's stored, the extra items stay but you can't store more. In co-op"
        " the host's number counts for everyone."
    ),
    on_change_while_enabled=_size_changed,
)

lost_loot_option = SliderOption(
    "Lost Loot size",
    0,
    0,
    100,
    1,
    description=(
        "How many items the Lost Loot machine holds for you. 0 = the game's own number (6 plus"
        " upgrades). In co-op the host's number counts for everyone."
    ),
    on_change_while_enabled=_size_changed,
)


def _tell_clients(sizes: dict[str, int]) -> None:
    """As host: sends our sizes to friends running the mod, when they or the list of them changed."""
    with_mod = coop.players_with(FEATURE)
    numbers = (sizes[limits.BACKPACK], sizes[limits.BANK], sizes[limits.LOST_LOOT])
    told = (numbers, frozenset(pc._get_address() for pc in with_mod))
    if told != _Co.told:
        _Co.told = told
        data = {"size": numbers[0], "bank": numbers[1], "lost_loot": numbers[2]}
        for pc in with_mod:
            coop.push(pc, PUSH_KIND, data)


def _on_host_sizes(data: dict[str, Any]) -> None:
    def number(key: str, option: SliderOption) -> int:
        # Capped at our own slider's maximum: those are the numbers known to work
        value = data.get(key)
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
        return min(value, int(option.max_value)) if ok else 0

    sizes = {limits.BACKPACK: number("size", size_option), limits.BANK: number("bank", bank_option),
             limits.LOST_LOOT: number("lost_loot", lost_loot_option)}  # fmt: skip
    _Co.host_sizes = sizes if any(sizes.values()) else None
    frametick.soon("bl4_backpack_size")  # applied at the next frame


# Twice a second through the shared frame tick (frametick/; frames in between cost nothing); the
# first one right after enabling. A slider change or the host's numbers apply at the next frame (soon).
@frametick.tick("bl4_backpack_size", every=INTERVAL)
def tick() -> None:
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        if coop is not None and coop.is_client():
            if coop.host_has(FEATURE) and _Co.host_sizes is not None:
                limits.apply(_Co.host_sizes)
            else:
                limits.restore()  # an unmodded host decides with its own numbers
            return
        _Co.host_sizes = None
        sizes = _sizes()
        limits.apply(sizes)
        if coop is not None:
            _tell_clients(sizes)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Backpack Size] {ex!r}")


def _enable() -> None:
    _Co.told = None


def _disable() -> None:
    try:
        limits.restore()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Backpack Size] {ex!r}")


if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
    coop.on_push(PUSH_KIND, _on_host_sizes)


mod = build_mod(
    options=[size_option, bank_option, lost_loot_option],
    hooks=[tick],
    on_enable=_enable,
    on_disable=_disable,
)
