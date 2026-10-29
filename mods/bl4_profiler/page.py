"""
The Mod Profiler's page in the BL4 Mod Menu screen: Run / Stop, then the last run's results, one
row per mod (hover a row for its hooks and threads).

Same approach as BL4 Direct Connect's page (bl4_direct/page.py): bl4_mod_menu.extensions loads
ui/profiler.js into the menu page, which draws through the Mod Menu's ns.modPages hook with the
Mod Menu's own rows and buttons. State comes from state() (GET /ext?k=bl4profiler), clicks back
through act(). Without the Mod Menu the mod still works: the options and the `modprofile` command.
"""

from __future__ import annotations

import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR
from unrealsdk import logging

from . import bench

KEY = "bl4profiler"
MOD_NAME = "BL4 Mod Profiler"
GAME_DIR = SETTINGS_DIR.parent.parent
UI_DIR = "OakGame/Content/uiresources/bl4profiler"
SCRIPT_URL = "coui://uiresources/bl4profiler/profiler.js"
UI_FILES = ("profiler.js", "profiler.css")
PUBLISH_S = 1.0
# Loaded from a folder (the dev junction): its files can change while the game runs
DEV = (Path(__file__).parent / "ui").is_dir()
REGISTER_RETRY_S = 1.0
SHOWN_S = 1.5
# Options the page replaces with its own rows (they stay in other menus)
HIDDEN_OPTIONS = ("Run benchmark",)


class S:
    registered = False
    register_tried = 0.0
    published_at = 0.0
    toast: dict[str, Any] = {"id": 0, "text": "", "error": False}  # noqa: RUF012
    length = lambda: 20.0  # noqa: E731 - set by __init__ (the slider)


def _exts() -> Any:
    return sys.modules.get("bl4_mod_menu.extensions")


def changed() -> None:
    if (exts := _exts()) is not None:
        exts.changed(KEY)


def toast(text: str, *, error: bool = False) -> None:
    S.toast = {"id": S.toast["id"] + 1, "text": text, "error": error}
    changed()


def state() -> dict[str, Any]:
    return {
        "phase": bench.B.phase,
        "left": round(bench.left(), 1),
        "length": S.length(),
        "message": bench.B.message,
        "result": bench.B.result,
        "report": str(bench.REPORT),
        "toast": S.toast,
        "hide": HIDDEN_OPTIONS,
    }


def act(action: dict[str, Any]) -> None:
    match action.get("do"):
        case "run":
            bench.request(S.length())
        case "stop":
            bench.cancel()
        case "open":
            if not bench.open_report():
                toast("No report yet: run a benchmark first.", error=True)
        case _:
            pass


def _register() -> None:
    now = time.monotonic()
    if S.registered or now - S.register_tried < REGISTER_RETRY_S:
        return
    S.register_tried = now
    exts = _exts()
    if exts is None:
        return
    publish(force=True)
    exts.register(KEY, SCRIPT_URL, state, act)
    S.registered = True


def unregister() -> None:
    if S.registered and (exts := _exts()) is not None:
        exts.unregister(KEY)
    S.registered = False


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4profiler/, touching only files that changed."""
    now = time.monotonic()
    if not force and now - S.published_at < PUBLISH_S:
        return
    S.published_at = now
    for name in UI_FILES:
        try:
            content = (resources.files(__package__) / "ui" / name).read_bytes()
            target = Path(GAME_DIR) / UI_DIR / name
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
            logging.dev_warning(f"[Mod Profiler] couldn't publish {name}: {ex}")


def tick() -> None:
    """A few times a second, from the mod's tick."""
    if not S.registered:
        _register()
        return
    if DEV and (exts := _exts()) is not None and exts.page_open(KEY, SHOWN_S):
        publish()
