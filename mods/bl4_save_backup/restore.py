"""
Staging a restore, which restore_helper.ps1 applies after the game exits.

The game keeps the profile and the loaded character in memory and saves them as it goes (and once
more when it quits), so a save written while it runs would be overwritten. Instead the restore is
a plan (`<account>/pending_restore.json`) and a hidden PowerShell helper (started once per game
session, outliving it like the Mod Menu's update helper) applies it once no Borderlands4 process
is left. The plan is read at that moment, so Cancel just deletes it.

Plain Python, no SDK imports. stage_*() run on the worker thread (they decrypt saves to check them).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import types
from importlib import resources
from pathlib import Path
from typing import Any

from . import savefile, store

PLAN = "pending_restore.json"
APPLYING = "pending_restore.applying"
RESULT = "last_restore.json"
SEEN = "last_restore.seen.json"
HELPER = "restore_helper.ps1"

# Survives hot reloads: one helper per game session
_state = sys.modules.get("_bl4_save_backup_restore")
if _state is None:
    _state = types.ModuleType("_bl4_save_backup_restore")
    _state.helper = None
    sys.modules["_bl4_save_backup_restore"] = _state


class RestoreError(Exception):
    pass


# region Plans


def pending(acc: store.Account) -> dict[str, Any] | None:
    for name in (PLAN, APPLYING):
        try:
            return json.loads((acc.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _save_plan(acc: store.Account, plan: dict[str, Any]) -> None:
    savefile.write_json(acc.dir / PLAN, plan)


def _add(acc: store.Account, actions: list[dict[str, Any]], key: bytes | None) -> dict[str, Any]:
    plan = pending(acc) or {"id": f"{time.time():.0f}", "created": time.time(), "actions": []}
    plan["save_dir"] = str(acc.save_dir)
    targets = {a["target"] for a in actions}
    plan["actions"] = [a for a in plan["actions"] if a["target"] not in targets] + actions
    if key is not None:
        plan["key"] = key.hex()
    _save_plan(acc, plan)
    return plan


def cancel(acc: store.Account, target: str | None = None) -> bool:
    """Drops the whole plan, or one file's action. False if there was nothing (or it's being applied)."""
    plan_path = acc.dir / PLAN
    plan = pending(acc)
    if plan is None or not plan_path.exists():
        return False
    if target is not None:
        plan["actions"] = [a for a in plan["actions"] if a["target"] != target]
    if target is None or not plan["actions"]:
        plan_path.unlink(missing_ok=True)
    else:
        _save_plan(acc, plan)
    return True


def last_result(acc: store.Account) -> dict[str, Any] | None:
    """What the helper did after the last exit, until the player has seen it."""
    try:
        result = json.loads((acc.dir / RESULT).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    rows = result.get("results")
    # PowerShell's ConvertTo-Json may write a one-item array as the item itself
    result["results"] = [rows] if isinstance(rows, dict) else [r for r in rows or [] if isinstance(r, dict)]
    return result


def result_seen(acc: store.Account) -> None:
    try:
        (acc.dir / RESULT).replace(acc.dir / SEEN)
    except OSError:
        pass


# endregion
# region Building actions (worker thread)


def _who(info: dict[str, Any], fallback: str) -> str:
    return info.get("name") or fallback


def _target_for(acc: store.Account, guid: str, fname: str) -> str:
    """The live file this character is in now, else its old slot if that's free."""
    for name, live in acc.live.items():
        if live.get("guid") == guid:
            return name
    if fname in acc.live or (acc.save_dir / fname).exists():
        raise RestoreError("another character is in its slot now")
    return fname


def _file_action(st: store.Store, acc: store.Account, backup: str, fname: str, target: str, what: str) -> dict[str, Any]:
    info = acc.sessions.get(backup, {}).get("files", {}).get(fname)
    source = acc.dir / backup / fname
    if info is None or not source.is_file():
        raise RestoreError("that backup is gone")
    data = source.read_bytes()
    if info.get("ok") is False:
        raise RestoreError(f"that backup didn't read whole ({info.get('error', 'damaged')})")
    key = st.key(acc)
    if key is not None:
        try:
            savefile.decrypt(key, data)
        except savefile.SaveError as ex:
            raise RestoreError(f"that backup doesn't open: {ex}") from None
    return {"kind": "file", "target": target, "source": str(source), "sha1": hashlib.sha1(data).hexdigest(), "what": what}


def stage_character(st: store.Store, acc: store.Account, backup: str, fname: str) -> str:
    info = acc.sessions.get(backup, {}).get("files", {}).get(fname) or {}
    guid = info.get("guid")
    if not guid:
        raise RestoreError("that backup has no character in it")
    what = f"{_who(info, fname)} (saved {store.when(info.get('mtime', 0))})"
    action = _file_action(st, acc, backup, fname, _target_for(acc, guid, fname), what)
    _add(acc, [action], None)
    return what


def _bank_block(st: store.Store, acc: store.Account, backup: str) -> tuple[str, bytes]:
    key = st.key(acc)
    if key is None:
        raise RestoreError("couldn't find the save key, so the bank can't be read")
    try:
        old = savefile.decrypt(key, (acc.dir / backup / "profile.sav").read_bytes())
        now = savefile.decrypt(key, (acc.save_dir / "profile.sav").read_bytes())
    except OSError:
        raise RestoreError("that backup's profile is gone") from None
    except savefile.SaveError as ex:
        raise RestoreError(f"a profile doesn't open: {ex}") from None
    block = savefile.bank_block(old)
    if block is None:
        raise RestoreError("that backup has no bank")
    try:
        savefile.with_bank(now, block)  # the same splice the helper does: it must fit the profile now
    except savefile.SaveError as ex:
        raise RestoreError(str(ex)) from None
    return block, key


def stage_bank(st: store.Store, acc: store.Account, backup: str) -> str:
    info = acc.sessions.get(backup, {}).get("files", {}).get("profile.sav") or {}
    block, key = _bank_block(st, acc, backup)
    what = f"Bank: {len(info.get('bank', ()))} items (saved {store.when(info.get('mtime', 0))})"
    _add(acc, [{"kind": "bank", "target": "profile.sav", "block": block, "what": what}], key)
    return what


def stage_profile(st: store.Store, acc: store.Account, backup: str) -> str:
    info = acc.sessions.get(backup, {}).get("files", {}).get("profile.sav") or {}
    what = f"Whole profile and bank (saved {store.when(info.get('mtime', 0))})"
    _add(acc, [_file_action(st, acc, backup, "profile.sav", "profile.sav", what)], None)
    return what


def stage_all(st: store.Store, acc: store.Account, backup: str) -> tuple[str, list[str]]:
    """Every save in a backup. Characters go back into the file they're in now. Returns (what, skipped)."""
    files = acc.sessions.get(backup, {}).get("files", {})
    actions, skipped = [], []
    for fname, info in sorted(files.items()):
        try:
            if fname == "profile.sav":
                actions.append(_file_action(st, acc, backup, fname, fname, "Profile and bank"))
            elif info.get("guid"):
                target = _target_for(acc, info["guid"], fname)
                actions.append(_file_action(st, acc, backup, fname, target, _who(info, fname)))
            else:
                skipped.append(f"{fname}: unreadable")
        except RestoreError as ex:
            skipped.append(f"{_who(info, fname)}: {ex}")
    if not actions:
        raise RestoreError("nothing in that backup can be restored" + (f" ({'; '.join(skipped)})" if skipped else ""))
    _add(acc, actions, None)
    created = acc.sessions.get(backup, {}).get("created", 0)
    return f"Everything from the backup of {store.when(created)}", skipped


# endregion
# region The helper


def start_helper(base: Path) -> bool:
    """Starts the helper once per game session (again if it ended). Returns whether one runs."""
    helper = _state.helper
    if helper is not None and helper.poll() is None:
        return True
    try:
        base.mkdir(parents=True, exist_ok=True)
        script = base / HELPER
        content = (resources.files(__package__) / HELPER).read_bytes()
        try:
            same = script.read_bytes() == content
        except OSError:
            same = False
        if not same:
            tmp = script.with_name(script.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(script)
        # The game runs in a job that doesn't allow breaking away, but the helper still outlives a
        # normal game exit (the Mod Menu's update helper and Item Cards' pak helper do the same)
        _state.helper = subprocess.Popen(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",  # noqa: S607
             "-File", str(script), "-Root", str(base)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def any_pending(base: Path) -> bool:
    try:
        return any((d / PLAN).is_file() for d in base.iterdir() if d.is_dir())
    except OSError:
        return False


# endregion
