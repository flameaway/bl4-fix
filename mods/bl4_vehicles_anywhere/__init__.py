"""
Vehicles Anywhere for Borderlands 4: summon and drive your vehicle in places where the game
normally says vehicles aren't allowed.

Once a second we switch off the game's vehicle rules around you (see rules.py): the map-wide
"no vehicles" setting and the no-vehicle zones. Turning the mod off puts them all back.

The host checks summons (ServerRequestPersonalVehicle), so in co-op the host should run it too.
"""

from __future__ import annotations

import time
from typing import Any

from mods_base import BoolOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import rules

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
INTERVAL = 1.0  # s


def _zones_changed(_option: BoolOption, value: bool) -> None:
    if not value:
        rules.restore_zones()


zones_option = BoolOption(
    "Ignore no-vehicle zones",
    True,
    description=(
        "Lets you summon inside no-vehicle zones and keeps you in the seat when you drive into"
        " one. Some races and missions use these zones."
    ),
    on_change=_zones_changed,
)

_next_check = 0.0


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _next_check
    now = time.monotonic()
    if now < _next_check:
        return
    _next_check = now + INTERVAL
    try:
        rules.apply(zones=zones_option.value)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Vehicles Anywhere] {ex!r}")


def _enable() -> None:
    global _next_check
    _next_check = 0.0


mod = build_mod(
    options=[zones_option],
    hooks=[tick],
    on_enable=_enable,
    on_disable=rules.restore,
)
