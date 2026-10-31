"""
The backups on disk, and the history the page shows.

Plain Python, no SDK imports: runs on the mod's worker thread (and in the offline tests).

Layout (outside SaveGames/, which Steam Cloud syncs):

    Documents/My Games/Borderlands 4/Saved/BL4Fix Save Backups/
        restore_helper.ps1                 copied in when a restore is staged
        <Steam ID>/
            2026-10-29_15-03-12/           one backup: plain copies of every .sav + info.json
            2026-10-29_18-40-02-before-restore/   made by the helper right before it restores
            pending_restore.json           a staged restore (restore.py), applied after the game exits
            last_restore.json              what the helper did

Each backup folder is complete on its own: a player can copy its files back by hand with the
game closed. A backup is written as `<name>.partial` and renamed when whole.

info.json: {"v": 1, "created": unix, "label": "launch" | "manual" | "before_restore" | "before_mod",
"keep": bool, "files": {"1.sav": fileinfo, ...}}; fileinfo = sha1, size, mtime, ok (decrypted
whole, None without a key), error, and savefile.summarize()'s fields. Folders the helper made
have no fileinfo yet: it's filled in the first time we load them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import savefile

ROOT_NAME = "BL4Fix Save Backups"
GB = 1 << 30
# Total size the backups of one account may take, whatever the count option says
MAX_BYTES = GB
READ_TRIES = 6  # a save the game is writing reads torn: try again a moment later
READ_RETRY_S = 0.5
LABELS = {"launch": "At launch", "manual": "Backed up by hand", "before_restore": "Before a restore",
          "before_mod": "Before loading a mod"}  # before_mod: backups.backup_now (the Mod Menu asks)


def root() -> Path:
    return savefile.saved_dir() / ROOT_NAME


def stamp_name(when: float, suffix: str = "") -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(when)) + (f"-{suffix}" if suffix else "")


@dataclass
class Account:
    sid: int
    save_dir: Path
    dir: Path
    # folder name -> info.json (loaded once, kept up to date by us)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The live save folder: name -> fileinfo, and the (name, size, mtime) it was read at
    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    live_stamp: Any = None


class Store:
    def __init__(self, keys: savefile.Keys, base: Path | None = None, saves_root: Path | None = None) -> None:
        self.keys = keys
        self.base = base or root()
        self.saves_root = saves_root  # tests point this at a fake SaveGames
        self.accounts: dict[int, Account] = {}

    # region Accounts and sessions

    def scan(self) -> list[Account]:
        """Finds the accounts and loads every backup's info (only new folders are read)."""
        found = []
        for sid, folder in savefile.accounts(self.saves_root):
            acc = self.accounts.get(sid)
            if acc is None:
                acc = self.accounts[sid] = Account(sid, folder, self.base / str(sid))
            acc.save_dir = folder
            self._load_sessions(acc)
            found.append(acc)
        return found

    def active(self) -> Account | None:
        """The account whose profile saved most recently (the one being played)."""
        accs = self.scan()
        return accs[0] if accs else None

    def _load_sessions(self, acc: Account) -> None:
        try:
            names = {p.name for p in acc.dir.iterdir() if p.is_dir() and not p.name.endswith(".partial")}
        except OSError:
            names = set()
        for gone in set(acc.sessions) - names:
            del acc.sessions[gone]
        for name in sorted(names - set(acc.sessions)):
            info = _read_json(acc.dir / name / "info.json") or {"v": 1, "label": "manual", "files": {}}
            info.setdefault("files", {})
            info.setdefault("created", (acc.dir / name).stat().st_mtime)
            missing = [p for p in savefile.save_files(acc.dir / name) if p.name not in info["files"]]
            for path in missing:  # a folder the helper made, or files copied in by hand
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                info["files"][path.name] = self._describe(acc, data, path.stat().st_mtime)
            if missing:
                try:
                    savefile.write_json(acc.dir / name / "info.json", info)
                except OSError:
                    pass
            acc.sessions[name] = info

    def ordered(self, acc: Account) -> list[str]:
        """Backup folder names, newest first."""
        return sorted(acc.sessions, key=lambda n: (acc.sessions[n].get("created", 0), n), reverse=True)

    # endregion
    # region Reading saves

    def _describe(self, acc: Account, data: bytes, mtime: float) -> dict[str, Any]:
        info: dict[str, Any] = {"sha1": hashlib.sha1(data).hexdigest(), "size": len(data), "mtime": mtime, "ok": None}
        key = self._key(acc)
        if key is None:
            info["error"] = "couldn't find the save key"
            return info
        try:
            info.update(savefile.summarize(savefile.decrypt(key, data)))
            info["ok"] = True
        except savefile.SaveError as ex:
            info["ok"] = False
            info["error"] = str(ex)
        return info

    def _key(self, acc: Account) -> bytes | None:
        try:
            profile = (acc.save_dir / "profile.sav").read_bytes()
        except OSError:
            # No live profile (deleted?): any backed-up one opens with the same key
            for name in self.ordered(acc):
                try:
                    profile = (acc.dir / name / "profile.sav").read_bytes()
                    break
                except OSError:
                    continue
            else:
                return None
        try:
            return self.keys.key(acc.sid, profile)
        except (OSError, savefile.SaveError):
            return None

    def key(self, acc: Account) -> bytes | None:
        return self._key(acc)

    def read_live(self, acc: Account, *, settle: bool = True) -> dict[str, tuple[bytes, dict[str, Any]]]:
        """
        Every save in the live folder, each read until it decrypts whole (the game may be writing
        it: a torn read fails the length/zlib checks). A file that never reads whole is kept as it
        is, marked ok False.
        """
        out = {}
        key = self._key(acc)
        for path in savefile.save_files(acc.save_dir):
            data, mtime = b"", 0.0
            for attempt in range(READ_TRIES if settle else 1):
                try:
                    mtime = path.stat().st_mtime
                    data = path.read_bytes()
                except OSError:
                    data = b""
                if data and (key is None or _whole(key, data)):
                    break
                if attempt + 1 < READ_TRIES and settle:
                    time.sleep(READ_RETRY_S)
            if data:
                out[path.name] = (data, self._describe(acc, data, mtime))
        return out

    def refresh_live(self, acc: Account) -> bool:
        """Re-reads the live saves if any changed on disk. True if something did."""
        stamp = _stamp(acc.save_dir)
        if stamp == acc.live_stamp:
            return False
        acc.live = {name: info for name, (_, info) in self.read_live(acc).items()}
        acc.live_stamp = stamp
        return True

    # endregion
    # region Backing up

    def backup(self, acc: Account, label: str, *, force: bool = False) -> tuple[str | None, str]:
        """
        Copies every save into a new backup folder. Returns (folder name or None, a message).
        Unless forced, nothing is written when every file is the same as in the newest backup.
        """
        files = self.read_live(acc)
        if not files:
            return None, "No saves found to back up."
        acc.live = {name: info for name, (_, info) in files.items()}
        acc.live_stamp = _stamp(acc.save_dir)
        newest = self.ordered(acc)[:1]
        if newest and not force:
            before = {n: f.get("sha1") for n, f in acc.sessions[newest[0]]["files"].items()}
            if before == {n: info["sha1"] for n, (_, info) in files.items()}:
                return None, "Nothing changed since the last backup."
        now = time.time()
        name = stamp_name(now, "before-restore" if label == "before_restore" else "")
        while (acc.dir / name).exists() or name in acc.sessions:
            now += 1
            name = stamp_name(now, "before-restore" if label == "before_restore" else "")
        partial = acc.dir / (name + ".partial")
        partial.mkdir(parents=True, exist_ok=True)
        info = {"v": 1, "created": now, "label": label, "keep": False, "files": {}}
        for fname, (data, finfo) in files.items():
            (partial / fname).write_bytes(data)
            os.utime(partial / fname, (finfo["mtime"], finfo["mtime"]))  # the save's own time
            info["files"][fname] = finfo
        savefile.write_json(partial / "info.json", info)
        partial.rename(acc.dir / name)
        acc.sessions[name] = info
        bad = [n for n, f in info["files"].items() if f.get("ok") is False]
        note = f" ({', '.join(bad)} didn't read whole: kept as it was)" if bad else ""
        return name, f"Backed up {len(files)} saves{note}."

    def clean_partials(self) -> None:
        """Half-written backups from a session that ended mid-backup."""
        for acc_dir in _dirs(self.base):
            for part in _dirs(acc_dir):
                if part.name.endswith(".partial") and time.time() - part.stat().st_mtime > 60:
                    shutil.rmtree(part, ignore_errors=True)

    def set_keep(self, acc: Account, name: str, keep: bool) -> bool:
        info = acc.sessions.get(name)
        if info is None:
            return False
        info["keep"] = keep
        savefile.write_json(acc.dir / name / "info.json", info)
        return True

    def session_bytes(self, acc: Account, name: str) -> int:
        return sum(int(f.get("size", 0)) for f in acc.sessions[name]["files"].values())

    def prune(self, acc: Account, keep: int | None) -> list[str]:
        """
        Removes the oldest backups past `keep` (None: no count limit) and past MAX_BYTES in all.
        Backups marked Keep are never removed and don't count; the newest backup always stays.
        """
        names = self.ordered(acc)
        removable = [n for n in names[1:] if not acc.sessions[n].get("keep")]
        doomed = []
        if keep is not None:
            unkept = [n for n in names if not acc.sessions[n].get("keep")]
            excess = len(unkept) - max(1, keep)
            doomed = removable[len(removable) - excess :] if excess > 0 else []
        total = sum(self.session_bytes(acc, n) for n in names if n not in doomed)
        for n in reversed(removable):
            if total <= MAX_BYTES:
                break
            if n not in doomed:
                doomed.append(n)
                total -= self.session_bytes(acc, n)
        for n in doomed:
            shutil.rmtree(acc.dir / n, ignore_errors=True)
            if not (acc.dir / n).exists():
                acc.sessions.pop(n, None)
        return doomed

    # endregion
    # region The page's view

    def view(self, acc: Account, keep_label: str) -> dict[str, Any]:
        """Everything the page shows, as plain JSON (written to a loose file the page reads)."""
        names = self.ordered(acc)
        sessions = acc.sessions
        total = sum(self.session_bytes(acc, n) for n in names)
        per = self.session_bytes(acc, names[0]) if names else sum(int(f.get("size", 0)) for f in acc.live.values())
        live_by_guid = {f["guid"]: (n, f) for n, f in acc.live.items() if f.get("guid")}
        return {
            "account": str(acc.sid),
            "folder": str(acc.dir),
            "count": len(names),
            "kept": sum(1 for n in names if sessions[n].get("keep")),
            "bytes": total,
            "size": _size(total),
            "per_backup": _size(per),
            "fit_1gb": int(MAX_BYTES // per) if per else 0,
            "keep_label": keep_label,
            "last": _session_row(self, acc, names[0]) if names else None,
            "characters": _characters(acc, names, live_by_guid),
            "bank": _bank(acc, names),
            "sessions": [_session_row(self, acc, n) for n in names],
        }

    # endregion


# region View helpers


def _characters(acc: Account, names: list[str], live_by_guid: dict[str, tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    chars: dict[str, dict[str, Any]] = {}
    for guid, (fname, f) in live_by_guid.items():
        chars[guid] = {"guid": guid, "file": fname, "exists": True, "now": _lite(f), "versions": [], "_seen": {}}
    for name in names:  # newest first
        for fname, f in acc.sessions[name]["files"].items():
            guid = f.get("guid")
            if not guid:
                continue
            ch = chars.get(guid)
            if ch is None:
                ch = chars[guid] = {"guid": guid, "file": fname, "exists": False, "now": _lite(f), "versions": [], "_seen": {}}
            seen = ch["_seen"]
            if f["sha1"] in seen:
                seen[f["sha1"]]["oldest_backup"] = name
                continue
            entry = {
                "id": f["sha1"][:12],
                "backup": name,  # the newest backup holding this version (restored from there)
                "oldest_backup": name,
                "file": fname,
                "saved": f.get("mtime", 0),
                "saved_text": when(f.get("mtime", 0)),
                "ok": f.get("ok"),
                "error": f.get("error", ""),
                "info": _lite(f),
                "_items": Counter(f.get("items", ())),
            }
            seen[f["sha1"]] = entry
            ch["versions"].append(entry)
    out = []
    for ch in chars.values():
        live = acc.live.get(ch["file"]) if ch["exists"] else None
        now_items = Counter(live.get("items", ())) if live else None
        versions = ch["versions"]
        versions.sort(key=lambda v: v["saved"], reverse=True)
        for i, v in enumerate(versions):
            v["current"] = bool(live) and v["id"] == live["sha1"][:12]
            if now_items is not None:
                v["gone_now"] = _count(v["_items"] - now_items)  # here, but not in the save now
                v["new_now"] = _count(now_items - v["_items"])
            older = versions[i + 1] if i + 1 < len(versions) else None
            if older is not None:
                v["vs_older"] = _delta(older["info"], v["info"], _count(v["_items"] - older["_items"]), _count(older["_items"] - v["_items"]))
            del v["_items"]
        del ch["_seen"]
        ch["name"] = ch["now"].get("name") or "Unknown character"
        ch["slot"] = _slot(ch["file"])
        out.append(ch)
    out.sort(key=lambda c: (not c["exists"], c["slot"]))
    return out


def _bank(acc: Account, names: list[str]) -> dict[str, Any]:
    live = acc.live.get("profile.sav")
    now_items = Counter(live.get("bank", ())) if live else None
    versions: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for name in names:
        f = acc.sessions[name]["files"].get("profile.sav")
        if not f or not f.get("bank_sig"):
            continue
        if f["bank_sig"] in seen:
            seen[f["bank_sig"]]["oldest_backup"] = name
            continue
        items = Counter(f.get("bank", ()))
        entry = {
            "id": f["bank_sig"],
            "backup": name,
            "oldest_backup": name,
            "saved": f.get("mtime", 0),
            "saved_text": when(f.get("mtime", 0)),
            "items": len(f.get("bank", ())),
            "current": bool(live) and live.get("bank_sig") == f["bank_sig"],
            "_items": items,
        }
        if now_items is not None:
            entry["gone_now"] = _count(items - now_items)
            entry["new_now"] = _count(now_items - items)
        seen[f["bank_sig"]] = entry
        versions.append(entry)
    for i, v in enumerate(versions):
        older = versions[i + 1] if i + 1 < len(versions) else None
        if older is not None:
            v["added"] = _count(v["_items"] - older["_items"])
            v["removed"] = _count(older["_items"] - v["_items"])
    for v in versions:
        del v["_items"]
    return {"now": len(live.get("bank", ())) if live else None, "versions": versions}


def _session_row(store: Store, acc: Account, name: str) -> dict[str, Any]:
    info = acc.sessions[name]
    files = []
    for fname, f in sorted(info["files"].items(), key=lambda kv: (kv[0] == "profile.sav", _slot(kv[0]))):
        who = "Profile and bank" if fname == "profile.sav" else f.get("name") or fname
        files.append({"file": fname, "who": who, "ok": f.get("ok"), "level": f.get("level")})
    return {
        "name": name,
        "label": LABELS.get(info.get("label", ""), "Backup"),
        "created": info.get("created", 0),
        "created_text": when(info.get("created", 0)),
        "keep": bool(info.get("keep")),
        "size": _size(store.session_bytes(acc, name)),
        "files": files,
        "bad": sum(1 for f in info["files"].values() if f.get("ok") is False),
    }


_LITE = ("name", "class", "level", "spec", "difficulty", "uvh", "playtime", "cash", "eridium", "backpack",
         "equipped", "lost_loot", "story_done", "last_played", "ok", "error", "size", "mtime")


def _lite(f: dict[str, Any]) -> dict[str, Any]:
    return {k: f[k] for k in _LITE if k in f}


def _delta(older: dict[str, Any], newer: dict[str, Any], added: int, removed: int) -> dict[str, Any]:
    d: dict[str, Any] = {"items_added": added, "items_removed": removed}
    for k in ("level", "spec", "uvh", "cash", "eridium", "playtime"):
        if k in older and k in newer:
            d[k] = newer[k] - older[k]
    return d


def _count(c: Counter[str]) -> int:
    return sum(c.values())


def _slot(fname: str) -> int:
    stem = fname.split(".")[0]
    return int(stem) if stem.isdigit() else 999


def when(t: float) -> str:
    """'Today 15:03', 'Yesterday 09:12', 'Oct 25 17:35', 'Oct 25 2025 17:35'."""
    if not t:
        return "?"
    then = datetime.fromtimestamp(t)
    days = (date.today() - then.date()).days
    clock = then.strftime("%H:%M")
    if days == 0:
        return f"Today {clock}"
    if days == 1:
        return f"Yesterday {clock}"
    month = then.strftime("%b")
    if then.year == date.today().year:
        return f"{month} {then.day} {clock}"
    return f"{month} {then.day} {then.year} {clock}"


def _size(n: float) -> str:
    if n >= GB:
        return f"{n / GB:.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def _whole(key: bytes, data: bytes) -> bool:
    try:
        savefile.decrypt(key, data)
    except savefile.SaveError:
        return False
    return True


def _stamp(folder: Path) -> Any:
    try:
        return tuple(sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in savefile.save_files(folder)))
    except OSError:
        return None


def _dirs(path: Path) -> list[Path]:
    try:
        return [p for p in path.iterdir() if p.is_dir()]
    except OSError:
        return []


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# endregion
