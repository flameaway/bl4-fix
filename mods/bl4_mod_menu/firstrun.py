"""
New BL4 Fix mods start switched on, without the setup app too.

mods_base enables a mod at launch only when its settings file says `"enabled": true`; a mod with no
settings file loads switched off. The setup app writes that file for every mod it newly installs
(installer/core.py `_install_mod`), the in-game updater for every new mod it adds
(updates._enable_setting). A manual install (the release's zip, Nexus, a .sdkmod dropped into
sdk_mods) has none, so every mod would sit there off until the player found the Mod Menu.

This runs once at the Mod Menu's import (it's a Library: always on, whatever its settings say).
For every `bl4_*.sdkmod` of ours (pyproject author "flame", like health's check) with no settings
file: a mod already imported is enabled now (mods_base saves its file); the rest get the file
written, so the SDK enables them when it imports them. A mod the player switched off has a file
(`"enabled": false`, mods_base writes it on every disable) and is left alone. Other modders'
mods are never touched. Plain file I/O plus mod.enable(), on the SDK's mod-loading thread like
its own enables.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path

OURS = re.compile(r'authors\s*=\s*\[[^\]]*"flame"')


def _ours(sdkmod: Path) -> bool:
    try:
        with zipfile.ZipFile(sdkmod) as zf:
            text = zf.read(f"{sdkmod.stem}/pyproject.toml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return bool(OURS.search(text))


def new_mods(sdk_mods: Path, settings_dir: Path) -> list[str]:
    """Our .sdkmods in sdk_mods that have never had a settings file."""
    return [
        path.stem for path in sorted(sdk_mods.glob("bl4_*.sdkmod"))
        if not (settings_dir / f"{path.stem}.json").exists() and _ours(path)
    ]  # fmt: skip


def enable_new(
    sdk_mods: Path,
    settings_dir: Path,
    registered: Iterable[object],
    log: Callable[[str], None] = print,
) -> list[str]:
    """Switches on every new mod of ours (see the module doc). Returns their ids."""
    found = new_mods(sdk_mods, settings_dir)
    if not found:
        return []
    by_file = {}
    for mod in registered:
        if (path := getattr(mod, "settings_file", None)) is not None:
            by_file[Path(path).name.lower()] = mod
    done = []
    for mod_id in found:
        try:
            if (mod := by_file.get(f"{mod_id}.json".lower())) is not None:
                if not getattr(mod, "is_enabled", False):
                    mod.enable()  # imported before us: mods_base saves "enabled": true
                if not (settings_dir / f"{mod_id}.json").exists():
                    _write(settings_dir / f"{mod_id}.json", {"enabled": True})
            else:
                _write(settings_dir / f"{mod_id}.json", {"enabled": True})
            done.append(mod_id)
        except Exception as ex:  # noqa: BLE001 - one mod's problem never stops the others
            log(f"[Mod Menu] couldn't switch on the new mod {mod_id}: {ex}")
    if done:
        log(f"[Mod Menu] switched on {len(done)} newly installed mod(s): {', '.join(done)}")
    return done


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(path)
