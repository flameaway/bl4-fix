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

Discover and Repositories (repos.py): add other modders' repositories (a GitHub "owner/name" or
an https link to an index, docs/mod-repos.md), browse the mods you don't have with their pictures,
install and update them without a restart (hotload.py). Mods dropped into sdk_mods (Open mods
folder) load from the Discover page too, also without a restart.

A mod can also open the menu on its own page, e.g. from a button in its own UI:

    import sys
    if (menu := sys.modules.get("bl4_mod_menu")) is not None:
        menu.open_menu("BL4 Shoot to Open")

Everything can be hot-reloaded through the bridge (dev/reload_mod_menu.py), and the page's
scripts reload themselves when they change; this file only holds the mod itself.
"""

from __future__ import annotations

import argparse
import sys

from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, KeybindOption, Library, build_mod, command
from unrealsdk import logging

from . import hotload, main_page, menu, pause_page, repos, uilink, updates, web

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
        " the mods installed by BL4 Fix Setup. Nothing downloads until you press Update all on the"
        " Updates page."
    ),
)


def _announce_changed(_: BoolOption, value: bool) -> None:
    updates.set_announce(value)


announce_option = BoolOption(
    "Tell me about new mods",
    True,
    description=(
        "When a release adds a mod you've never installed, MODS shows a \"!\" and the Updates page"
        " marks it NEW, once per mod. Off: no badge for new mods. Either way, every mod you don't have"
        " is listed on the Updates page with an Install button."
    ),
    on_change=_announce_changed,
)


def _check_now(_: ButtonOption) -> None:
    if updates.check_async(GAME_DIR):
        web.say("Checking for updates...")


check_button = ButtonOption("Check for updates now", on_press=_check_now, description="Asks GitHub right away.")

# Only matters once the player added a repository, and adding one is asking for its mods: on
repos_option = BoolOption(
    "Check repositories at launch",
    True,
    description=(
        "Once per launch, reads the mod repositories you added (on a background thread), so Discover"
        " lists their mods and Updates their new versions. Nothing downloads until you press Install"
        " or Update. With no repositories added, nothing is checked."
    ),
)


def _say(text: str, *, error: bool = False) -> None:
    (logging.error if error else logging.info)(f"[Mod Menu] {text}")


@command("modrepo", description="Mod repositories: modrepo add <owner/name or https link> | remove <key> | list | check")
def modrepo_command(args: argparse.Namespace) -> None:
    words = list(args.words)
    verb = words[0].lower() if words else "list"
    if verb == "add" and len(words) > 1:
        repos.add_async(words[1])
        _say(f"Checking {words[1]}... confirm it on the Repositories page (MODS), or type modrepo confirm.")
    elif verb == "confirm":
        repos.confirm_add()
    elif verb == "remove" and len(words) > 1:
        repos.remove_source(repos.normalize(words[1]) or words[1])
    elif verb == "check":
        repos.refresh_async()
    else:
        _say(f"{repos.OFFICIAL_NAME} (built in): {repos.official_key()}")
        for source in repos.R.sources:
            info = repos.R.info.get(source["key"], {})
            _say(f"{source['key']}: {info.get('name', '')} {info.get('message', '')}".rstrip())
        if not repos.R.sources:
            _say("No repositories added. modrepo add <owner/name or https link>")


modrepo_command.add_argument("words", nargs="*")


@command("modload", description="Loads a mod from the mods folder without a restart: modload <module name>, or modload to list them")
def modload_command(args: argparse.Namespace) -> None:
    if not args.name:
        hotload.scan(GAME_DIR)
        found = hotload.H.found
        for item in found:
            _say(f"{item['id']}: {item['name']} {item['version']} ({'changed on disk' if item['kind'] == 'changed' else 'not loaded'})")
        if not found:
            _say("Every mod in the mods folder is loaded.")
        return
    try:
        if args.name in sys.modules:
            _say(hotload.reload(GAME_DIR, args.name))
        else:
            _say(hotload.load(GAME_DIR, args.name))
    except Exception as ex:  # noqa: BLE001
        _say(str(ex), error=True)


modload_command.add_argument("name", nargs="?")


def open_menu(mod_name: str | None = None) -> None:
    """Opens the menu, on the page of the mod with this name if one is given."""
    menu.open_preferred(mod_name)


@command("modmenu", description="Opens the in-game mod menu.")
def modmenu_command(_: argparse.Namespace) -> None:
    # The console is still open on this frame; let it close before we take over input
    menu.S.pending = menu.open_preferred


# The BL4 UI Link (uilink/, shared by our mods) runs ui/link.js in the pause menu's page and on the
# title screen, which loads ui/loader.js there (MODS, the MODS screen, other mods' extensions).
# Until 0.2.0 two page paks of our own did that (4245 / 4247): pause_page / main_page remove them.
LINK_KEY = "modmenu"
LINK_SCRIPT = "coui://uiresources/bl4modmenu/link.js"


def _enable() -> None:
    menu.install(lambda: open_key.value, GAME_DIR)
    uilink.register(LINK_KEY, LINK_SCRIPT, dict, lambda _action: None, views=(uilink.MENUS, "menu_main"), mod="BL4 Mod Menu")
    for page in (pause_page, main_page):
        try:
            if line := page.sync(GAME_DIR):
                logging.info(f"[Mod Menu] {line}")
        except Exception as ex:  # noqa: BLE001  # never let a file problem break the mod
            logging.error(f"[Mod Menu] Couldn't remove {page.PAK_NAME}: {ex}")
    # Network and files only, on worker threads (updates.py)
    try:
        updates.set_announce(announce_option.value)
        updates.resume(GAME_DIR)
        if update_option.value:
            updates.check_async(GAME_DIR)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Mod Menu] Couldn't check for updates: {ex}")
    try:
        repos.start(GAME_DIR, check=repos_option.value)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Mod Menu] Couldn't read the mod repositories: {ex}")


def _disable() -> None:
    menu.uninstall()
    uilink.unregister(LINK_KEY)
    uilink.stop()


mod = build_mod(
    cls=Library,
    options=[open_key, update_option, announce_option, check_button, repos_option],
    keybinds=[],
    hooks=[],
    commands=[modmenu_command, modrepo_command, modload_command],
    on_enable=_enable,
    on_disable=_disable,
)
