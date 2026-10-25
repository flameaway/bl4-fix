"""
Loading builds from hotkeys: one per build, plus next / previous saved build. None are bound by
default; set them in the mod's options (the BL4 Mod Menu, F7).

Keys are read from Windows each frame like our other mods' keys (bl4_mod_menu.keys), so they work
in gameplay and in the game's menus, only while the game window is in front, and not while a
build's name is being typed. In gameplay a notice at the top of the screen says what loaded
(notice.py); with the inventory open its own notice does.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from mods_base import get_pc

from . import builds, page
from .notice import Notice

NEXT, PREVIOUS = "next", "previous"


class S:
    # Set by __init__: (action, key) for each hotkey option; action is "1".."5", NEXT or PREVIOUS
    keys: Callable[[], list[tuple[str, str | None]]] = list
    options: Callable[[], tuple[bool, bool]] = lambda: (True, True)
    down: dict[str, bool] = {}  # noqa: RUF012
    # The build last loaded by a hotkey, for next / previous when none matches what you wear
    last = 0


def _keys() -> Any:
    return sys.modules.get("bl4_mod_menu.keys")


def tick() -> None:
    Notice.tick()
    keys = _keys()
    if keys is None:
        return
    pressed = []
    for action, key in S.keys():
        down = bool(key) and keys.key_down(key)
        # Held when the game started (or the key was just bound) doesn't count as a press
        was = S.down.get(action, True)
        S.down[action] = down
        if down and not was:
            pressed.append(action)
    if pressed and page.S.typing is None:
        run(pressed[0])


def _target(action: str) -> int | None:
    if action not in (NEXT, PREVIOUS):
        return int(action)
    # Read now (not the page's cache): which build you wear decides where next / previous go
    squares = builds.summaries()
    saved = [s["n"] for s in squares if not s["empty"]]
    if not saved:
        return None
    current = next((s["n"] for s in squares if not s["empty"] and s.get("current")), S.last)
    step = 1 if action == NEXT else -1
    if current in saved:
        return saved[(saved.index(current) + step) % len(saved)]
    return saved[0] if action == NEXT else saved[-1]


def _tell(title: str, details: list[str] | None = None, *, error: bool = False) -> None:
    page.say(title, details, error=error)
    exts = sys.modules.get("bl4_mod_menu.extensions")
    # With the inventory open, its own notice shows it; otherwise ours, over the game
    if exts is None or not exts.page_open(page.KEY):
        pc = get_pc()
        if pc is not None:
            Notice.show(pc, title, details, error=error)


def run(action: str) -> None:
    try:
        n = _target(action)
        if n is None:
            _tell("No saved builds to switch to.", error=True)
            return
        skills, specs = S.options()
        char = builds.game.character()
        name = ((builds.presets.get(char, n) if char else None) or {}).get("name") or ""

        def tell(headline: str, details: list[str], error: bool = False) -> None:
            _tell(headline.replace(f"Build {n}", f"Build {n} ({name})", 1) if name else headline, details, error=error)

        headline, details = builds.load(n, skills=skills, specializations=specs, on_done=tell)
        S.last = n
        tell(headline, details)
    except builds.Refused as ex:
        _tell(str(ex), error=True)


def stop() -> None:
    Notice.remove()
