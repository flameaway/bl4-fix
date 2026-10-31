"""
Vehicles Anywhere for Borderlands 4: summon and drive your vehicle in places where the game
normally says vehicles aren't allowed.

Once a second we switch off the game's vehicle rules around you (see rules.py): the map-wide
"no vehicles" setting and the no-vehicle zones. Turning the mod off puts them all back.

The host checks summons (ServerRequestPersonalVehicle), so in co-op the host should run it too.
"""

from __future__ import annotations

from mods_base import BoolOption, build_mod
from unrealsdk import logging

from . import frametick, rules

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

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

# Once a second through the shared frame tick (frametick/); the first one right after enabling
@frametick.tick("bl4_vehicles_anywhere", every=INTERVAL)
def tick() -> None:
    try:
        rules.apply(zones=zones_option.value)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        # Each distinct error once: a game patch renaming something this reads would fail every second
        text = repr(ex)[:300]
        if text not in _logged_errors and len(_logged_errors) < 20:  # noqa: PLR2004
            _logged_errors.add(text)
            logging.error(f"[Vehicles Anywhere] {text} (logged once)")


_logged_errors: set[str] = set()


mod = build_mod(
    options=[zones_option],
    hooks=[tick],
    on_disable=rules.restore,
)
