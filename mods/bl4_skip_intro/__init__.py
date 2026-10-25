"""
Skip Intro for Borderlands 4: no 2K / Gearbox logo movies at launch.

Enabling moves the two logo movies aside (see movies.py); disabling puts them back. The movies play
before mods load, so either takes effect on the next launch. While enabled, each launch moves aside
any copies a Steam file check or game update brought back. The BL4 Direct installer has the same
switch (it sets this mod's enabled flag, so the two agree).
"""

from __future__ import annotations

from mods_base import SETTINGS_DIR, build_mod
from unrealsdk import logging

from . import movies

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

GAME_DIR = SETTINGS_DIR.parent.parent


def _enable() -> None:
    try:
        if moved := movies.skip(GAME_DIR):
            logging.info(f"[Skip Intro] Moved aside {', '.join(moved)}: no logos from the next launch")
    except OSError as ex:
        logging.error(f"[Skip Intro] Couldn't move the logo movies: {ex!r}")


def _disable() -> None:
    try:
        if restored := movies.restore(GAME_DIR):
            logging.info(f"[Skip Intro] Put back {', '.join(restored)}: logos play from the next launch")
    except OSError as ex:
        logging.error(f"[Skip Intro] Couldn't put the logo movies back: {ex!r}")


mod = build_mod(on_enable=_enable, on_disable=_disable)
