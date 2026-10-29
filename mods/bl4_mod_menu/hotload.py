"""
Loading SDK mods while the game runs: a new .sdkmod dropped into sdk_mods (or installed from a
repository) loads at once, and an updated one reloads, with no restart.

Loading a new mod does what the SDK's sdk_mods/__main__.py does at game start: check the file
(one root folder named like the zip), put the .sdkmod on sys.path and import the module. Its
build_mod() registers it, and mods_base turns it on if its settings say so.

Reloading replaces a loaded mod: deregister its mods (disables them without changing their saved
"enabled"), drop its modules, import it again from the new file. The new file is never written over
the loaded one: a running game keeps reading a loaded .sdkmod (zipimport keeps its directory, mods
read their assets from it), so the update stays in sdk_mods/.bl4_updates/ and is imported from
there; the update helper moves it into place after the game closes (updates.py). If the new copy
fails to import, the old modules and mods are put back.

What can't reload: the mod frameworks and this menu, libraries (they can't be disabled), and mods
other mods imported from (they'd keep the old copy). Those update at the next start.

scan() is plain file I/O for any thread; load(), reload() and unload() touch mods: game thread only.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import sys
import threading
import time
import tomllib
import zipfile
import zipimport
from pathlib import Path
from types import ModuleType
from typing import Any

# Never loaded, reloaded or removed from here: the SDK's own modules and this menu
FRAMEWORK = frozenset({
    "mods_base", "unrealsdk", "pyunrealsdk", "keybinds", "console_mod_menu", "bl4_mod_menu",
    "__main__", "builtins", "debugpy", "ipykernel",
})  # fmt: skip
# Mods the BL4 Fix install needs (the setup app keeps them installed)
REQUIRED = frozenset({"bl4_mod_menu", "bl4_direct"})
MOD_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# The SDK's "MyMod (3).sdkmod" check: a browser renamed a second download
NUMBERED = re.compile(r"^(.+?) ?\(\d+\)\.sdkmod$", re.I)
ICON_NAME = "icon.png"
ICON_DIR = "OakGame/Content/uiresources/bl4modmenu/img/mods"
ICON_URL = "coui://uiresources/bl4modmenu/img/mods/"
MAX_ICON = 1 << 20

_lock = threading.Lock()


class H:
    """What scan() found. Written by the scanning thread under _lock."""

    revision = 0
    # (size, mtime_ns) of each loaded mod's .sdkmod when we first looked: a change = replaced on disk
    baseline: dict[str, tuple[int, int]] = {}
    # Entries in sdk_mods the game hasn't loaded, or changed since it did
    found: list[dict[str, Any]] = []
    # Last outcome per mod id: {"state": "loaded"/"reloaded"/"failed"/"removed", "message"}
    results: dict[str, dict[str, str]] = {}
    # Mod id -> coui:// url of the icon the mod carries (its icon.png)
    icons: dict[str, str] = {}
    scanned_at = 0.0
    # Mod ids that sit on sys.path from somewhere other than sdk_mods (a staged update)
    loaded_from: dict[str, str] = {}
    # Last pyproject read per file: path -> ((size, mtime), info)
    info_cache: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def _bump() -> None:
    H.revision += 1


# region Reading sdk_mods (any thread)


def sdk_mods(game_dir: Path) -> Path:
    return game_dir / "sdk_mods"


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def zip_root_ok(path: Path) -> str:
    """'' if the .sdkmod is laid out like the SDK wants (one root folder named like the file)."""
    try:
        with zipfile.ZipFile(path) as zf:
            roots = {n.split("/", 1)[0] for n in zf.namelist() if n}
            if roots != {path.stem}:
                hint = ""
                if (m := NUMBERED.match(path.name)) and roots == {m.group(1)}:
                    hint = f" Rename it to {m.group(1)}.sdkmod."
                return f"{path.name} must hold one folder named {path.stem}.{hint}"
            if f"{path.stem}/__init__.py" not in zf.namelist():
                return f"{path.name} has no {path.stem}/__init__.py."
    except (OSError, zipfile.BadZipFile) as ex:
        return f"{path.name} isn't a valid .sdkmod ({ex})."
    return ""


def read_info(path: Path) -> dict[str, str]:
    """{name, version, description, author} from the mod's pyproject.toml, as far as present."""
    stamp = _stamp(path) or (0, 0)
    cached = H.info_cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    data: dict[str, Any] = {}
    try:
        if path.is_dir():
            raw = (path / "pyproject.toml").read_bytes()
        else:
            with zipfile.ZipFile(path) as zf:
                raw = zf.read(f"{path.stem}/pyproject.toml")
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        pass
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    sdkmod = data.get("tool", {}).get("sdkmod", {}) if isinstance(data.get("tool"), dict) else {}
    authors = project.get("authors") or []
    author = ", ".join(str(a.get("name", "")) for a in authors if isinstance(a, dict) and a.get("name"))
    info = {
        "name": str(sdkmod.get("name") or project.get("name") or path.stem)[:80],
        "version": str(sdkmod.get("version") or project.get("version") or "")[:32],
        "description": str(project.get("description") or "")[:600],
        "author": author[:80],
    }
    H.info_cache[str(path)] = (stamp, info)
    return info


def module_file(mod_id: str) -> Path | None:
    """The .sdkmod (or folder) a loaded mod came from."""
    module = sys.modules.get(mod_id)
    path = getattr(module, "__file__", None) or ""
    if not path:
        return None
    low = path.lower()
    at = low.find(".sdkmod")
    if at >= 0:
        return Path(path[: at + len(".sdkmod")])
    return Path(path).parent


def _candidates(game_dir: Path) -> dict[str, Path]:
    """Mod id -> the entry in sdk_mods the SDK would load it from (folders beat .sdkmod files)."""
    out: dict[str, Path] = {}
    try:
        entries = list(os.scandir(sdk_mods(game_dir)))
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name == "__pycache__" or name == "settings":
            continue
        try:
            if entry.is_dir():
                if "." in name or not os.path.exists(os.path.join(entry.path, "__init__.py")):
                    continue
                out[name] = Path(entry.path)
            elif name.lower().endswith(".sdkmod"):
                out.setdefault(Path(name).stem, Path(entry.path))
        except OSError:
            continue
    return out


def scan(game_dir: Path) -> None:
    """Looks for mods in sdk_mods the game hasn't loaded, and loaded .sdkmod files replaced since
    the first look. File stats plus one pyproject read per new file: off the game thread."""
    found: list[dict[str, Any]] = []
    for mod_id, path in sorted(_candidates(game_dir).items()):
        if mod_id in FRAMEWORK or not MOD_ID.match(mod_id):
            continue
        stamp = _stamp(path)
        if mod_id in sys.modules:
            if path.is_dir() or stamp is None:
                continue
            base = H.baseline.setdefault(mod_id, stamp)
            # Replaced on disk while loaded (dropped over the old copy): offer a reload
            if base != stamp and H.loaded_from.get(mod_id) is None:
                found.append({"id": mod_id, "kind": "changed", "file": path.name, **read_info(path)})
            continue
        problem = "" if path.is_dir() else zip_root_ok(path)
        found.append({"id": mod_id, "kind": "new", "file": path.name, "problem": problem, **read_info(path)})
    with _lock:
        if found != H.found:
            H.found = found
            _bump()
        H.scanned_at = time.monotonic()


def take_baseline(game_dir: Path) -> None:
    """At start: remember each loaded .sdkmod as it is, so a replacement shows up as changed."""
    for mod_id, path in _candidates(game_dir).items():
        if mod_id in sys.modules and not path.is_dir() and (stamp := _stamp(path)) is not None:
            H.baseline.setdefault(mod_id, stamp)


def publish_icons(game_dir: Path) -> None:
    """Copies each loaded mod's icon.png (if it has one) next to the page, as img/mods/<id>.png.
    Worker thread: one small file read per mod with an icon, skipped when it's already there."""
    icons: dict[str, str] = {}
    target_dir = game_dir / ICON_DIR
    skip = FRAMEWORK | set(sys.stdlib_module_names)
    for mod_id in sorted(n for n in list(sys.modules) if "." not in n and n not in skip and MOD_ID.match(n)):
        source = module_file(mod_id)
        if source is None:
            continue
        try:
            if source.is_dir():
                if not (source / "pyproject.toml").is_file():
                    continue  # a package, not a mod
                path = source / ICON_NAME
                data = path.read_bytes() if path.is_file() and path.stat().st_size <= MAX_ICON else b""
            else:
                with zipfile.ZipFile(source) as zf:
                    name = f"{mod_id}/{ICON_NAME}"
                    data = zf.read(name) if name in zf.namelist() and zf.getinfo(name).file_size <= MAX_ICON else b""
        except (OSError, KeyError, zipfile.BadZipFile):
            continue
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        target = target_dir / f"{mod_id}.png"
        try:
            if not target.is_file() or target.read_bytes() != data:
                target_dir.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(target)
            icons[mod_id] = f"{ICON_URL}{mod_id}.png?v={len(data)}"
        except OSError:
            continue
    with _lock:
        if icons != H.icons:
            H.icons = icons
            _bump()


def snapshot() -> dict[str, Any]:
    with _lock:
        return {"found": list(H.found), "results": dict(H.results), "icons": dict(H.icons)}


# endregion
# region Mods of a module (game thread)


def _registry() -> list[Any]:
    return importlib.import_module("mods_base.mod_list").mod_list


def _belongs(name: str, mod_id: str) -> bool:
    return name == mod_id or name.startswith(mod_id + ".")


def mods_of(mod_id: str) -> list[Any]:
    """The registered mods a module made (build_mod names their settings file after the module)."""
    module = sys.modules.get(mod_id)
    own = {id(v) for v in vars(module).values()} if module is not None else set()
    out = []
    for mod in _registry():
        settings = getattr(mod, "settings_file", None)
        if (
            id(mod) in own
            or _belongs(type(mod).__module__, mod_id)
            or (settings is not None and Path(settings).stem == mod_id)
        ):
            out.append(mod)
    return out


def dependents(mod_id: str) -> list[str]:
    """Other loaded mods holding something of this one (from x import y): they'd keep the old copy."""
    users: set[str] = set()
    for name, module in list(sys.modules.items()):
        top = name.split(".", 1)[0]
        if module is None or top == mod_id or top in users or top in FRAMEWORK:
            continue
        try:
            values = list(vars(module).values())
        except TypeError:
            continue
        for value in values:
            if isinstance(value, ModuleType):
                owner = value.__name__
            else:
                owner = getattr(value, "__module__", None)
            if isinstance(owner, str) and _belongs(owner, mod_id):
                users.add(top)
                break
    return sorted(users)


def can_reload(mod_id: str) -> str:
    """'' if the mod can reload now, else why not."""
    if mod_id in FRAMEWORK:
        return "It's part of the mod menu or the SDK: it updates when the game starts again."
    mods = mods_of(mod_id)
    if any(getattr(m, "enabling_locked", False) or type(m).__name__ == "Library" for m in mods):
        return "It's a library (always on): it updates when the game starts again."
    if users := dependents(mod_id):
        return f"{', '.join(users)} use{'s' if len(users) == 1 else ''} it: it updates when the game starts again."
    return ""


# endregion
# region Loading (game thread)


def _forget_zip(path: Path | str) -> None:
    """Drops the import system's cached view of a zip (its directory), so a new file is read fresh."""
    key = str(path)
    sys.path_importer_cache.pop(key, None)
    cache = getattr(zipimport, "_zip_directory_cache", None)
    if isinstance(cache, dict):
        cache.pop(key, None)


def _purge(mod_id: str) -> dict[str, ModuleType]:
    removed = {n: m for n, m in list(sys.modules.items()) if _belongs(n, mod_id)}
    for name in removed:
        sys.modules.pop(name, None)
    return removed


def _result(mod_id: str, state: str, message: str) -> str:
    with _lock:
        H.results[mod_id] = {"state": state, "message": message}
        H.found = [f for f in H.found if f["id"] != mod_id]
        _bump()
    return message


def _drop_new(before: list[Any]) -> None:
    """After a failed import: take out the mods it registered before it failed."""
    from mods_base.mod_list import deregister_mod  # noqa: PLC0415

    for mod in [m for m in _registry() if all(m is not b for b in before)]:
        try:
            deregister_mod(mod)
        except Exception:  # noqa: BLE001, S110 - half-built: best effort
            pass


def _timed_import(game_dir: Path, mod_id: str) -> tuple[ModuleType, float]:
    started = time.perf_counter()
    # Only sdk_mods' own finder (a folder dropped in since it listed the directory).
    # importlib.invalidate_caches() would make every loaded .sdkmod re-read its zip directory:
    # 15-200 ms measured in game with 27 of them
    finder = sys.path_importer_cache.get(str(sdk_mods(game_dir)))
    if finder is not None and hasattr(finder, "invalidate_caches"):
        finder.invalidate_caches()
    module = importlib.import_module(mod_id)
    return module, (time.perf_counter() - started) * 1000


def load(game_dir: Path, mod_id: str, *, enable: bool = True, path: Path | None = None) -> str:
    """Loads a mod the game hasn't loaded yet, like the SDK does at start. Returns what happened."""
    if not MOD_ID.match(mod_id) or mod_id in FRAMEWORK:
        raise RuntimeError(f"{mod_id} can't be loaded from here.")
    if mod_id in sys.modules:
        raise RuntimeError(f"{mod_id} is already loaded.")
    path = path or _candidates(game_dir).get(mod_id)
    if path is None:
        raise RuntimeError(f"{mod_id} isn't in the mods folder.")
    if not path.is_dir():
        if problem := zip_root_ok(path):
            raise RuntimeError(problem)
        _forget_zip(path)
        if str(path) not in sys.path:
            sys.path.append(str(path))
        if path.parent != sdk_mods(game_dir):
            H.loaded_from[mod_id] = str(path)
    before = list(_registry())
    try:
        _, ms = _timed_import(game_dir, mod_id)
    except Exception as ex:
        _drop_new(before)
        _purge(mod_id)
        _result(mod_id, "failed", f"{mod_id} failed to load: {ex}")
        raise RuntimeError(f"{mod_id} failed to load: {ex}") from ex
    mods = mods_of(mod_id)
    if enable:
        for mod in mods:
            if not mod.is_enabled and not getattr(mod, "enabling_locked", False):
                mod.enable()
    if (stamp := _stamp(path)) is not None and not path.is_dir():
        H.baseline[mod_id] = stamp
    names = ", ".join(m.name for m in mods) or mod_id
    return _result(mod_id, "loaded", f"Loaded {names} ({ms:.0f} ms)." if mods else f"Loaded {mod_id}, but it added no mod.")


def reload(game_dir: Path, mod_id: str, path: Path | None = None) -> str:
    """
    Replaces a loaded mod with the copy at path (a staged update), or with its file in sdk_mods
    (replaced on disk). Raises with the reason if it can't; on a failed import the old copy is put
    back and keeps running.
    """
    if mod_id not in sys.modules:
        return load(game_dir, mod_id, path=path)
    if why := can_reload(mod_id):
        raise RuntimeError(why)
    old_file = module_file(mod_id)
    source = path or _candidates(game_dir).get(mod_id)
    if source is None:
        raise RuntimeError(f"{mod_id} isn't in the mods folder.")
    if not source.is_dir() and (problem := zip_root_ok(source)):
        raise RuntimeError(problem)

    from mods_base.mod_list import deregister_mod, register_mod  # noqa: PLC0415

    old_mods = mods_of(mod_id)
    was_on = {m.name for m in old_mods if m.is_enabled}
    for mod in old_mods:
        deregister_mod(mod)  # disables it, keeping "enabled" in its settings for the new copy
    old_modules = _purge(mod_id)
    old_path = list(sys.path)
    if not source.is_dir():
        _forget_zip(source)
        if old_file is not None:
            _forget_zip(old_file)
        entry = str(source)
        if entry in sys.path:
            sys.path.remove(entry)
        # Ahead of the old file, so the import finds the new copy first
        at = sys.path.index(str(old_file)) if old_file is not None and str(old_file) in sys.path else len(sys.path)
        sys.path.insert(at, entry)
    before = list(_registry())
    try:
        _, ms = _timed_import(game_dir, mod_id)
    except Exception as ex:
        _drop_new(before)
        _purge(mod_id)
        sys.path[:] = old_path
        if not source.is_dir():
            _forget_zip(source)
        sys.modules.update(old_modules)
        for mod in old_mods:
            if mod not in _registry():
                register_mod(mod)  # turns it back on if its settings say so
            if mod.name in was_on and not mod.is_enabled:
                mod.enable()
        _result(mod_id, "failed", f"{mod_id}: the new copy failed to load ({ex}). The old one is still running.")
        raise RuntimeError(f"the new copy of {mod_id} failed to load ({ex}); the old one is still running") from ex
    if source.parent != sdk_mods(game_dir) and not source.is_dir():
        H.loaded_from[mod_id] = str(source)
    elif (stamp := _stamp(source)) is not None:
        H.baseline[mod_id] = stamp
    new_mods = mods_of(mod_id)
    for mod in new_mods:
        # On as before, whatever the settings file says
        if mod.name in was_on and not mod.is_enabled and not getattr(mod, "enabling_locked", False):
            mod.enable()
    names = ", ".join(m.name for m in new_mods) or mod_id
    return _result(mod_id, "reloaded", f"Reloaded {names} ({ms:.0f} ms).")


def unload(mod_id: str) -> list[str]:
    """Turns a mod off and takes it out of the list (removing it). Its modules stay imported."""
    from mods_base.mod_list import deregister_mod  # noqa: PLC0415

    names = []
    for mod in mods_of(mod_id):
        if getattr(mod, "enabling_locked", False):
            raise RuntimeError(f"{mod.name} can't be turned off while the game runs.")
        if mod.is_enabled:
            mod.disable()
        deregister_mod(mod)
        names.append(mod.name)
    _result(mod_id, "removed", f"Removed {', '.join(names) or mod_id}.")
    return names


# endregion


def open_folder(game_dir: Path) -> None:
    """Opens sdk_mods in Explorer, from a worker (ShellExecute can take a moment)."""

    def run() -> None:
        try:
            os.startfile(str(sdk_mods(game_dir)))  # noqa: S606 - our own folder
        except OSError:
            # No file association for folders (rare): start Explorer itself
            explorer = shutil.which("explorer") or "explorer"
            os.spawnl(os.P_NOWAIT, explorer, "explorer", str(sdk_mods(game_dir)))  # noqa: S606

    threading.Thread(target=run, name="bl4_mod_menu.open_folder", daemon=True).start()
