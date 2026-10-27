"""
Where every fast travel station stands, from stations.json (tools/gen_instant_travel.py).

The station handle the ECHO map passes to RequestTeleportToStation is unresolved in Python: only
its name (`World_P.FT_GRA_West_Silo2`), no position or type, and a far station's actor isn't
streamed in. So the positions come from the game's NCS data, baked ahead of time.

stations.json is pinned to the game files it came from ("source_data" SHA1s). `start_check()`
compares them on a worker thread (the pak lookup takes about a second); until it has passed,
and for good after a game patch changed them, `find()` returns nothing and the game travels as
normal.
"""

from __future__ import annotations

import json
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import pak

_data: dict[str, Any] | None = None
_by_name: dict[str, tuple[float, float, float, float, bool]] | None = None
_current: bool | None = None
_check_thread: threading.Thread | None = None


def _load() -> dict[str, Any]:
    global _data
    if _data is None:
        try:
            _data = json.loads((resources.files(__package__) / "stations.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            logging.error(f"[Instant Travel] stations.json: {ex!r}")
            _data = {"stations": {}}
    return _data


def _check(game_dir: Path) -> None:
    global _current
    paks = sorted((game_dir / "OakGame" / "Content" / "Paks").glob("*.pak"))
    prints = _load().get("source_data") or {}
    try:
        _current = bool(prints) and all(
            (entry := pak.winning_entry(paks, path)) is not None and entry.sha1 == sha1 for path, sha1 in prints.items()
        )
    except Exception as ex:  # noqa: BLE001 - can't tell: stay out of the way
        logging.warning(f"[Instant Travel] couldn't check the station data: {ex!r}")
        _current = False
    if not _current:
        logging.warning("[Instant Travel] the game's station data changed since stations.json was made:"
                        " fast travel goes through the portal until the mod is updated")  # fmt: skip


def start_check(game_dir: Path) -> None:
    global _check_thread
    if _current is None and (_check_thread is None or not _check_thread.is_alive()):
        _check_thread = threading.Thread(target=_check, args=(game_dir,), name="bl4_instant_travel.data", daemon=True)
        _check_thread.start()


def find(name: str) -> tuple[float, float, float, float, bool] | None:
    """(x, y, z, yaw, nested) of a fast travel station by its handle name, or None."""
    global _by_name
    if not _current:
        return None
    if _by_name is None:
        _by_name = {key.lower(): (v[0], v[1], v[2], v[3], bool(v[4])) for key, v in _load()["stations"].items()}
    return _by_name.get(name.lower())
