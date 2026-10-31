"""
Undoing installs and updates: roll a mod back to the copy it had before, and bring back the mods
the setup app's safe mode set aside.

Roll back: whenever the update helper (updates.helper_script) moves a staged <id>.sdkmod over one in
sdk_mods (BL4 Fix's updates and other repositories' alike), it first copies the one being replaced
to sdk_mods/.bl4_rollback/<id>.sdkmod: one previous copy per mod, a newer one replaces it (not
when the staged file is the same bytes). The Updates page lists each copy whose version differs from
the installed one; Roll back stages that copy as an update (.bl4_updates/<id>.sdkmod: the same
helper moves it in when the game closes, keeping the newer one as the copy to go back to), reloads
the mod from it when it can (like repos.update_async), and tells updates.py / repos.py not to offer
the version it rolled back from again this session (updates.hold).

Safe mode: the setup app moves every mod that isn't BL4 Fix's into sdk_mods/.bl4_safe_mode/ with a
record safe_mode.json = {"format": 1, "moved": ["foo.sdkmod", "a_folder", ...], "time": unix}.
Discover and Repositories say so and offer Restore: each entry goes back into sdk_mods (a mod that
isn't loaded can move while the game runs; a name sdk_mods already has stays put and is named),
then hotload.scan() lists them under "In your mods folder", where Load starts one now. They aren't
loaded by Restore itself on purpose: one of them may be why the player turned safe mode on. The
record is read defensively: the folder's own entries are what gets restored.

Plain Python: file work on repos.py's worker, reloads through repos._queue (the game thread).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import hotload, updates
except ImportError:  # offline tests import the folder's modules directly
    import hotload  # type: ignore[no-redef]
    import updates  # type: ignore[no-redef]

ROLLBACK = ".bl4_rollback"  # in sdk_mods (dot folders aren't loaded as mods)
SAFE_DIR = ".bl4_safe_mode"  # in sdk_mods, made by the setup app (installer/core.py)
SAFE_RECORD = "safe_mode.json"
MAX_SAFE = 200  # entries listed / restored at once

_lock = threading.Lock()


class B:
    """Undo state. Workers write it under _lock; snapshot() reads it."""

    revision = 0
    game_dir: Path | None = None
    # {"id", "name", "installed", "previous"}: rollback copies whose version differs from the installed one
    rollbacks: list[dict[str, Any]] = []
    # mod id -> {"state": "working"/"staged"/"reloaded"/"failed", "message"}
    jobs: dict[str, dict[str, Any]] = {}
    # {"count", "names": [{"file", "name"}], "time"}, or None when safe mode set nothing aside
    safe: dict[str, Any] | None = None
    restoring = False


def _set(**changes: Any) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(B, key, value)
        B.revision += 1


def _job(mod_id: str, state: str, message: str) -> None:
    with _lock:
        B.jobs = {**B.jobs, mod_id: {"state": state, "message": message}}
        B.revision += 1


def _repos() -> Any:
    try:
        from . import repos  # noqa: PLC0415 - repos imports this module
    except ImportError:
        import repos  # type: ignore[no-redef]  # noqa: PLC0415
    return repos


def rollback_dir(game_dir: Path) -> Path:
    return updates.sdk_mods(game_dir) / ROLLBACK


def safe_dir(game_dir: Path) -> Path:
    return updates.sdk_mods(game_dir) / SAFE_DIR


def snapshot() -> dict[str, Any]:
    """The part of /repos the Updates, Discover and Repositories pages show."""
    with _lock:
        return {"rollbacks": B.rollbacks, "jobs": B.jobs, "safe": B.safe, "restoring": B.restoring}


def scan(game_dir: Path) -> None:
    """Lists rollback copies and what safe mode set aside. File reads: a worker (at start, after an action)."""
    B.game_dir = game_dir
    rollbacks = _scan_rollbacks(game_dir)
    safe = _scan_safe(game_dir)
    with _lock:
        if rollbacks != B.rollbacks or safe != B.safe:
            B.rollbacks, B.safe = rollbacks, safe
            B.revision += 1


# region Roll back


def _scan_rollbacks(game_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        copies = sorted(p for p in rollback_dir(game_dir).glob("*.sdkmod") if p.is_file())
    except OSError:
        return out
    mods_dir = updates.sdk_mods(game_dir)
    for copy in copies:
        mod_id = copy.stem
        if not hotload.MOD_ID.match(mod_id) or (mods_dir / mod_id).is_dir():
            continue  # a folder copy wins over any .sdkmod: nothing to roll back
        installed = updates._zip_version(mods_dir / f"{mod_id}.sdkmod", mod_id)
        previous = updates._zip_version(copy, mod_id)
        if not installed or not previous or installed == previous:
            continue
        name = hotload.read_info(copy).get("name") or mod_id
        out.append({"id": mod_id, "name": name, "installed": installed, "previous": previous})
    out.sort(key=lambda r: str(r["name"]).lower())
    return out


def roll_back(game_dir: Path, mod_id: str) -> None:
    """Stages the rollback copy as an update and queues its reload. Blocking file work: a worker."""
    entry = next((r for r in B.rollbacks if r["id"] == mod_id), None)
    if entry is None:
        raise RuntimeError("there's no earlier copy of that mod any more")
    copy = rollback_dir(game_dir) / f"{mod_id}.sdkmod"
    if problem := hotload.zip_root_ok(copy):
        raise RuntimeError(f"the earlier copy can't be used: {problem}")
    staged = updates.staging(game_dir) / f"{mod_id}.sdkmod"
    if hotload.H.loaded_from.get(mod_id) == str(staged):
        # Running from the staged file (updated this session): it can't be written over
        raise RuntimeError("it was updated in this session already: restart the game first, then roll back")
    data = copy.read_bytes()
    updates._write(staged, data)
    (updates.staging(game_dir) / f"{mod_id}.remove").unlink(missing_ok=True)  # removed earlier: rolling back keeps it
    if not updates.start_helper(game_dir):
        staged.unlink(missing_ok=True)
        raise RuntimeError("couldn't start the helper that installs it when the game closes")
    # Updates mustn't offer the version just rolled back from (again) this session
    updates.hold(mod_id, entry["installed"])
    repos = _repos()
    with repos._lock:
        record = repos.R.installed.get(mod_id)
        if record:
            repos.R.installed[mod_id] = {**record, "version": entry["previous"], "sha256": hashlib.sha256(data).hexdigest()}
            repos.R.revision += 1
            repos.R.events += 1
    if record:
        repos._save()
    repos._net.put(repos._recompute)
    name, back = entry["name"], entry["previous"]
    if mod_id in hotload.FRAMEWORK:
        text = f"{name} goes back to {back} when the game closes (it can't reload while the game runs)."
        _job(mod_id, "staged", text)
        repos._say(text)
        return
    _job(mod_id, "working", "Reloading...")

    def done(text: str, error: bool) -> None:
        if error:
            msg = f"{name} goes back to {back} when the game closes: {text}"
            _job(mod_id, "staged", msg)
        else:
            msg = f"Rolled {name} back to {back}: {text}"
            _job(mod_id, "reloaded", msg)
        repos._say(msg)

    repos._queue("reload", mod_id, staged, done)


def roll_back_async(mod_id: str) -> bool:
    game_dir = B.game_dir
    if game_dir is None or B.jobs.get(mod_id, {}).get("state") == "working":
        return False
    _job(mod_id, "working", "Staging the earlier copy...")
    repos = _repos()

    def run() -> None:
        try:
            roll_back(game_dir, mod_id)
        except Exception as ex:  # noqa: BLE001
            _job(mod_id, "failed", f"Couldn't roll back: {ex}")
            repos._say(f"Couldn't roll back: {ex}.", error=True)

    repos._net.put(run)
    return True


# endregion
# region Safe mode


def _safe_entries(folder: Path) -> list[Path]:
    """What safe mode set aside: the record's order first, then anything else in the folder."""
    try:
        present = {p.name: p for p in folder.iterdir() if p.name != SAFE_RECORD and not p.name.endswith((".tmp", ".part"))}
    except OSError:
        return []
    order: list[str] = []
    try:
        record = json.loads((folder / SAFE_RECORD).read_text(encoding="utf-8"))
        moved = record.get("moved") if isinstance(record, dict) else None
        if isinstance(moved, list):
            order = [m for m in moved if isinstance(m, str) and m in present]
    except (OSError, ValueError, RecursionError):
        pass  # missing or corrupt: the folder's own entries
    names = list(dict.fromkeys(order + sorted(present)))
    return [present[n] for n in names[:MAX_SAFE]]


def _record_time(folder: Path) -> float:
    try:
        record = json.loads((folder / SAFE_RECORD).read_text(encoding="utf-8"))
        value = record.get("time") if isinstance(record, dict) else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    except (OSError, ValueError, RecursionError):
        return 0.0


def _display_name(path: Path) -> str:
    if (path.is_dir() and (path / "pyproject.toml").is_file()) or path.suffix.lower() == ".sdkmod":
        return hotload.read_info(path).get("name") or path.stem
    return path.name


def _scan_safe(game_dir: Path) -> dict[str, Any] | None:
    folder = safe_dir(game_dir)
    if not folder.is_dir():
        return None
    entries = _safe_entries(folder)
    if not entries:
        return None
    names = [{"file": p.name, "name": _display_name(p)} for p in entries]
    return {"count": len(names), "names": names, "time": _record_time(folder)}


def restore_safe(game_dir: Path) -> tuple[list[str], list[str]]:
    """Moves everything safe mode set aside back into sdk_mods. Returns (moved, skipped). A worker."""
    folder = safe_dir(game_dir)
    mods_dir = updates.sdk_mods(game_dir)
    moved: list[str] = []
    skipped: list[str] = []
    for path in _safe_entries(folder):
        target = mods_dir / path.name
        if target.exists():
            skipped.append(path.name)  # sdk_mods has one by that name (installed again since)
            continue
        try:
            os.rename(path, target)  # same folder tree: a rename; fails rather than overwrite
            moved.append(path.name)
        except OSError:
            skipped.append(path.name)
    left = [p.name for p in _safe_entries(folder)]
    try:
        if not left:
            (folder / SAFE_RECORD).unlink(missing_ok=True)
            folder.rmdir()  # only when empty (else OSError: something else is in it, left alone)
        elif moved:
            # What's still there (temp + rename, like every JSON we write)
            data = {"format": 1, "moved": left, "time": _record_time(folder) or time.time()}
            updates._write(folder / SAFE_RECORD, json.dumps(data, indent=4).encode("utf-8"))
    except OSError:
        pass
    return moved, skipped


def restore_safe_async() -> bool:
    game_dir = B.game_dir
    if game_dir is None or B.restoring:
        return False
    _set(restoring=True)
    repos = _repos()

    def run() -> None:
        try:
            moved, skipped = restore_safe(game_dir)
            hotload.scan(game_dir)  # "In your mods folder" lists them, with Load
            scan(game_dir)
            text = (
                f"Put back {len(moved)} mod{'s' if len(moved) != 1 else ''} from safe mode: they're under"
                " \"In your mods folder\" on Discover (Load starts one now), and load at the next start."
                if moved else "Nothing was put back from safe mode."
            )  # fmt: skip
            if skipped:
                text += f" Left in {SAFE_DIR} (your mods folder already has one by that name): {', '.join(skipped)}."
            repos._say(text, error=not moved)
        except Exception as ex:  # noqa: BLE001
            repos._say(f"Couldn't restore the mods safe mode set aside: {ex}", error=True)
        finally:
            _set(restoring=False)

    repos._net.put(run)
    return True


# endregion
