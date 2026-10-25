"""
BL4 Mod Menu: an in-game settings screen for every SDK mod. Open it from MODS in the pause menu,
with F7, or by typing `modmenu` in the console.

The screen is HTML in the game's own menu page, built from the game's Options menu parts, so it
looks native (web.py and ui/). Where that page isn't loaded, e.g. at the title screen, F7 opens a
basic panel drawn with UMG instead (menu.py).

Other mods need nothing extra to show up here. The options and keybinds a mod gives build_mod
(BoolOption, SliderOption, DropdownOption, SpinnerOption, ButtonOption, KeybindOption,
GroupedOption, NestedOption) are drawn as controls, and changing one runs its on_change callback
and saves the mod's settings at once. So a mod is configurable here, with no restart, as long as
its options take effect from their on_change callback (or are read when used) rather than only
at startup.

A mod can also open the menu on its own page, e.g. from a button in its own UI:

    import sys
    if (menu := sys.modules.get("bl4_mod_menu")) is not None:
        menu.open_menu("BL4 Shoot to Open")

Everything can be hot-reloaded through the bridge (dev/reload_mod_menu.py), and the page's
scripts reload themselves when they change; this file only holds the mod itself.
"""

from __future__ import annotations

import argparse

from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, KeybindOption, Library, build_mod, command
from unrealsdk import logging

from . import main_page, menu, pause_page, updates, web

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

__all__ = ("open_menu",)

GAME_DIR = SETTINGS_DIR.parent.parent

open_key = KeybindOption(
    "Open key",
    "F7",
    description="Opens and closes this menu. Works in game, in the game's own menus and at the title screen.",
)


update_option = BoolOption(
    "Check for updates at launch",
    True,
    description=(
        "Once per launch, asks GitHub (on a background thread) whether there are newer versions of"
        " the mods installed by BL4 Mods Setup. Nothing downloads until you press Update all on the"
        " Updates page."
    ),
)


def _check_now(_: ButtonOption) -> None:
    if updates.check_async(GAME_DIR):
        web.say("Checking for updates...")


check_button = ButtonOption("Check for updates now", on_press=_check_now, description="Asks GitHub right away.")


def open_menu(mod_name: str | None = None) -> None:
    """Opens the menu, on the page of the mod with this name if one is given."""
    menu.open_preferred(mod_name)


@command("modmenu", description="Opens the in-game mod menu.")
def modmenu_command(_: argparse.Namespace) -> None:
    # The console is still open on this frame; let it close before we take over input
    menu.S.pending = menu.open_preferred


def _enable() -> None:
    menu.install(lambda: open_key.value, GAME_DIR)
    try:
        logging.info(f"[Mod Menu] {pause_page.sync(GAME_DIR)}")
    except Exception as ex:  # noqa: BLE001  # never let a file problem break the mod
        logging.error(f"[Mod Menu] Couldn't set up MODS in the pause menu: {ex}")
    try:
        logging.info(f"[Mod Menu] {main_page.sync(GAME_DIR)}")
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Mod Menu] Couldn't set up MODS on the title screen: {ex}")
    # Network and files only, on worker threads (updates.py)
    try:
        updates.resume(GAME_DIR)
        if update_option.value:
            updates.check_async(GAME_DIR)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Mod Menu] Couldn't check for updates: {ex}")


mod = build_mod(
    cls=Library,
    options=[open_key, update_option, check_button],
    keybinds=[],
    hooks=[],
    commands=[modmenu_command],
    on_enable=_enable,
    on_disable=menu.uninstall,
)
