"""
Build Presets for Borderlands 4: five build slots per character, right in the inventory.

Open the inventory: under the Reward Center button there are five squares. Click an empty one to
save your build into it; click a saved one to load it. Hover a square to see what's in it (each
item by name, and the skill trees your points are in, in the skill screen's colors), and use its
small buttons to save over it, name it (type, then Enter) or clear it.

A build is everything you have equipped (four weapons, shield, ordnance, repkit, enhancement,
class mod), your skill points, action skill, augments and capstone, and your specializations
(points and perks). Loading puts each saved item back into its slot, but only that exact item:
if it's no longer in your backpack the slot is left as it is. Skills are refunded and spent again
only where they differ (free, like the skill screen's own reset).

Hotkeys (none bound by default; set them in this mod's options): load build 1-5, next / previous
saved build. They work in gameplay too, where a notice at the top of the screen says what loaded.

The squares need no other mod: they run through the BL4 UI Link (uilink/, a pak our mods share
that replaces none of the game's pages). They show from the game start after the first one with
this mod (paks only load at startup), sooner if the BL4 Mod Menu is installed. Or use the console:
`build` lists the slots, `build save 2`, `build load 2`, `build clear 2`, `build rename 2 Boss killer`.

Co-op: saving works anywhere. Equipping and respeccing belong to the host, so a client's load is
done by the host's copy of this mod (BL4 Direct's co-op channel): the host needs Build Presets for
a friend to load theirs, and the friend needs it for the squares.

Everything but this file can be hot-reloaded (dev/reload_builds.py), and the squares' script and
styles reload themselves in the page.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from mods_base import SETTINGS_DIR, BoolOption, GroupedOption, KeybindOption, build_mod, command, hook
from unrealsdk import logging
from unrealsdk.hooks import Type

from . import builds, hotkeys, page

__version__ = "0.2.1"
__version_info__ = (0, 2, 1)

GAME_DIR = SETTINGS_DIR.parent.parent
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0


def log(msg: str) -> None:
    logging.info(f"[Build Presets] {msg}")


skills_option = BoolOption(
    "Load skills",
    True,
    description="Loading a build also puts back its skill points, action skill, augments and capstone.",
)
specs_option = BoolOption(
    "Load specializations",
    True,
    description="Loading a build also puts back its specialization points and perks.",
)

load_keys = [
    KeybindOption(f"Load build {n}", None, description=f"Loads build {n} (in gameplay or in the game's menus).")
    for n in range(1, builds.presets.SLOTS + 1)
]
next_key = KeybindOption("Next build", None, description="Loads the next saved build after the one you're wearing.")
previous_key = KeybindOption("Previous build", None, description="Loads the saved build before the one you're wearing.")
hotkey_group = GroupedOption("Hotkeys", [*load_keys, next_key, previous_key])


def _options() -> tuple[bool, bool]:
    return skills_option.value, skills_option.value and specs_option.value


page.S.options = _options
hotkeys.S.options = _options
# Rebinding takes effect within this long (the list was rebuilt every frame, unbound keys included)
HOTKEYS_CHECK_S = 1.0


class _Hotkeys:
    at = -1e9
    bound: list[tuple[str, str]] = []  # noqa: RUF012


def _bound_hotkeys() -> list[tuple[str, str]]:
    """The hotkeys that have a key (none by default): hotkeys.tick reads only those."""
    now = time.monotonic()
    if now - _Hotkeys.at >= HOTKEYS_CHECK_S:
        _Hotkeys.at = now
        pairs = [*((str(n), option.value) for n, option in enumerate(load_keys, 1)),
                 (hotkeys.NEXT, next_key.value), (hotkeys.PREVIOUS, previous_key.value)]  # fmt: skip
        _Hotkeys.bound = [(action, key) for action, key in pairs if key]
        # A key bound later starts as "held" (hotkeys.tick's default), so it needs a fresh press
        for action, key in pairs:
            if not key:
                hotkeys.S.down.pop(action, None)
    return _Hotkeys.bound


hotkeys.S.keys = _bound_hotkeys


class S:
    errors: set[str] = set()  # noqa: RUF012


@hook(TICK_FUNC, Type.POST)
def _tick(*_: Any) -> None:
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    try:
        page.tick()
        hotkeys.tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in S.errors:
            S.errors.add(repr(ex))
            logging.exception(ex)


@command("build", description="List, save, load or clear your build presets.")
def build_command(args: argparse.Namespace) -> None:
    try:
        if args.action == "list":
            for square in builds.summaries():
                if square["empty"]:
                    log(f"{square['n']}. (empty)")
                    continue
                title = " · ".join(x for x in (square["name"], square.get("action"), square["tree_text"]) if x)
                log(f"{square['n']}. {title} · saved {square['saved']}")
                for gear in square["gear"]:
                    log(f"     {gear['slot']}: {gear['label']}{'' if gear['have'] else ' (not in your backpack)'}")
            return
        if args.n is None or not 1 <= args.n <= builds.presets.SLOTS:
            log(f"Give a slot from 1 to {builds.presets.SLOTS}.")
            return
        if args.action == "save":
            log(builds.save(args.n))
        elif args.action == "load":
            headline, details = builds.load(args.n, skills=skills_option.value,
                                            specializations=skills_option.value and specs_option.value)  # fmt: skip
            log(" ".join([headline, *details]))
        elif args.action == "clear":
            log(builds.clear(args.n))
        elif args.action == "rename":
            log(builds.rename(args.n, " ".join(args.name)))
    except builds.Refused as ex:
        log(str(ex))


build_command.add_argument("action", nargs="?", default="list", choices=("list", "save", "load", "clear", "rename"))
build_command.add_argument("n", type=int, nargs="?", help="build slot (1-5)")
build_command.add_argument("name", nargs="*", help="rename: the new name (nothing clears it)")


def _enable() -> None:
    page.register(GAME_DIR)


def _disable() -> None:
    page.unregister()
    hotkeys.stop()


mod = build_mod(
    options=[skills_option, specs_option, hotkey_group],
    hooks=[_tick],
    commands=[build_command],
    on_enable=_enable,
    on_disable=_disable,
)

if builds.coop is not None:
    builds.coop.feature(builds.FEATURE, lambda: mod.is_enabled)
