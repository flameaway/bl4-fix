"""
Our mods start switched on, however they were installed.

mods_base enables a mod at launch only when its settings file says `"enabled": true`
(`mods_base/settings.py`: `if self.auto_enable and settings.get("enabled", False)`), so a mod with
no settings file loads switched off. The setup app and the Mod Menu's updater write that file for
the mods they add, and the Mod Menu's `firstrun.py` for every new mod of ours it finds, but a mod
installed by hand without the Mod Menu (a Nexus zip, a .sdkmod dropped into sdk_mods), or with a
Mod Menu that didn't load (issue #5), had nothing to turn it on: no UI, no hotkeys, nothing.

Each mod calls `ensure_on(__name__)` in its `__init__.py` right before `build_mod()`: with no
settings file yet, it writes `{"enabled": true}` (temp file + rename), and `build_mod` then loads
it and enables the mod like any saved setting. A mod the player switched off has a file
(`"enabled": false`, mods_base writes it on every disable) and is left alone; so is any other
existing file, readable or not. One `exists()` at import, nothing after. Never raises.

Byte-identical copies in each mod (`tools/sync_shared.py`); no shared state, no owner.
"""

from __future__ import annotations

import json
from pathlib import Path

VERSION = 1


def ensure_on(module: str, settings_dir: Path | str | None = None) -> bool:
    """Writes `{"enabled": true}` for `module` if it has no settings file. True if it wrote one."""
    try:
        if settings_dir is None:
            from mods_base import SETTINGS_DIR

            settings_dir = SETTINGS_DIR
        path = Path(settings_dir) / f"{module}.json"
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.firston.tmp")
        tmp.write_text(json.dumps({"enabled": True}, indent=4), encoding="utf-8")
        tmp.replace(path)
    except Exception as ex:  # noqa: BLE001 - never stop the mod loading; it just starts off
        _log(f"[{module}] couldn't switch the mod on for its first launch: {ex}")
        return False
    _log(f"[{module}] first launch: switched on (no settings file yet)")
    return True


def _log(text: str) -> None:
    try:
        from unrealsdk import logging

        logging.info(text)
    except Exception:  # noqa: BLE001 - outside the game (tests)
        print(text)
