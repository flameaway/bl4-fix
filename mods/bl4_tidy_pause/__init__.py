"""
Tidy Pause Menu for Borderlands 4: the pause menu's News panel moves to the right side, under the
party list, so it no longer covers the bottom of the menu (with BL4 Fix's Mods, Kairosdex and
Carnage Report entries added, Quit Game ended up under it). Or hide the panel altogether.

The panel is moved by a page script that runs in the pause menu's page through the BL4 Mod Menu
(its extensions), so this mod needs the Mod Menu and ships no pak. Turning it off (or picking
"Game default") puts the panel back where the game had it, at once.

Everything but this file can be hot-reloaded (dev/reload_tidy_pause.py), and the page's script and
styles reload themselves in the page.
"""

from __future__ import annotations

import time
from typing import Any

from mods_base import SETTINGS_DIR, DropdownOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Type

from . import page

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

GAME_DIR = SETTINGS_DIR.parent.parent
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# Nothing here needs every frame: register with the Mod Menu (it may load after us) and, from a
# source folder, copy the page's files when they change
CHECK_S = 1.0

NEWS_CHOICES = {"Right side": "right", "Hidden": "hidden", "Game default": "default"}


def _news_changed(_: DropdownOption, _value: str) -> None:
    page.changed()


news_option = DropdownOption(
    "News panel",
    "Right side",
    list(NEWS_CHOICES),
    description=(
        "Where the pause menu's News panel goes. Right side: under the party list, clear of the"
        " menu entries. Hidden: gone, with its arrows. Game default: where the game puts it."
    ),
    on_change=_news_changed,
)
page.S.news = lambda: NEWS_CHOICES.get(news_option.value, "right")


class S:
    registered = False
    checked_at = 0.0
    errors: set[str] = set()  # noqa: RUF012


@hook(TICK_FUNC, Type.POST)
def _tick(*_: Any) -> None:
    now = time.monotonic()
    if now - S.checked_at < CHECK_S:
        return
    S.checked_at = now
    try:
        if not S.registered:
            S.registered = page.register(GAME_DIR)
        else:
            page.publish()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in S.errors:
            S.errors.add(repr(ex))
            logging.exception(ex)


def _enable() -> None:
    S.registered = False
    S.checked_at = 0.0


def _disable() -> None:
    # The page script sees its key unregistered and puts the panel back
    page.unregister()
    S.registered = False


mod = build_mod(
    options=[news_option],
    hooks=[_tick],
    on_enable=_enable,
    on_disable=_disable,
)
