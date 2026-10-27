"""
Bosses the game's own save shows you've beaten, so the dex isn't empty for everything beaten
before the mod was installed. Read only: we never write a save.

The game's .sav files (Documents/My Games/Borderlands 4/Saved/SaveGames/<Steam ID>/Profiles/client:
profile.sav plus one <n>.sav per character) are AES-256-ECB encrypted, then zlib, then YAML text.
The key is 32 bytes in the game's exe with the Steam ID (u64, little-endian) XORed into its first
8 bytes. We don't carry the key: we read it from the player's own exe, at the offset this build
keeps it (KNOWN_OFFSET), or, after a game patch moves it, by trying every 32-byte window of the
exe's data sections on profile.sav until one decrypts it (a few seconds, once: the offset found is
remembered in settings/weapondex_savekey.json with the exe's size and date).

The dex is account-wide, like the game's shared progress: a boss counts once any character on
the profile has beaten it. What counts as beaten (missions.json, made by
tools/gen_weapondex_missions.py, says which missions and unlockables are which boss's):
- account-wide unlockables in profile.sav (`echo_upgrade_challenges.Activity_DrillSites_...`,
  `sharedprogress_cowbell...`): finished activities, recorded for every character;
- missions with `status: completed` in any character's save (<n>.sav): story and side missions,
  and activities.
Not the Big Encore machines in profile.sav's discovery list (`gbx_discovery_pg_shared`): those
are only places seen (a whole DLC base was "discovered" without its boss beaten).
"Per-character Kairosdex" uses only the character's own finished missions (beaten(char): each
save's `char_guid` is PlayerState.ActiveCharGuid), not the profile's shared activities.

Runs on a worker thread; the game thread only reads the result (beaten()) and calls tick(), which
starts a new read when a save file changes (the game saves often).
"""

from __future__ import annotations

import ctypes
import json
import re
import struct
import threading
import time
import zlib
from ctypes import wintypes
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR
from unrealsdk import logging

from . import owned


KNOWN_OFFSET = 0xB981310  # the key's file offset in the 2026-10-22 build
KEY_CACHE = SETTINGS_DIR / "weapondex_savekey.json"
CHECK_EVERY_S = 20.0
SCAN_ALIGN = 16

_LIST_ITEM = re.compile(r"^\s*- ([\w.]+)\s*$", re.MULTILINE)
_KEY_LINE = re.compile(r"^( *)(\w+): *(.*?)\s*$")
_CHAR_GUID = re.compile(r"^\s*char_guid:\s*'?([0-9A-Fa-f]{32})'?\s*$", re.MULTILINE)


class S:
    beaten: frozenset[str] = frozenset()  # boss ids, from the last read
    # Per character id (progress.character()'s form): its own finished missions' bosses
    by_char: dict[str, frozenset[str]] = {}  # noqa: RUF012 - replaced whole by the worker
    stamp: Any = None  # the save files' names, sizes and dates when last read
    checked_at = 0.0
    worker: threading.Thread | None = None
    changed = False  # set by the worker: the dex page needs an update


def beaten(char: str | None = None) -> frozenset[str]:
    """Boss ids the game's saves show some character on the profile (or `char` itself) has beaten."""
    return S.beaten if char is None else S.by_char.get(char, frozenset())


def tick(on_change: Any) -> None:
    """Call every frame from the game thread."""
    if S.changed:
        S.changed = False
        on_change()
    now = time.monotonic()
    if now - S.checked_at < CHECK_EVERY_S or (S.worker is not None and S.worker.is_alive()):
        return
    S.checked_at = now
    folder = _save_folder()
    if folder is None:
        return
    stamp = _stamp(folder)
    if stamp == S.stamp:
        return  # unchanged since the last read (or the last failure)
    S.worker = threading.Thread(target=_read, args=(folder, stamp), name="weapondex save", daemon=True)
    S.worker.start()


def _read(folder: Path, stamp: Any) -> None:
    try:
        steam_id = int(folder.parent.parent.name)
        profile = (folder / "profile.sav").read_bytes()
        key = _key(steam_id, profile)
        if key is None:
            raise RuntimeError("couldn't find the save key in the game's exe")
        shared = _decrypt(key, profile)
        found = _unlockables_beaten(shared)
        held = owned.from_save_text(shared)  # the shared bank
        by_char: dict[str, frozenset[str]] = {}
        carried: list[tuple[str, list[str], float]] = []
        for path in folder.glob("*.sav"):
            if path.stem.isdigit():  # a character
                saved_at = path.stat().st_mtime
                text = _decrypt(key, path.read_bytes())
                missions = _missions_beaten(text)
                found |= missions
                serials = owned.from_save_text(text)  # backpack and equipped (not lost loot)
                held += serials
                if guid := _CHAR_GUID.search(text):
                    char = guid.group(1).lower()
                    by_char[char] = frozenset(missions)
                    carried.append((char, serials, saved_at))
        if owned.rebuild(held):
            S.changed = True
        if owned.add_serials(held):
            S.changed = True
        for char, serials, saved_at in carried:
            if owned.add_from_save(char, serials, saved_at):
                S.changed = True
        if by_char != S.by_char:
            S.by_char = by_char
            S.changed = True
        if found != S.beaten:
            S.beaten = frozenset(found)
            S.changed = True
            logging.info(f"[Kairosdex] From your save: {len(found)} bosses beaten")
    except Exception as ex:  # noqa: BLE001 - the dex still works without it
        logging.error(f"[Kairosdex] Couldn't read the game's save: {ex}")
    S.stamp = stamp


@cache
def _links() -> dict[str, dict[str, list[str]]]:
    return json.loads((resources.files(__package__) / "missions.json").read_text(encoding="utf-8"))


def _unlockables_beaten(text: str) -> set[str]:
    have = {item.lower() for item in _LIST_ITEM.findall(text) if "." in item}
    return {boss for boss, link in _links().items() if have.intersection(link["unlockables"])}


def _missions_beaten(text: str) -> set[str]:
    """Missions with `status: completed` in a character save's missions section:

        missions:
          local_sets:
            missionset_main_beach:
              missions:
                mission_main_beach:
                  ui_flags: 1
                  status: completed
    """
    done = set()
    parents: list[tuple[int, str]] = []  # (indent, key) of the keys we're inside
    inside = False
    for line in text.splitlines():
        found = _KEY_LINE.match(line)
        if found is None:
            continue
        indent, key, value = len(found.group(1)), found.group(2), found.group(3)
        if indent == 0:
            inside = key == "missions"
            parents = []
            continue
        if not inside:
            continue
        while parents and parents[-1][0] >= indent:
            parents.pop()
        if key == "status" and value == "completed" and parents:
            done.add(parents[-1][1].lower())
        parents.append((indent, key))
    return {boss for boss, link in _links().items() if done.intersection(link["missions"])}


# region Files


def _documents() -> Path:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL: Documents, wherever it is
    return Path(buf.value) if buf.value else Path.home() / "Documents"


def _save_folder() -> Path | None:
    """Profiles/client of the Steam account that saved most recently."""
    root = _documents() / "My Games/Borderlands 4/Saved/SaveGames"
    best = None
    try:
        for account in root.iterdir():
            profile = account / "Profiles/client/profile.sav"
            if account.name.isdigit() and profile.is_file():
                mtime = profile.stat().st_mtime
                if best is None or mtime > best[0]:
                    best = (mtime, profile.parent)
    except OSError:
        return None
    return best[1] if best else None


def _stamp(folder: Path) -> Any:
    try:
        return tuple(sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.glob("*.sav")))
    except OSError:
        return None


# endregion
# region Decryption (Windows' own AES, through bcrypt.dll)

_bcrypt = ctypes.WinDLL("bcrypt")
_alg = ctypes.c_void_p()
_alg_lock = threading.Lock()


def _aes() -> ctypes.c_void_p:
    with _alg_lock:
        if not _alg.value:
            if _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(_alg), ctypes.c_wchar_p("AES"), None, 0):
                raise OSError("no AES")
            mode = ctypes.create_unicode_buffer("ChainingModeECB")
            if _bcrypt.BCryptSetProperty(_alg, ctypes.c_wchar_p("ChainingMode"), mode, ctypes.sizeof(mode), 0):
                raise OSError("no AES-ECB")
    return _alg


def _aes_decrypt(key: bytes, blob: bytes) -> bytes:
    blob = blob[: len(blob) // 16 * 16]
    handle = ctypes.c_void_p()
    if _bcrypt.BCryptGenerateSymmetricKey(_aes(), ctypes.byref(handle), None, 0, key, 32, 0):
        return b""
    try:
        out = ctypes.create_string_buffer(len(blob))
        got = wintypes.ULONG()
        if _bcrypt.BCryptDecrypt(handle, blob, len(blob), None, None, 0, out, len(blob), ctypes.byref(got), 0):
            return b""
        return out.raw
    finally:
        _bcrypt.BCryptDestroyKey(handle)


def _decrypt(key: bytes, blob: bytes) -> str:
    return zlib.decompressobj().decompress(_aes_decrypt(key, blob)).decode("utf-8", errors="replace")


def _works(key: bytes, profile: bytes) -> bool:
    """The key opens the save: its start decompresses to text."""
    head = _aes_decrypt(key, profile[:4096])
    if len(head) < 2 or head[0] != 0x78:
        return False
    try:
        text = zlib.decompressobj().decompress(head)[:64]
    except zlib.error:
        return False
    return len(text) > 0 and all(32 <= c < 127 or c in (9, 10, 13) for c in text)


def _with_id(base: bytes, steam_id: int) -> bytes:
    sid = struct.pack("<Q", steam_id)
    return bytes(a ^ b for a, b in zip(base[:8], sid, strict=True)) + base[8:]


# endregion
# region Key


def _exe() -> Path:
    return SETTINGS_DIR.parent.parent / "OakGame/Binaries/Win64/Borderlands4.exe"


def _key(steam_id: int, profile: bytes) -> bytes | None:
    exe = _exe()
    st = exe.stat()
    ident = [st.st_size, st.st_mtime_ns]
    try:
        cached = json.loads(KEY_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    offsets = [cached["offset"]] if cached.get("exe") == ident and isinstance(cached.get("offset"), int) else []
    offsets.append(KNOWN_OFFSET)
    with exe.open("rb") as f:
        for offset in offsets:
            f.seek(offset)
            key = _with_id(f.read(32), steam_id)
            if _works(key, profile):
                _remember(ident, offset)
                return key
    logging.info("[Kairosdex] Looking for the save key in the game's exe (once after a game patch, a few seconds)")
    offset = _scan(exe, steam_id, profile)
    if offset is None:
        return None
    _remember(ident, offset)
    with exe.open("rb") as f:
        f.seek(offset)
        return _with_id(f.read(32), steam_id)


def _remember(ident: list[int], offset: int) -> None:
    try:
        KEY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = KEY_CACHE.with_name(KEY_CACHE.name + ".tmp")
        tmp.write_text(json.dumps({"exe": ident, "offset": offset}), encoding="utf-8")
        tmp.replace(KEY_CACHE)
    except OSError:
        pass


def _scan(exe: Path, steam_id: int, profile: bytes) -> int | None:
    """Every aligned 32-byte window of the exe's data sections, tried as the key."""
    first = profile[:16]
    with exe.open("rb") as f:
        head = f.read(4096)
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        count = struct.unpack_from("<H", head, pe + 6)[0]
        table = pe + 24 + struct.unpack_from("<H", head, pe + 20)[0]
        sections = []
        for i in range(count):
            size, ptr = struct.unpack_from("<II", head, table + i * 40 + 16)
            flags = struct.unpack_from("<I", head, table + i * 40 + 36)[0]
            if size and not flags & 0x20000000:  # not code
                sections.append((ptr, size))
        for ptr, size in sections:
            f.seek(ptr)
            blob = f.read(size)
            for at in range(0, len(blob) - 32, SCAN_ALIGN):
                base = blob[at : at + 32]
                if base.count(0) > 8 or len(set(base)) < 16:
                    continue  # too regular to be a key
                key = _with_id(base, steam_id)
                start = _aes_decrypt(key, first)
                if start[:1] == b"\x78" and ((0x78 << 8) | start[1]) % 31 == 0 and _works(key, profile):
                    return ptr + at
    return None


# endregion
