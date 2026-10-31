"""
UVH Boss Teleport for Borderlands 4: go straight to the boss an Ultimate Vault Hunter challenge
asks you to kill.

Open the ECHO menu's Challenges, pick Ultimate Vault Hunter and hover a boss challenge, say "Now
Who's Gonna Watch This Core" (kill Core Observer in UVH 2). Its card says "[T] Teleport to Core
Observer": press T and you're taken to the fight, next to its Big Encore machine, the same trip
as the item cards' Big Encore travel. On the same map that's at once (and the menu closes); on
another map it's the game's own fast travel to the nearest station, then a short hop to the arena.
The boss also gets your custom waypoint on the map.

Solo or as host. The card prompt runs in the game's menu page through the BL4 UI Link (uilink/,
no Mod Menu needed). The console works too: `uvhtp` lists the bosses, `uvhtp core observer` (a
boss, a challenge's title, or its number in the list) goes there.

Everything but this file can be hot-reloaded (dev/reload_uvh_teleport.py), and the card's script
and styles reload themselves in the page. The boss list (bosses.json) comes from the game's data:
tools/gen_uvh_teleport.py.
"""

from __future__ import annotations

import argparse
import traceback

from mods_base import SETTINGS_DIR, KeybindOption, build_mod, command
from unrealsdk import logging

from . import frametick, page, travel

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

GAME_DIR = SETTINGS_DIR.parent.parent


def log(msg: str) -> None:
    logging.info(f"[UVH Teleport] {msg}")


teleport_key = KeybindOption(
    "Teleport key",
    "T",
    description=(
        "While hovering an Ultimate Vault Hunter boss challenge in the ECHO menu's Challenges:"
        " takes you to that boss."
    ),
)
page.S.key = lambda: teleport_key.value


class S:
    errors: set[str] = set()  # noqa: RUF012


@frametick.tick("bl4_uvh_teleport")  # once per engine frame (shared hook, frametick/)
def _tick() -> None:
    try:
        page.tick()
        travel.tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in S.errors:
            S.errors.add(repr(ex))
            logging.error("".join(traceback.format_exception(ex)))
        travel.cancel()


@command("uvhtp", description="List the UVH challenge bosses, or teleport to one.")
def uvhtp_command(args: argparse.Namespace) -> None:
    bosses = travel.bosses()
    if not args.boss:
        for n, boss in enumerate(bosses, 1):
            uvh = f" (UVH {boss['uvh']})" if boss["uvh"] is not None else ""
            log(f"{n:2}. {boss['boss']}, {boss['place']}{uvh}: \"{boss['title']}\"")
        log("uvhtp <number | boss | challenge title> to go there.")
        return
    text = " ".join(args.boss)
    boss = bosses[int(text) - 1] if text.isdigit() and 1 <= int(text) <= len(bosses) else travel.find(text)
    if boss is None:
        log(f"No UVH challenge boss matches '{text}'. Type uvhtp for the list.")
        return
    try:
        travel.start(boss)
    except travel.Refused as ex:
        log(str(ex))


uvhtp_command.add_argument("boss", nargs="*", help="a number from the list, a boss or a challenge title")


def _enable() -> None:
    page.register(GAME_DIR)


def _disable() -> None:
    page.unregister()
    travel.cancel()


mod = build_mod(
    options=[teleport_key],
    hooks=[_tick],
    commands=[uvhtp_command],
    on_enable=_enable,
    on_disable=_disable,
)
