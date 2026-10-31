"""
The menu page's script: keeps every item picture the game holds loaded in the page.

Why: the inventory and bank lists reuse about 50 cells while scrolling, handing each one another
item's picture many times a second (seen 2026-10-29: 500-800 picture switches in 3 s). The page
then loads that picture again, and the cell shows nothing until it has: "items disappear then
reappear" while the picture itself was in the game the whole time. A picture the page already
holds switches in at once, so ui/pictures.js holds all of them (a 1 px element each, out of the
way), from the list of paths our state gives. Verified 2026-10-29 by hand: a probe holding the
pictures it had seen made the second scroll through the backpack clean.

The BL4 UI Link (uilink/, shared by our mods; no Mod Menu needed) runs ui/pictures.js in the menu
page and re-runs it whenever it changes.
"""

from __future__ import annotations

import time
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import uilink

KEY = "itempictures"
UI_DIR = "OakGame/Content/uiresources/bl4itempictures"
SCRIPT_URL = "coui://uiresources/bl4itempictures/pictures.js"
UI_FILES = ("pictures.js",)
PUBLISH_S = 1.0


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    # The page files come from a source folder (dev junction), not a packed .sdkmod; set by the forced publish
    from_source = False
    paths: list[str] = []  # noqa: RUF012 - every picture's path, set by __init__
    registered = False


def set_paths(paths: list[str]) -> None:
    if paths != S.paths:
        S.paths = paths
        uilink.changed(KEY)


def state() -> dict[str, Any]:
    return {"paths": S.paths}


def act(_action: dict[str, Any]) -> None:
    """The script only reads our state."""


def register(game_dir: Path) -> None:
    S.game_dir = game_dir
    publish(force=True)
    uilink.register(KEY, SCRIPT_URL, state, act, views=(uilink.MENUS,), mod="Instant Item Pictures")
    S.registered = True


def unregister() -> None:
    if S.registered:
        S.registered = False
        uilink.unregister(KEY)
        uilink.stop()


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4itempictures/, touching only files that changed."""
    now = time.monotonic()
    if S.game_dir is None or (not force and now - S.published_at < PUBLISH_S):
        return
    S.published_at = now
    if not force and not S.from_source:
        # A packed .sdkmod can't change while the game runs: copied once, at register. Checked
        # before anything else: resources.files() on a packed mod opens its zip (Build Presets'
        # copy of this cost 0.9 ms every second)
        return
    root = resources.files(__package__)
    from_source = S.from_source = isinstance(root, Path)
    if not force and not from_source:
        return
    for name in UI_FILES:
        source = root / "ui" / name
        try:
            if from_source:
                st = source.stat()
                stamp = (st.st_size, st.st_mtime_ns)
                if not force and S.published.get(name) == stamp:
                    continue
                S.published[name] = stamp
            content = source.read_bytes()
            target = S.game_dir / UI_DIR / name
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(target)
        except OSError as ex:
            S.published.pop(name, None)
            logging.dev_warning(f"[Instant Item Pictures] couldn't publish {name}: {ex}")
