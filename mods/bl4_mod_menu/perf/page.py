"""
The Performance page in the MODS screen: Run / Stop, the length, then the last run's results, one
row per mod (hover a row for its hooks and threads).

menu.js draws it as one of its own pages (with Updates, Discover, Repositories); the content comes
from ui/perf.js, which this module registers as one of the Mod Menu's extensions (extensions.py) and
which draws through the same ns.modPages hook other mods' pages use, under the page key PAGE.
State comes from state() (GET /ext?k=bl4perf), clicks back through act().
"""

from __future__ import annotations

import time
from importlib import resources
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR
from unrealsdk import logging

from .. import extensions
from . import bench

KEY = "bl4perf"
GAME_DIR = SETTINGS_DIR.parent.parent
UI_DIR = "OakGame/Content/uiresources/bl4modmenu"
SCRIPT_URL = "coui://uiresources/bl4modmenu/perf.js"
UI_FILES = ("perf.js", "perf.css")
PUBLISH_S = 1.0
_PACKAGE = __package__.rpartition(".")[0]  # bl4_mod_menu: ui/ lives there
# Loaded from a folder (the dev junction): its files can change while the game runs
DEV = (Path(__file__).parent.parent / "ui").is_dir()
SHOWN_S = 1.5
LENGTHS = (10.0, 20.0, 30.0, 60.0)


class S:
    registered = False
    published_at = 0.0
    toast: dict[str, Any] = {"id": 0, "text": "", "error": False}  # noqa: RUF012
    length = 20.0


def changed() -> None:
    extensions.changed(KEY)


def toast(text: str, *, error: bool = False) -> None:
    S.toast = {"id": S.toast["id"] + 1, "text": text, "error": error}
    changed()


def state() -> dict[str, Any]:
    return {
        "phase": bench.B.phase,
        "left": round(bench.left(), 1),
        "length": S.length,
        "message": bench.B.message,
        "result": bench.B.result,
        "report": str(bench.REPORT),
        "toast": S.toast,
    }


def act(action: dict[str, Any]) -> None:
    match action.get("do"):
        case "run":
            bench.request(S.length)
        case "stop":
            bench.cancel()
        case "length":
            # The next length in LENGTHS (cycles)
            later = [x for x in LENGTHS if x > S.length]
            S.length = later[0] if later else LENGTHS[0]
            changed()
        case "open":
            if not bench.open_report():
                toast("No report yet: run a benchmark first.", error=True)
        case _:
            pass


def register() -> None:
    publish(force=True)
    extensions.register(KEY, SCRIPT_URL, state, act)
    S.registered = True


def unregister() -> None:
    if S.registered:
        extensions.unregister(KEY)
    S.registered = False


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4modmenu/, touching only files that changed."""
    now = time.monotonic()
    if not force and now - S.published_at < PUBLISH_S:
        return
    S.published_at = now
    for name in UI_FILES:
        try:
            content = (resources.files(_PACKAGE) / "ui" / name).read_bytes()
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
            logging.dev_warning(f"[Performance] couldn't publish {name}: {ex}")


def tick() -> None:
    """A few times a second: dev hot reload of the page's files while it shows."""
    if DEV and extensions.page_open(KEY, SHOWN_S):
        publish()
