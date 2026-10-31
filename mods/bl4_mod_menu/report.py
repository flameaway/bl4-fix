"""
Bug reports: one zip with what a bug needs (versions, errors, logs, the newest crash, installed
mods and their settings), private details masked. The Mod Menu's "Report a problem" builds it in
game (on a worker thread, with `live` from the running mods); the setup app builds it with the
game closed (`live=None`), for when the game won't start.

Plain Python that runs on the setup app's 3.11 and the game's 3.14: no SDK imports, no relative
imports (the setup app imports this file from the Mod Menu's .sdkmod under a stand-in package).
Only ever reads, except for the zip it writes into `out_dir`. Nothing is sent anywhere: the player
attaches the zip to the GitHub issue `issue_url` opens.
"""

from __future__ import annotations

import calendar
import ctypes
import ipaddress
import json
import os
import platform
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

# Same as updates.REPO (not imported: this file can't import the mod's other modules)
REPO = "flameaway/bl4-fix"
STEAM_APP_ID = "1285190"
KEEP_REPORTS = 5
REPORT_PREFIX = "bl4fix-report-"

LOG_TAIL = 2 * 1024 * 1024  # per SDK log
TRACE_CAP = 512 * 1024  # every traceback found in a log, outside the tail too
CRASH_CAP = 1024 * 1024
ERRORS_READ = 4 * 1024 * 1024
MAX_BLOCKS = 300
URL_CAP = 1900

WIN64_REL = Path("OakGame/Binaries/Win64")
PLUGINS_REL = WIN64_REL / "Plugins"
MOD_PAKS_REL = Path("OakGame/Content/Paks/~mods")
# A dot folder: the SDK doesn't try to import it as a mod
REPORTS_REL = Path("sdk_mods/.bug_reports")
ERRORS_FILE = "bl4fix_errors.jsonl"


def default_dir(game_dir: Path) -> Path:
    return Path(game_dir) / REPORTS_REL


# region reading


def _read_bytes(path: Path, cap: int) -> tuple[bytes, int]:
    """The last `cap` bytes of a file and its size ((b"", -1) if it can't be read)."""
    try:
        # Python opens files shareable, so the running game's log reads fine
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - cap))
            return f.read(cap), size
    except OSError:
        return b"", -1


def _read_text(path: Path, cap: int = 1024 * 1024) -> str | None:
    data, size = _read_bytes(path, cap)
    if size < 0:
        return None
    text = data.decode("utf-8", "replace")
    if size > cap:
        text = text.split("\n", 1)[-1]  # drop the cut first line
    return text


def _read_json(path: Path) -> Any:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _iso(t: float | None) -> str | None:
    return None if t is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


# endregion
# region privacy


class Redactor:
    """Masks the Windows user, Steam names, BL4 Direct friends, IPs and account ids."""

    _IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.])")
    _IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:.])")
    _STEAMID = re.compile(r"(?<!\d)7656119\d{10}(?!\d)")
    _HEX32 = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{32}(?![0-9A-Za-z])")
    _XML_IDS = re.compile(r"<(MachineId|LoginId|EpicAccountId|UserName|UserActivityHint|CommandLine)>[^<]*</\1>")
    _USERS_DIR = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)(?!Public\b|Default\b)([^\\/\s\"'<>|:*?]+)")
    _CGNAT = ipaddress.ip_network("100.64.0.0/10")

    def __init__(self, game_dir: Path | None = None) -> None:
        self._ips: dict[str, str] = {}
        self._words: list[tuple[re.Pattern[str], str]] = []
        home = os.environ.get("USERPROFILE") or str(Path.home())
        user = os.environ.get("USERNAME") or Path(home).name
        names = {user: "<user>", os.environ.get("COMPUTERNAME", ""): "<pc>"}
        for i, name in enumerate(self._steam_names(game_dir), 1):
            names.setdefault(name, f"<steam-name-{i}>")
        for i, name in enumerate(self._friend_names(game_dir), 1):
            names.setdefault(name, f"<friend-{i}>")
        # Longest first, so "Bob Smith" goes before "Bob"
        for name in sorted((n for n in names if n and len(n) >= 3), key=len, reverse=True):
            self._words.append((re.compile(rf"(?i)(?<![\w]){re.escape(name)}(?![\w])"), names[name]))

    @staticmethod
    def _steam_names(game_dir: Path | None) -> list[str]:
        roots = [Path(r"C:\Program Files (x86)\Steam")]
        if game_dir is not None:
            parts = [p.lower() for p in Path(game_dir).parts]
            if "steamapps" in parts:
                roots.insert(0, Path(*Path(game_dir).parts[: parts.index("steamapps")]))
        found: list[str] = []
        for root in roots:
            text = _read_text(root / "config" / "loginusers.vdf", 256 * 1024) or ""
            found += re.findall(r'"(?:AccountName|PersonaName)"\s+"([^"]+)"', text)
        return list(dict.fromkeys(found))

    @staticmethod
    def _friend_names(game_dir: Path | None) -> list[str]:
        if game_dir is None:
            return []
        data = _read_json(Path(game_dir) / "sdk_mods" / "settings" / "bl4_direct_friends.json")
        friends = data.get("friends") if isinstance(data, dict) else None
        if not isinstance(friends, list):
            return []
        return [str(f["name"]) for f in friends if isinstance(f, dict) and f.get("name")]

    def _ip_token(self, ip: str, kind: str) -> str:
        if ip not in self._ips:
            self._ips[ip] = f"<{kind}-{sum(1 for v in self._ips.values() if v.startswith('<' + kind)) + 1}>"
        return self._ips[ip]

    def _v4(self, m: re.Match[str]) -> str:
        text = m.group(0)
        if any(int(g) > 255 for g in m.groups()):
            return text
        ip = ipaddress.ip_address(text)
        if ip.is_loopback or ip.is_unspecified or text == "255.255.255.255":
            return text
        return self._ip_token(text, "lan-ip" if ip.is_private or ip in self._CGNAT else "ip")

    def _v6(self, m: re.Match[str]) -> str:
        text = m.group(0)
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return text
        if ip.is_loopback or ip.is_unspecified:
            return text
        return self._ip_token(text, "ip6")

    def __call__(self, text: str) -> str:
        text = self._USERS_DIR.sub(r"\1<user>", text)
        for pattern, token in self._words:
            text = pattern.sub(token, text)
        text = self._XML_IDS.sub(lambda m: f"<{m.group(1)}>(removed)</{m.group(1)}>", text)
        text = self._STEAMID.sub("<steamid>", text)
        text = self._HEX32.sub("<id>", text)
        text = self._IPV4.sub(self._v4, text)
        return self._IPV6.sub(self._v6, text)

    def obj(self, value: Any) -> Any:
        """The same masking over every string (keys too) in a JSON value."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {self(str(k)): self.obj(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.obj(v) for v in value]
        return value


# endregion
# region the SDK log


_LOG_LINE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+Z)\s+(?P<thread>[0-9a-fA-F]+)\s+(?P<loc>.*?)\s+(?P<lvl>[A-Z]+)\|\s?(?P<msg>.*)$")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]{12,16}\b")


def log_blocks(text: str) -> list[dict[str, Any]]:
    """
    Every error block in an SDK log (ERR lines and Python tracebacks, with the lines that follow
    on the same thread), duplicates folded into one with a count.
    """
    blocks: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None

    def close() -> None:
        nonlocal current
        if current is None:
            return
        lines = current["lines"]
        sig = _ADDRESS.sub("0x?", "\n".join(lines))
        known = blocks.get(sig)
        if known is not None:
            known["count"] += 1
            known["last"] = current["first"]
        elif len(blocks) < MAX_BLOCKS:
            blocks[sig] = {"first": current["first"], "last": current["first"], "count": 1,
                           "where": current["where"], "lines": lines}
        current = None

    for raw in text.splitlines():
        m = _LOG_LINE.match(raw)
        if m is None:
            # A line the SDK didn't prefix: part of the message above it
            if current is not None and len(current["lines"]) < 120:
                current["lines"].append(raw)
            continue
        msg = m["msg"]
        starts = m["lvl"] in ("ERR", "ERROR") or msg.startswith("Traceback (most recent call last)")
        # One logging call = one timestamp: its lines stay together, the next call starts anew
        if current is not None and (m["thread"], m["lvl"], m["ts"]) == (current["thread"], current["lvl"], current["first"]):
            if len(current["lines"]) < 120:
                current["lines"].append(msg)
            continue
        close()
        if starts:
            current = {"thread": m["thread"], "lvl": m["lvl"], "first": m["ts"], "where": m["loc"].lstrip("~ ").strip(), "lines": [msg]}
    close()
    return sorted(blocks.values(), key=lambda b: b["first"])


def _blocks_text(blocks: list[dict[str, Any]]) -> str:
    out: list[str] = []
    size = 0
    for b in blocks:
        head = f"=== {b['first']}  x{b['count']}" + (f" (last {b['last']})" if b["count"] > 1 else "") + f"  {b['where']}"
        body = "\n".join([head, *b["lines"], ""])
        size += len(body)
        if size > TRACE_CAP:
            out.append(f"... {len(blocks) - len(out)} more blocks cut")
            break
        out.append(body)
    return "\n".join(out)


def _log_info(path: Path) -> dict[str, Any]:
    """The tail and every error block of one SDK log."""
    data, size = _read_bytes(path, LOG_TAIL)
    info: dict[str, Any] = {"file": path.name, "exists": size >= 0, "size": size, "modified": _iso(_mtime(path))}
    if size < 0:
        return info
    tail = data.decode("utf-8", "replace")
    whole = tail
    if size > LOG_TAIL:
        tail = f"[first {size - LOG_TAIL} bytes cut; error blocks from them are in {path.stem}.errors.txt]\n" + tail.split("\n", 1)[-1]
        # The errors anywhere in the file, read in pieces so a huge log stays bounded in memory
        whole = _scan_errors(path)
    info["tail"] = tail
    info["blocks"] = log_blocks(whole)
    head = (_read_bytes_head(path, 64 * 1024) or b"").decode("utf-8", "replace")
    first = next((m for m in map(_LOG_LINE.match, head.splitlines()[:5]) if m), None)
    if first:
        info["started"] = first["ts"]
        try:
            info["started_t"] = calendar.timegm(time.strptime(first["ts"][:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
    for key, pattern in (("unrealsdk", r"unrealsdk (v[\w.]+)"), ("pyunrealsdk", r"pyunrealsdk (v[\w.]+)")):
        if mm := re.search(pattern, head):
            info[key] = mm.group(1)
    return info


def _read_bytes_head(path: Path, n: int) -> bytes | None:
    try:
        with path.open("rb") as f:
            return f.read(n)
    except OSError:
        return None


def _scan_errors(path: Path) -> str:
    """The lines around errors in a log too big to read whole (each ERR/traceback + 60 lines)."""
    keep: list[str] = []
    budget = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if " ERR|" in line or "Traceback (most recent" in line:
                    budget = 60
                if budget > 0:
                    keep.append(line.rstrip("\n"))
                    budget -= 1
                    if len(keep) > 200_000:
                        break
    except OSError:
        return ""
    return "\n".join(keep)


# endregion
# region what's installed


def _pyproject(text: str) -> dict[str, str]:
    project = re.search(r"^\[project\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    body = project.group(1) if project else text
    out = {}
    for key in ("name", "version"):
        if m := re.search(rf'^{key}\s*=\s*"([^"]*)"', body, re.M):
            out[key] = m.group(1)
    return out


def _sdkmod_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"id": path.stem, "kind": "sdkmod", "file": path.name, "size": None, "modified": _iso(_mtime(path))}
    try:
        info["size"] = path.stat().st_size
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            toml = f"{path.stem}/pyproject.toml"
            if toml not in names:
                toml = next((n for n in names if n.count("/") == 1 and n.endswith("/pyproject.toml")), "")
            if toml:
                info.update(_pyproject(zf.read(toml).decode("utf-8", "replace")))
    except (OSError, zipfile.BadZipFile, KeyError) as ex:
        info["error"] = type(ex).__name__
    return info


def _folder_info(path: Path, kind: str) -> dict[str, Any]:
    info: dict[str, Any] = {"id": path.name, "kind": kind, "modified": _iso(_mtime(path))}
    text = _read_text(path / "pyproject.toml", 64 * 1024)
    if text:
        info.update(_pyproject(text))
    return info


def _extra_folders(game_dir: Path) -> list[Path]:
    """`[mod_manager] extra_folders` from unrealsdk.user.toml (a dev setup; players don't have it)."""
    text = _read_text(game_dir / PLUGINS_REL / "unrealsdk.user.toml", 64 * 1024)
    if not text:
        return []
    try:
        import tomllib  # noqa: PLC0415

        folders = tomllib.loads(text).get("mod_manager", {}).get("extra_folders", [])
    except (ImportError, ValueError):
        return []
    return [Path(f) for f in folders if isinstance(f, str)] if isinstance(folders, list) else []


def installed_mods(game_dir: Path) -> list[dict[str, Any]]:
    """
    Every .sdkmod, loose mod folder and dev folder mod, with versions and settings flags. A mod
    found twice is one entry (the copy the SDK loads: dev folders, then loose folders, then
    .sdkmods), the others under `copies`.
    """
    sdk_mods = game_dir / "sdk_mods"
    found: list[dict[str, Any]] = []
    for folder in _extra_folders(game_dir):
        try:
            subs = sorted(p for p in folder.iterdir() if p.is_dir() and (p / "__init__.py").is_file())
        except OSError:
            continue
        found += [_folder_info(p, "dev folder") for p in subs]
    try:
        entries = sorted(sdk_mods.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        entries = []
    entries = [e for e in entries if not e.name.startswith(".") and e.name not in ("settings", "__pycache__")]
    found += [_folder_info(e, "folder") for e in entries if e.is_dir() and (e / "__init__.py").is_file()]
    found += [_sdkmod_info(e) for e in entries if e.is_file() and e.suffix == ".sdkmod"]
    by_id: dict[str, dict[str, Any]] = {}
    for mod in found:
        first = by_id.setdefault(mod["id"], mod)
        if first is not mod:
            first.setdefault("copies", []).append({k: mod.get(k) for k in ("kind", "version", "file") if mod.get(k)})
    mods = sorted(by_id.values(), key=lambda m: m["id"].lower())
    settings = sdk_mods / "settings"
    for mod in mods:
        data = _read_json(settings / f"{mod['id']}.json")
        if isinstance(data, dict):
            mod["enabled"] = data.get("enabled")
            mod["settings"] = data
    return mods


def _paks(game_dir: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted((game_dir / MOD_PAKS_REL).iterdir())
    except OSError:
        return []
    return [{"name": p.name, "size": p.stat().st_size, "modified": _iso(_mtime(p))} for p in entries if p.is_file()]


# endregion
# region game, SDK, crashes


def _exe_version(path: Path) -> str | None:
    """The exe's file version resource (None if there's none or this isn't Windows)."""
    try:
        version = ctypes.WinDLL("version")  # our own instance: no shared prototypes change
        version.GetFileVersionInfoSizeW.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p)
        version.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
        version.GetFileVersionInfoW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        version.VerQueryValueW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32))
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), ctypes.c_uint32()
        if not version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or length.value < 52:
            return None
        ms, ls = ctypes.cast(ptr.value + 8, ctypes.POINTER(ctypes.c_uint32 * 2)).contents  # dwFileVersionMS/LS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def game_info(game_dir: Path) -> dict[str, Any]:
    exe = game_dir / WIN64_REL / "Borderlands4.exe"
    info: dict[str, Any] = {"exe_version": _exe_version(exe), "exe_modified": _iso(_mtime(exe)), "store": "other"}
    parts = [p.lower() for p in game_dir.parts]
    if "steamapps" in parts:
        info["store"] = "Steam"
        acf = Path(*game_dir.parts[: parts.index("steamapps") + 1]) / f"appmanifest_{STEAM_APP_ID}.acf"
        text = _read_text(acf, 256 * 1024) or ""
        for key in ("buildid", "LastUpdated", "StateFlags"):
            if m := re.search(rf'"{key}"\s+"(\d+)"', text):
                info[key] = m.group(1)
        if "LastUpdated" in info:
            info["LastUpdated"] = _iso(float(info["LastUpdated"]))
    elif "epic games" in " ".join(parts):
        info["store"] = "Epic"
    return info


def sdk_info(game_dir: Path) -> dict[str, Any]:
    plugins = game_dir / PLUGINS_REL
    info: dict[str, Any] = {}
    text = _read_text(plugins / "unrealsdk.toml", 64 * 1024) or ""
    if m := re.search(r'\[mod_manager\][^\[]*display_version\s*=\s*"([^"]+)"', text):
        info["mod_manager"] = m.group(1)
    try:
        dlls = sorted(p.name for p in plugins.glob("python3*.dll") if p.name != "python3.dll")
    except OSError:
        dlls = []
    if dlls:
        digits = dlls[-1][len("python"):-len(".dll")]
        info["python"] = f"{digits[0]}.{digits[1:]}"
    info["dev_folders"] = [str(p) for p in _extra_folders(game_dir)]
    return info


def _documents() -> Path:
    """The user's Documents folder (it can be moved, e.g. into OneDrive)."""
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.WinDLL("shell32").SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # CSIDL_PERSONAL
            return Path(buf.value)
    except (OSError, AttributeError):
        pass
    return Path.home() / "Documents"


def saved_dir() -> Path:
    return _documents() / "My Games" / "Borderlands 4" / "Saved"


def newest_crash() -> dict[str, Any] | None:
    crashes = saved_dir() / "Crashes"
    try:
        dirs = [p for p in crashes.iterdir() if p.is_dir() and p.name.startswith("UECC-")]
    except OSError:
        return None
    if not dirs:
        return None
    newest = max(dirs, key=lambda p: _mtime(p) or 0)
    info: dict[str, Any] = {"folder": newest.name, "time": _iso(_mtime(newest)), "t": _mtime(newest), "files": {}}
    try:
        files = sorted(newest.iterdir())
    except OSError:
        files = []
    for f in files:
        if f.name == "CrashContext.runtime-xml" or (f.suffix.lower() in (".log", ".txt") and (f.stat().st_size < CRASH_CAP)):
            text = _read_text(f, CRASH_CAP)
            if text is not None:
                info["files"][f.name] = text
    xml = info["files"].get("CrashContext.runtime-xml", "")
    for key in ("CrashType", "ErrorMessage", "BuildVersion", "SecondsSinceStart", "PlatformFullName"):
        if m := re.search(rf"<{key}>(.*?)</{key}>", xml, re.S):
            info[key] = m.group(1).strip()[:400]
    if m := re.search(r"<PCallStack>(.*?)</PCallStack>", xml, re.S):
        info["stack_top"] = [line.strip() for line in m.group(1).strip().splitlines() if line.strip()][:15]
    return info


# endregion
# region our errors (settings/bl4fix_errors.jsonl, written by the Mod Menu's errors.py)


def recorded_errors(game_dir: Path, live: dict | None = None) -> list[dict[str, Any]]:
    """One entry per (session, signature), with its count lines folded in; newest first."""
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    counts: dict[tuple[str, str], int] = {}
    text = _read_text(game_dir / "sdk_mods" / "settings" / ERRORS_FILE, ERRORS_READ) or ""
    rows: list[Any] = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    rows += list((live or {}).get("errors") or [])
    for row in rows:
        if not isinstance(row, dict) or "sig" not in row:
            continue
        key = (str(row.get("session", "")), str(row["sig"]))
        if "trace" in row or "msg" in row:
            entry = entries.setdefault(key, {})
            entry.update({k: v for k, v in row.items() if k != "count" or isinstance(v, int)})
        if isinstance(row.get("count"), int):
            counts[key] = max(counts.get(key, 0), row["count"])
    for key, entry in entries.items():
        entry["count"] = max(counts.get(key, 1), entry.get("count", 1) if isinstance(entry.get("count"), int) else 1)
    return sorted(entries.values(), key=lambda e: str(e.get("t", "")), reverse=True)


# endregion
# region build


def _windows() -> str:
    """platform.release() says 10 on Windows 11 too; the build number tells them apart."""
    release = platform.release()
    try:
        if release == "10" and int(platform.version().split(".")[2]) >= 22000:
            return "11"
    except (IndexError, ValueError):
        pass
    return release


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def _mod_rows(mods: list[dict[str, Any]], live: dict | None) -> list[dict[str, Any]]:
    """Installed mods with what the running game knows (status, error count) merged in."""
    by_id = {m["id"]: dict(m) for m in mods}
    for lm in (live or {}).get("mods") or []:
        if not isinstance(lm, dict) or not lm.get("id"):
            continue
        row = by_id.setdefault(lm["id"], {"id": lm["id"], "kind": "loaded"})
        for key in ("name", "version", "enabled", "status", "errors"):
            if lm.get(key) is not None:
                row[key] = lm[key]
    return sorted(by_id.values(), key=lambda m: (not str(m["id"]).startswith("bl4_"), str(m["id"])))


def _summary(data: dict[str, Any]) -> str:
    game, sdk, session = data["game"], data["sdk"], data.get("session") or {}
    menu = next((m for m in data["mods"] if m["id"] == "bl4_mod_menu"), {})
    lines = [
        f"BL4 Fix bug report, {data['created']} (made {'in game' if data['from'] == 'game' else 'by the setup app, game closed'})",
        f"Mod Menu {menu.get('version', '?')}  //  BL4 Direct Connect "
        f"{next((m.get('version', '?') for m in data['mods'] if m['id'] == 'bl4_direct'), 'not installed')}",
        f"Game: {game.get('store')} build {game.get('buildid', '?')}, exe {game.get('exe_version') or '?'}"
        + (f", updated {game['LastUpdated']}" if game.get("LastUpdated") else ""),
        f"SDK: mod manager {sdk.get('mod_manager', '?')}, unrealsdk {sdk.get('unrealsdk', '?')}, "
        f"pyunrealsdk {sdk.get('pyunrealsdk', '?')}, Python {sdk.get('python', '?')}",
        f"Windows {_windows()} ({platform.version()})",
    ]
    if data["from"] == "game":
        lines.append(f"Session {session.get('id') or '?'}: role {session.get('role') or '?'}, map {session.get('map') or '?'}")
    per_mod: dict[str, list[int]] = {}
    for e in data["errors"]:
        per_mod.setdefault(str(e.get("mod") or "?"), []).append(int(e.get("count") or 1))
    if per_mod:
        lines.append("Errors recorded: " + ", ".join(f"{m} {len(c)} ({sum(c)}x)" for m, c in sorted(per_mod.items(), key=lambda kv: -sum(kv[1]))))
    else:
        lines.append("Errors recorded: none")
    logs = data["logs"]
    lines.append("Log errors: " + ", ".join(f"{info['file']} {len(info.get('blocks', []))}" if info.get("exists") else f"{info['file']} missing" for info in logs))
    crash = data.get("crash")
    if crash:
        when = crash["time"]
        current = logs[0].get("started_t")
        rel = "" if current is None or crash.get("t") is None else " (this session)" if crash["t"] >= current else " (an earlier session)"
        message = (crash.get("ErrorMessage") or "").strip().split("\n", 1)[0][:160]
        lines.append(f"Newest crash: {when}{rel}, {crash.get('CrashType', '?')}: {message}".rstrip(": "))
    else:
        lines.append("Newest crash: none found")
    lines += ["", "== Mods =="]
    for m in data["mods"]:
        flag = {True: "on", False: "OFF", None: ""}.get(m.get("enabled"), str(m.get("enabled")))
        copies = ", ".join(f"{c.get('kind')} {c.get('version', '?')}" for c in m.get("copies") or [])
        extra = "  ".join(x for x in (
            m.get("status") or "",
            f"{m['errors']} errors" if m.get("errors") else "",
            m.get("kind", "") if m.get("kind") != "sdkmod" else "",
            f"(also {copies})" if copies else "",
        ) if x)
        lines.append(f"  {m['id']:28} {m.get('version', '?'):10} {flag:4} {m.get('name', '')}  {extra}".rstrip())
    lines += ["", "== Paks/~mods =="] + ([f"  {p['name']}  {p['size']} bytes  {p['modified']}" for p in data["paks"]] or ["  (none)"])
    if data.get("health"):
        lines += ["", "== Health ==", "  " + json.dumps(data["health"], ensure_ascii=False)[:2000]]
    if data["errors"]:
        lines += ["", "== Newest errors (all in errors.jsonl) =="]
        for e in data["errors"][:10]:
            lines.append(f"  [{e.get('t', '?')}] {e.get('mod', '?')} {e.get('version', '')} x{e.get('count', 1)}: {str(e.get('msg', ''))[:200]}")
            trace = str(e.get("trace") or "").strip().splitlines()
            lines += [f"      {t}" for t in trace[-6:]]
    for info in logs:
        blocks = info.get("blocks") or []
        if blocks:
            lines += ["", f"== {info['file']}: last error blocks (all in {Path(info['file']).stem}.errors.txt) =="]
            for b in blocks[-5:]:
                lines.append(f"  {b['first']} x{b['count']} {b['where']}")
                lines += [f"      {t}" for t in b["lines"][-6:]]
    if crash and crash.get("stack_top"):
        lines += ["", "== Newest crash: call stack top =="] + [f"  {s}" for s in crash["stack_top"]]
    return "\n".join(lines) + "\n"


def collect(game_dir: Path, live: dict | None = None, redact: Redactor | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """The report's data (for report.json / summary.txt) and the extra files for the zip, masked."""
    game_dir = Path(game_dir)
    r = redact or Redactor(game_dir)
    plugins = game_dir / PLUGINS_REL
    logs = [_log_info(plugins / "unrealsdk.log"), _log_info(plugins / "unrealsdk.prev.log")]
    sdk = sdk_info(game_dir)
    for key in ("unrealsdk", "pyunrealsdk"):
        if logs[0].get(key):
            sdk[key] = logs[0][key]
    sdk.update({k: v for k, v in ((live or {}).get("sdk") or {}).items() if v})
    crash = newest_crash()
    mods = installed_mods(game_dir)
    files: dict[str, str] = {}
    for m in mods:
        if "settings" in m:
            files[f"settings/{m['id']}.json"] = r(json.dumps(m.pop("settings"), indent=2, ensure_ascii=False))
    for info in logs:
        stem = Path(info["file"]).stem
        if info.get("tail"):
            files[f"logs/{info['file']}"] = r(info.pop("tail"))
        if info.get("blocks"):
            files[f"logs/{stem}.errors.txt"] = r(_blocks_text(info["blocks"]))
    if crash:
        for name, text in crash.pop("files").items():
            files[f"crash/{crash['folder']}/{name}"] = r(text)
    crash_client = _read_text(saved_dir() / "Logs" / "CrashReportClient.log", 256 * 1024)
    if crash_client:
        files["crash/CrashReportClient.log"] = r(crash_client)
    # The Mod Menu's Performance page (perf/bench.py), and the old Mod Profiler mod's
    for name in ("bl4_performance_report.txt", "bl4_profiler_report.txt"):
        profiler = _read_text(game_dir / "sdk_mods" / "settings" / name, 512 * 1024)
        if profiler:
            files[name] = r(profiler)
    errors = recorded_errors(game_dir, live)
    if errors:
        files["errors.jsonl"] = "".join(json.dumps(r.obj(e), ensure_ascii=False) + "\n" for e in errors)
    health = (live or {}).get("health") or _read_json(game_dir / "sdk_mods" / "settings" / "bl4fix_health.json")
    session = None
    if live is not None:
        session = {"id": live.get("session"), "map": live.get("map"), "role": live.get("role")}
    data = {
        "created": _now(),
        "from": "game" if live is not None else "setup",
        "game": game_info(game_dir),
        "sdk": r.obj(sdk),
        "session": session,
        "mods": r.obj(_mod_rows(mods, live)),
        "paks": r.obj(_paks(game_dir)),
        "health": r.obj(health),
        "errors": r.obj(errors),
        "logs": r.obj(logs),
        "crash": r.obj(crash),
    }
    return data, files


def build(game_dir: Path, out_dir: Path | None = None, live: dict | None = None) -> Path:
    """
    Writes a report zip into `out_dir` (default `sdk_mods/.bug_reports`, where the newest
    KEEP_REPORTS are kept) and returns its path. Blocks: call it from a worker thread.
    """
    game_dir = Path(game_dir)
    out = Path(out_dir) if out_dir is not None else default_dir(game_dir)
    out.mkdir(parents=True, exist_ok=True)
    data, files = collect(game_dir, live)
    summary = _summary(data)
    for info in data["logs"]:
        info.pop("blocks", None)  # in logs/*.errors.txt
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = out / f"{REPORT_PREFIX}{stamp}.zip"
    n = 1
    while target.exists():
        n += 1
        target = out / f"{REPORT_PREFIX}{stamp}-{n}.zip"
    tmp = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("summary.txt", summary)
            zf.writestr("report.json", json.dumps(data, indent=2, ensure_ascii=False, default=str))
            for name, text in sorted(files.items()):
                zf.writestr(name, text)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    _prune(out)
    return target


def _prune(out: Path) -> None:
    try:
        reports = sorted(out.glob(f"{REPORT_PREFIX}*.zip"), key=lambda p: _mtime(p) or 0, reverse=True)
    except OSError:
        return
    for old in reports[KEEP_REPORTS:]:
        try:
            old.unlink()
        except OSError:
            pass


# endregion
# region the GitHub issue


def issue_url(zip_path: Path, live: dict | None = None) -> str:
    """
    A new-issue page on GitHub with the bug form (.github/ISSUE_TEMPLATE/bug.yml) and the versions
    filled in from the report. Field names match the form's ids.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            data = json.loads(zf.read("report.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        data = {"mods": [], "game": {}, "sdk": {}, "errors": []}
    game, sdk = data.get("game") or {}, data.get("sdk") or {}
    session = data.get("session") or {}
    ours = [m for m in data.get("mods", []) if str(m.get("id", "")).startswith("bl4_")]
    others = [m for m in data.get("mods", []) if not str(m.get("id", "")).startswith("bl4_")]
    head = (
        f"Game build {game.get('buildid', '?')} ({game.get('store', '?')}), SDK {sdk.get('mod_manager', '?')}, "
        f"Python {sdk.get('python', '?')}"
        + (f", role {session['role']}, map {session['map']}" if session.get("role") else "")
    )
    mod_list = ", ".join(f"{m['id'][4:]} {m.get('version', '?')}" + ("" if m.get("enabled") is not False else " (off)") for m in ours)
    errored = list(dict.fromkeys(str(e.get("mod")) for e in data.get("errors", []) if e.get("mod")))
    params = {
        "template": "bug.yml",
        "title": "[Bug] " + (f"{errored[0]}: " if errored else ""),
        "versions": f"{head}\nMods: {mod_list}",
        "mods": ", ".join(errored[:6]),
        "others": ", ".join(f"{m['id']} {m.get('version', '')}".strip() for m in others),
        "report": f"{Path(zip_path).name} (attach it here)",
    }
    url = f"https://github.com/{REPO}/issues/new?" + urlencode({k: v for k, v in params.items() if v})
    while len(url) > URL_CAP and params["others"]:
        params["others"] = params["others"][: len(params["others"]) // 2].rsplit(",", 1)[0] + ", ..."
        if len(params["others"]) < 10:
            params["others"] = ""
        url = f"https://github.com/{REPO}/issues/new?" + urlencode({k: v for k, v in params.items() if v})
    if len(url) > URL_CAP:
        params["versions"] = params["versions"][: max(100, len(params["versions"]) - (len(url) - URL_CAP) // 3)] + "..."
        url = f"https://github.com/{REPO}/issues/new?" + urlencode({k: v for k, v in params.items() if v})
    return url


# endregion


if __name__ == "__main__":  # python report.py <game folder> <out folder>: a report from a shell
    print(build(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None))
