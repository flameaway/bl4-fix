"""
Which bosses each character has beaten, saved in the SDK's settings folder
(settings/weapondex_progress.json), keyed by the character's id (PlayerState.ActiveCharGuid):

    {"<character id>": {"defeated": {"<boss id>": <unix time first beaten>}}}

Kept apart from the game's save, which doesn't record per-boss kills (savegame.py reads what it
does record). The dex shows every character's wins together (defeated_anywhere), or, with
"Per-character Kairosdex" on, only the loaded character's (scope).
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging

FILE: Path = SETTINGS_DIR / "weapondex_progress.json"

_all: dict[str, Any] | None = None


class Mode:
    # Set by __init__: whether the "Per-character Kairosdex" option is on (read from any thread)
    per_char: Callable[[], bool] = lambda: False  # noqa: E731


def _load() -> dict[str, Any]:
    global _all
    if _all is None:
        try:
            loaded = json.loads(FILE.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("not an object")  # noqa: TRY301
            _all = loaded
        except FileNotFoundError:
            _all = {}
        except (OSError, ValueError) as ex:
            # Never overwrite an unreadable file with an empty one (it holds every recorded kill)
            _all = {}
            bad = FILE.with_name(f"weapondex_progress.bad-{int(time.time())}.json")
            try:
                FILE.replace(bad)
            except OSError:
                # Can't move it (locked?): keep a copy instead, the next save writes over the file
                try:
                    shutil.copyfile(FILE, bad)
                except OSError as copy_ex:
                    logging.error(f"[Kairosdex] {FILE.name} was unreadable ({ex}) and couldn't be kept aside ({copy_ex})")
                    return _all
            logging.error(f"[Kairosdex] {FILE.name} was unreadable ({ex}); moved aside to {bad.name}")
    return _all


def _save() -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(FILE.name + ".tmp")
        tmp.write_text(json.dumps(_load(), indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(FILE)
    except OSError as ex:
        logging.error(f"[Kairosdex] Couldn't save progress: {ex}")


def character() -> str | None:
    """The id of the character being played, or None (title screen, loading)."""
    pc = get_pc(possibly_loading=True)
    state = getattr(pc, "PlayerState", None) if pc is not None else None
    if state is None:
        return None
    guid = state.ActiveCharGuid
    parts = [guid.A, guid.B, guid.C, guid.D]
    if not any(parts):
        return None
    return "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts)


def scope() -> str | None:
    """
    The character whose own dex shows ("Per-character Kairosdex" on and a character loaded), or
    None: the account's. Game thread.
    """
    return character() if Mode.per_char() else None


def defeated(char: str | None = None) -> dict[str, float]:
    char = char or character()
    if char is None:
        return {}
    return (_load().get(char) or {}).get("defeated") or {}


def defeated_anywhere() -> dict[str, float]:
    """Every character's wins together (the dex is account-wide), each at its first time."""
    out: dict[str, float] = {}
    for record in _load().values():
        for boss, when in ((record or {}).get("defeated") or {}).items():
            out[boss] = min(out.get(boss, when), when)
    return out


def mark(boss_id: str, char: str | None = None) -> bool:
    """Records a win. False if there's no character or it was already recorded."""
    char = char or character()
    if char is None:
        return False
    record = _load().setdefault(char, {}).setdefault("defeated", {})
    if boss_id in record:
        return False
    record[boss_id] = round(time.time())
    _save()
    return True


def forget(boss_id: str | None = None, char: str | None = None) -> None:
    """Forgets one boss, or every boss when boss_id is None, for this character."""
    char = char or character()
    if char is None:
        return
    record = (_load().get(char) or {}).get("defeated")
    if record is None:
        return
    if boss_id is None:
        record.clear()
    else:
        record.pop(boss_id, None)
    _save()
