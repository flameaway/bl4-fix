"""
The pause menu's page script.

The pause menu lives in the game's menu page, the same page as the BL4 Mod Menu, so this borrows
the Mod Menu's link (bl4_mod_menu.extensions): the menu loads ui/pause.js into the page and re-runs
it whenever it changes. Our state says where the News panel goes; the script does the moving.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

KEY = "tidypause"
UI_DIR = "OakGame/Content/uiresources/bl4tidypause"
SCRIPT_URL = "coui://uiresources/bl4tidypause/pause.js"
UI_FILES = ("pause.js",)
PUBLISH_S = 1.0


def _menu_module(name: str) -> Any:
    return sys.modules.get(f"bl4_mod_menu.{name}")


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    # Set by __init__: the News panel's place ("right", "hidden" or "default")
    news: Callable[[], str] = lambda: "right"


def changed() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.changed(KEY)


def state() -> dict[str, Any]:
    return {"news": S.news()}


def act(_action: dict[str, Any]) -> None:
    """The script only reads our state."""


def register(game_dir: Path) -> bool:
    S.game_dir = game_dir
    exts = _menu_module("extensions")
    if exts is None:
        return False
    publish(force=True)
    exts.register(KEY, SCRIPT_URL, state, act)
    return True


def unregister() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.unregister(KEY)


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4tidypause/, touching only files that changed."""
    now = time.monotonic()
    if S.game_dir is None or (not force and now - S.published_at < PUBLISH_S):
        return
    S.published_at = now
    root = resources.files(__package__)
    from_source = isinstance(root, Path)
    if not force and not from_source:
        return  # a packed .sdkmod can't change while the game runs: copied once, at register
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
            logging.dev_warning(f"[Tidy Pause Menu] couldn't publish {name}: {ex}")
