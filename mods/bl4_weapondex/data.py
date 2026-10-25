"""
The dex itself: every boss with a dedicated drop table and every legendary and pearl, from
dex.json (made from the game's data by tools/gen_weapondex.py).

Plain Python, no game access.
"""

from __future__ import annotations

import json
import re
import threading
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from . import pak

# Live actors are named after their def, plus a number when there's more than one:
# Char_GruntAssault, Char_GruntAssault_2147291575
_INSTANCE_SUFFIX = re.compile(r"_\d{5,}$")


@cache
def dex() -> dict[str, Any]:
    return json.loads((resources.files(__package__) / "dex.json").read_text(encoding="utf-8"))


@cache
def _by_char() -> dict[str, str]:
    return {char: boss["id"] for boss in dex()["bosses"] for char in boss["chars"]}


@cache
def _by_display() -> dict[str, str]:
    return {name: boss["id"] for boss in dex()["bosses"] for name in boss["names"]}


@cache
def boss_ids() -> frozenset[str]:
    return frozenset(boss["id"] for boss in dex()["bosses"])


def boss_name(boss_id: str) -> str:
    return next((b["name"] for b in dex()["bosses"] if b["id"] == boss_id), boss_id)


def boss_for(actor_name: str, display_def: str | None = None) -> str | None:
    """The boss an actor is, by its object name (its def) or the UI name it shows."""
    name = _INSTANCE_SUFFIX.sub("", actor_name).lower()
    if name in _by_char():
        return _by_char()[name]
    if display_def:
        return _by_display().get(display_def.lower())
    return None


def find_boss(text: str) -> str | None:
    """A boss by id or (part of) its name, for console commands."""
    want = text.strip().lower()
    bosses = dex()["bosses"]
    for test in (lambda b: b["id"] == want, lambda b: b["name"].lower() == want, lambda b: want in b["name"].lower()):
        found = [b["id"] for b in bosses if test(b)]
        if len(found) == 1:
            return found[0]
    return None


_data_ok: bool | None = None
_data_thread: threading.Thread | None = None


def _check_data(game_dir: Path) -> None:
    global _data_ok
    paks = sorted((game_dir / "OakGame" / "Content" / "Paks").glob("*.pak"))
    prints = dex().get("source_data") or {}
    try:
        ok = bool(prints) and all(
            (entry := pak.winning_entry(paks, path)) is not None and entry.sha1 == sha1
            for path, sha1 in prints.items()
        )
    except Exception:  # noqa: BLE001 - can't tell; don't cry wolf
        ok = True
    _data_ok = ok


def data_current(game_dir: Path) -> bool:
    """
    Whether the game files dex.json was made from are unchanged (checked once per session).

    The check reads 360 pak indexes (1.3 s cold, measured): on the game thread it froze the game
    for 2.3 s on the first dex poll after each load. It runs on a worker thread; until it's done
    this says True (no "outdated" warning yet).
    """
    global _data_thread
    if _data_ok is None and (_data_thread is None or not _data_thread.is_alive()):
        _data_thread = threading.Thread(target=_check_data, args=(game_dir,), name="bl4_weapondex.data", daemon=True)
        _data_thread.start()
    return True if _data_ok is None else _data_ok
