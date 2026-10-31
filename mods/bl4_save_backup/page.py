"""
Save Backups' page in the BL4 Mod Menu screen (MODS > BL4 Save Backups).

Copied from BL4 Direct Connect's page (bl4_direct/page.py): bl4_mod_menu.extensions loads
ui/backups.js into the menu page, which draws this mod's page (ns.modPages) from the Mod Menu's
own rows and buttons. state() here is small (what's busy, the staged restore, a toast, the view's
revision); the big view (every character's history, the bank's, every backup) is a loose file the
worker writes (backups._publish) and the script fetches when the revision moves.

Clicks come back through act() on the game thread; everything that touches files goes to the
worker. "Quit game" closes the game normally (it saves first), a moment after the click.
"""

from __future__ import annotations

import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import ENGINE, SETTINGS_DIR, get_pc
from unrealsdk import logging

from . import backups

KEY = "bl4savebackup"
MOD_NAME = "BL4 Save Backups"  # pyproject name: the Mod Menu draws our page for this mod
GAME_DIR = SETTINGS_DIR.parent.parent
UI_DIR = GAME_DIR / "OakGame/Content/uiresources/bl4savebackup"
SCRIPT_URL = "coui://uiresources/bl4savebackup/backups.js"
UI_FILES = ("backups.js", "backups.css")
PUBLISH_S = 1.0
DEV = (Path(__file__).parent / "ui").is_dir()
REGISTER_RETRY_S = 1.0
SHOWN_S = 1.5  # the script polls every 0.5 s while the page shows
REFRESH_S = 5.0  # while the page shows, re-check the live saves this often (a stat, on the worker)
QUIT_DELAY_S = 0.6


class S:
    registered = False
    register_tried = 0.0
    published_at = 0.0
    refreshed_at = 0.0
    quit_at = 0.0


def _menu_module(name: str) -> Any:
    return sys.modules.get(f"bl4_mod_menu.{name}")


def changed() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.changed(KEY)


def state() -> dict[str, Any]:
    return {
        "rev": backups.W.rev,
        "view": "coui://uiresources/bl4savebackup/view.json",
        "busy": backups.W.busy,
        "toast": backups.W.toast,
        "pending": backups.W.pending,
        "result": backups.W.result,
        "keep": backups.W.keep,
        "quitting": S.quit_at > 0,
    }


def act(action: dict[str, Any]) -> None:
    W = backups
    name = str(action.get("backup") or "")
    match action.get("do"):
        case "backup":
            W.submit("backup", lambda: W.backup("manual"), "Backing up your saves...")
        case "keep":
            keep = bool(action.get("keep"))
            W.submit(f"keep {name}", lambda: W.set_keep(name, keep))
        case "restore":
            kind, fname = str(action.get("kind")), str(action.get("file") or "")
            if kind in ("character", "bank", "profile", "all") and name:
                W.submit("stage", lambda: W.stage(kind, name, fname), "Checking the backup...")
        case "cancel":
            target = action.get("target")
            W.submit("cancel", lambda: W.cancel(str(target) if target else None))
        case "dismiss":
            W.submit("dismiss", W.dismiss_result)
        case "folder":
            W.submit("folder", W.open_folder)
        case "refresh":
            W.submit("refresh", W.refresh)
        case "quit":
            if W.W.pending:
                S.quit_at = time.monotonic() + QUIT_DELAY_S
                changed()
        case _:
            pass


def _quit_game() -> None:
    """Closes the game normally (it saves first; the helper restores after that)."""
    world = ENGINE.GameViewport.World
    system = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    system.QuitGame(world, get_pc(possibly_loading=True), 0, False)  # EQuitPreference::Quit


# region Link


def _register() -> None:
    now = time.monotonic()
    if S.registered or now - S.register_tried < REGISTER_RETRY_S:
        return
    S.register_tried = now
    exts = _menu_module("extensions")
    if exts is None:
        return
    publish(force=True)
    exts.register(KEY, SCRIPT_URL, state, act)
    S.registered = True


def unregister() -> None:
    S.quit_at = 0.0
    if S.registered and (exts := _menu_module("extensions")) is not None:
        exts.unregister(KEY)
    S.registered = False


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4savebackup/, touching only files that changed."""
    now = time.monotonic()
    if not force and now - S.published_at < PUBLISH_S:
        return
    S.published_at = now
    for name in UI_FILES:
        try:
            content = (resources.files(__package__) / "ui" / name).read_bytes()
            target = UI_DIR / name
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
            logging.dev_warning(f"[Save Backups] couldn't publish {name}: {ex}")


def tick() -> None:
    """A few times a second (the shared frame tick)."""
    if not S.registered:
        _register()
        return
    now = time.monotonic()
    if S.quit_at and now >= S.quit_at:
        S.quit_at = 0.0
        _quit_game()
        return
    exts = _menu_module("extensions")
    if exts is None or not exts.page_open(KEY, SHOWN_S):
        return
    if now - S.refreshed_at > REFRESH_S:
        S.refreshed_at = now
        backups.submit("refresh", backups.refresh)
    if DEV:
        publish()


# endregion
