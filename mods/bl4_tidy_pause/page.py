"""
The pause menu's page script.

The pause menu lives in the game's menu page. The BL4 UI Link (uilink/, shared by our mods; no Mod
Menu needed) runs ui/pause.js there and re-runs it whenever it changes; until the link's pak has
loaded (the first start after installing) the BL4 Mod Menu's own link carries it if installed.
Our state says where the News panel goes; the script does the moving.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import uilink

KEY = "tidypause"
UI_DIR = "OakGame/Content/uiresources/bl4tidypause"
SCRIPT_URL = "coui://uiresources/bl4tidypause/pause.js"
UI_FILES = ("pause.js",)
PUBLISH_S = 1.0


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    # The page files come from a source folder (dev junction), not a packed .sdkmod; set by the forced publish
    from_source = False
    # Set by __init__: the News panel's place ("right", "hidden" or "default")
    news: Callable[[], str] = lambda: "right"


def changed() -> None:
    uilink.changed(KEY)


def state() -> dict[str, Any]:
    return {"news": S.news()}


def act(_action: dict[str, Any]) -> None:
    """The script only reads our state."""


def register(game_dir: Path) -> None:
    S.game_dir = game_dir
    publish(force=True)
    uilink.register(KEY, SCRIPT_URL, state, act, views=(uilink.MENUS,), mod="Tidy Pause Menu")


def unregister() -> None:
    uilink.unregister(KEY)
    uilink.stop()


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4tidypause/, touching only files that changed."""
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
            logging.dev_warning(f"[Tidy Pause Menu] couldn't publish {name}: {ex}")
