"""
The saved builds: five per character, in the SDK's settings folder (settings/build_presets.json),
keyed by the character's id (PlayerState.ActiveCharGuid):

    {"<character id>": {"1": <build>, ..., "5": <build>}}

A build:

    {"saved": <unix time>,
     "gear": {"<equip slot>": {"serial": "@Ug...", "label": "Legendary Jakobs Shotgun",
                               "rarity": "legendary", "kind": "weapon"}},
     "skills": {"<graph>": {"type": 0, "points": {"<node>": n}} | {"type": 1, "active": {"<node>": level}}}}

Plain Python, no game access: page.py and __init__.py read the game and pass what to keep.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR
from unrealsdk import logging

FILE: Path = SETTINGS_DIR / "build_presets.json"
SLOTS = 5

_all: dict[str, Any] | None = None
# Goes up whenever the builds change (or are read again): the page's cache redraws
version = 0
_unreadable = False  # logged once: the file is there but can't be read right now


def _load() -> dict[str, Any]:
    """
    The saved builds. While the file can't be read this is an empty dict that isn't kept, and
    _save() refuses, so a passing problem never ends with the file overwritten by one build.
    """
    global _all, _unreadable
    if _all is None:
        try:
            text = FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        except OSError as ex:
            if not _unreadable:
                logging.error(f"[Build Presets] Couldn't read the builds ({ex}); not saving until it reads.")
            _unreadable = True
            return {}
        loaded: Any = {}
        if text is not None:
            try:
                loaded = json.loads(text)
            except ValueError:
                loaded = None
        if not isinstance(loaded, dict):
            # Keep the broken file for the player instead of saving over it
            bad = FILE.with_name(f"{FILE.stem}.bad-{int(time.time())}{FILE.suffix}")
            try:
                FILE.replace(bad)
            except OSError as ex:
                if not _unreadable:
                    logging.error(f"[Build Presets] {FILE.name} is damaged and couldn't be moved aside ({ex}); not saving.")
                _unreadable = True
                return {}
            logging.error(f"[Build Presets] {FILE.name} was damaged; it's kept as {bad.name} and the builds start empty.")
            loaded = {}
        _all, _unreadable = loaded, False
        _bump()
    return _all


def _bump() -> None:
    global version
    version += 1


def _save() -> bool:
    """Writes the builds; False if it couldn't (logged)."""
    if _all is None:
        return False  # couldn't read the file: don't replace it
    _bump()
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(FILE.name + ".tmp")
        tmp.write_text(json.dumps(_all, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(FILE)
    except OSError as ex:
        logging.error(f"[Build Presets] Couldn't save the builds: {ex}")
        return False
    return True


def _builds(char: str) -> dict[str, Any]:
    builds = _load().get(char)
    return builds if isinstance(builds, dict) else {}


def get(char: str, n: int) -> dict[str, Any] | None:
    build = _builds(char).get(str(n))
    return build if isinstance(build, dict) else None


def all_of(char: str) -> list[dict[str, Any] | None]:
    return [get(char, n) for n in range(1, SLOTS + 1)]


def put(char: str, n: int, build: dict[str, Any]) -> bool:
    """Saves build n; False if the file couldn't be written (logged)."""
    builds = _builds(char)
    builds[str(n)] = build
    _load()[char] = builds
    return _save()


def clear(char: str, n: int) -> bool:
    builds = _builds(char)
    if builds.pop(str(n), None) is not None:
        return _save()
    return True
