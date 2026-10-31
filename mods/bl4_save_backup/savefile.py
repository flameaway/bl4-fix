"""
Reading the game's .sav files: the key, decryption, and what a save says about itself.

Plain Python (3.11 and up), no SDK imports: the mod runs it on worker threads, and the offline
tests run it outside the game.

The format (checked on every file of a real profile, 2026-10-29): AES-256-ECB over
`zlib(text, level 6) + u32 LE length of text + PKCS7 padding`. The text is YAML. Re-encrypting the
decrypted text that way gives the original file byte for byte, so a file we write (the bank
restore, restore_helper.ps1) is in exactly the game's own format.

The key is 32 bytes in the game's exe with the Steam ID (u64, little-endian) XORed into its first
8 bytes (Kairosdex found it first: mods/bl4_weapondex/savegame.py). We don't carry it: we read it
from the player's own exe at the offset this build keeps it (KNOWN_OFFSET), or at the one a cache
file remembers (ours, or Kairosdex's: same format), or after a game patch moved it, by trying every
32-byte window of the exe's data sections until one opens profile.sav (seconds, once; the scan
sleeps every few ms so it never holds the GIL long).

YAML is read line by line (indent = nesting), only the few fields shown. No YAML library: the game
writes a simple, regular subset, and this stays fast and dependency free.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import struct
import threading
import time
import zlib
from ctypes import wintypes
from pathlib import Path
from typing import Any

KNOWN_OFFSET = 0xB981310  # the key's file offset in the 2026-10-22 build
SCAN_ALIGN = 16
SCAN_SLICE_S = 0.002  # the key scan works this long, then sleeps 1 ms (never sleep(0))
# Set by the mod's worker: called every few hundred lines of the YAML loops, sleeps 1 ms after ~1 ms
# of work, so a summary never holds the GIL for long (a save is ~3,000 lines, ~4 ms of Python)
PACE: Any = None
PACE_EVERY = 256


class SaveError(Exception):
    pass


# region Folders


def documents() -> Path:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL: Documents, wherever it is
    return Path(buf.value) if buf.value else Path.home() / "Documents"


def saved_dir() -> Path:
    return documents() / "My Games/Borderlands 4/Saved"


def accounts(saves_root: Path | None = None) -> list[tuple[int, Path]]:
    """(Steam ID, Profiles/client folder) for every account that has a profile, newest profile first."""
    root = saves_root or saved_dir() / "SaveGames"
    found = []
    try:
        for account in root.iterdir():
            folder = account / "Profiles/client"
            profile = folder / "profile.sav"
            if account.name.isdigit() and profile.is_file():
                found.append((profile.stat().st_mtime, int(account.name), folder))
    except OSError:
        return []
    found.sort(reverse=True)
    return [(sid, folder) for _, sid, folder in found]


def save_files(folder: Path) -> list[Path]:
    """profile.sav and the character saves (<n>.sav) in a Profiles/client folder."""
    try:
        return sorted(p for p in folder.glob("*.sav") if p.stem == "profile" or p.stem.isdigit())
    except OSError:
        return []


# endregion
# region AES (Windows' own, through bcrypt.dll)

_bcrypt = ctypes.WinDLL("bcrypt")
_bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.ULONG]
_bcrypt.BCryptSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
_bcrypt.BCryptGenerateSymmetricKey.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.ULONG, ctypes.c_char_p, wintypes.ULONG, wintypes.ULONG,
]
_crypt_args = [
    ctypes.c_void_p, ctypes.c_char_p, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG,
    ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG,
]
_bcrypt.BCryptDecrypt.argtypes = _crypt_args
_bcrypt.BCryptEncrypt.argtypes = _crypt_args
_bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
_alg = ctypes.c_void_p()
_alg_lock = threading.Lock()


def _aes() -> ctypes.c_void_p:
    with _alg_lock:
        if not _alg.value:
            if _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(_alg), "AES", None, 0):
                raise SaveError("Windows has no AES")
            mode = ctypes.create_unicode_buffer("ChainingModeECB")
            if _bcrypt.BCryptSetProperty(_alg, "ChainingMode", mode, ctypes.sizeof(mode), 0):
                raise SaveError("Windows has no AES-ECB")
    return _alg


def _aes_run(key: bytes, blob: bytes, *, encrypt: bool) -> bytes:
    blob = blob[: len(blob) // 16 * 16]
    handle = ctypes.c_void_p()
    if _bcrypt.BCryptGenerateSymmetricKey(_aes(), ctypes.byref(handle), None, 0, key, 32, 0):
        raise SaveError("bad key")
    try:
        out = ctypes.create_string_buffer(len(blob))
        got = wintypes.ULONG()
        run = _bcrypt.BCryptEncrypt if encrypt else _bcrypt.BCryptDecrypt
        if run(handle, blob, len(blob), None, None, 0, out, len(blob), ctypes.byref(got), 0):
            raise SaveError("AES failed")
        return out.raw
    finally:
        _bcrypt.BCryptDestroyKey(handle)


def decrypt(key: bytes, blob: bytes) -> str:
    """The save's YAML text. Raises SaveError unless the whole file checks out (a torn read fails)."""
    if not blob or len(blob) % 16:
        raise SaveError("not a whole save (size)")
    plain = _aes_run(key, blob, encrypt=False)
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise SaveError("not a whole save (padding)")
    inflate = zlib.decompressobj()
    try:
        text = inflate.decompress(plain[:-pad])  # zlib checks its own adler32 at the end
    except zlib.error as ex:
        raise SaveError(f"damaged ({ex})") from None
    if not inflate.eof or inflate.unused_data != struct.pack("<I", len(text)):
        raise SaveError("not a whole save (length)")
    return text.decode("utf-8", errors="replace")


def encrypt(key: bytes, text: str) -> bytes:
    """The game's own format (decrypt(encrypt(t)) == t; byte-identical to the game's files)."""
    raw = text.encode("utf-8")
    body = zlib.compress(raw, 6) + struct.pack("<I", len(raw))
    pad = 16 - len(body) % 16
    return _aes_run(key, body + bytes([pad]) * pad, encrypt=True)


def _opens(key: bytes, profile: bytes) -> bool:
    """The key opens the save: its start decompresses to text."""
    try:
        head = _aes_run(key, profile[:4096], encrypt=False)
    except SaveError:
        return False
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


class Keys:
    """Finds and remembers the save key per Steam ID (the exe part is shared)."""

    def __init__(self, exe: Path, cache: Path, also_read: tuple[Path, ...] = ()) -> None:
        self.exe = exe
        self.cache = cache
        self.also_read = also_read  # other mods' caches in the same format (Kairosdex)
        self.base: bytes | None = None
        self.lock = threading.Lock()

    def key(self, steam_id: int, profile: bytes) -> bytes | None:
        with self.lock:
            if self.base is not None:
                key = _with_id(self.base, steam_id)
                if _opens(key, profile):
                    return key
            base = self._find(steam_id, profile)
            if base is None:
                return None
            self.base = base
            return _with_id(base, steam_id)

    def _find(self, steam_id: int, profile: bytes) -> bytes | None:
        st = self.exe.stat()
        ident = [st.st_size, st.st_mtime_ns]
        offsets = []
        for path in (self.cache, *self.also_read):
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if cached.get("exe") == ident and isinstance(cached.get("offset"), int):
                offsets.append(cached["offset"])
        offsets.append(KNOWN_OFFSET)
        with self.exe.open("rb") as f:
            for offset in offsets:
                f.seek(offset)
                base = f.read(32)
                if len(base) == 32 and _opens(_with_id(base, steam_id), profile):
                    self._remember(ident, offset)
                    return base
        offset = _scan(self.exe, steam_id, profile)
        if offset is None:
            return None
        self._remember(ident, offset)
        with self.exe.open("rb") as f:
            f.seek(offset)
            return f.read(32)

    def _remember(self, ident: list[int], offset: int) -> None:
        try:
            write_json(self.cache, {"exe": ident, "offset": offset})
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
            slice_end = time.perf_counter() + SCAN_SLICE_S
            for at in range(0, len(blob) - 32, SCAN_ALIGN):
                if not at & 0x3FF and time.perf_counter() > slice_end:
                    time.sleep(0.001)  # let the game thread have the GIL
                    slice_end = time.perf_counter() + SCAN_SLICE_S
                base = blob[at : at + 32]
                if base.count(0) > 8 or len(set(base)) < 16:
                    continue  # too regular to be a key
                key = _with_id(base, steam_id)
                start = _aes_run(key, first, encrypt=False)
                if start[:1] == b"\x78" and ((0x78 << 8) | start[1]) % 31 == 0 and _opens(key, profile):
                    return ptr + at
    return None


# endregion
# region What a save says


def _entries(text: str) -> list[tuple[int, int, str, str]]:
    """
    (line number, indent, key, value) for every keyed line. The game writes list items at their
    parent's indent (`experience:` then `- type: Character` under it, `  level: 52` below), so a
    `- ` item becomes a "-" key one deeper than its dash, and its inline key one deeper still.
    """
    out = []
    pace = PACE
    for number, line in enumerate(text.split("\n")):
        if pace is not None and not number % PACE_EVERY:
            pace()
        stripped = line.lstrip(" ")
        if not stripped:
            continue
        indent = len(line) - len(stripped)
        if stripped.startswith("- ") or stripped == "-":
            rest = stripped[2:].strip()
            key, sep, value = rest.partition(": ") if not rest.startswith(("'", '"')) else ("", "", "")
            if sep or rest.endswith(":") and not rest.startswith(("'", '"')):
                if not sep:
                    key, value = rest[:-1], ""
                out.append((number, indent + 1, "-", ""))
                out.append((number, indent + 2, key.strip(), value.strip()))
            else:
                out.append((number, indent + 1, "-", rest))
            continue
        key, sep, value = stripped.partition(":")
        if sep:
            out.append((number, indent, key.strip(), value.strip()))
    return out


def _walk(text: str) -> list[tuple[tuple[str, ...], str]]:
    """(path of keys, value) for every keyed line; list items are "-" in the path."""
    out = []
    stack: list[tuple[int, str]] = []
    pace = PACE
    for number, indent, key, value in _entries(text):
        if pace is not None and not number % PACE_EVERY:
            pace()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        out.append((tuple(k for _, k in stack), value))
    return out


def _num(value: str) -> float | None:
    try:
        return float(value.strip("'\""))
    except ValueError:
        return None


def _serial_hash(serial: str) -> str:
    return hashlib.sha1(serial.strip("'\"").encode()).hexdigest()[:10]


def summarize(text: str) -> dict[str, Any]:
    """What the page shows about a save. Character saves have "guid"; profile.sav has "bank"."""
    if text.startswith("state:") or "\nstate:" in text[:200]:
        return _character(text)
    return _profile(text)


def _character(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {"kind": "character"}
    items: list[str] = []
    lost = equipped = 0
    exp_type = None
    for path, value in _walk(text):
        top = path[0]
        if top == "state":
            tail = path[1:]
            if tail == ("char_guid",):
                info["guid"] = value.strip("'\"").lower()
            elif tail == ("char_name",):
                info["name"] = value.strip("'\"")
            elif tail == ("class",):
                info["class"] = value
            elif tail == ("player_difficulty",):
                info["difficulty"] = value
            elif tail == ("experience", "-"):
                exp_type = None
            elif tail == ("experience", "-", "type"):
                exp_type = value
            elif tail == ("experience", "-", "level") and exp_type in ("Character", "Specialization"):
                info["level" if exp_type == "Character" else "spec"] = int(_num(value) or 0)
            elif tail == ("total_playtime",):
                info["playtime"] = _num(value) or 0.0
            elif tail == ("last_played_timestamp",):
                info["last_played"] = int(_num(value) or 0)
            elif tail == ("currencies", "cash"):
                info["cash"] = int(_num(value) or 0)
            elif tail == ("currencies", "eridium"):
                info["eridium"] = int(_num(value) or 0)
            elif len(tail) == 5 and tail[:3] == ("inventory", "items", "backpack") and tail[4] == "serial":
                # Equipped gear is in the backpack too (flags 1), and listed again under equipped
                items.append(_serial_hash(value))
            elif len(tail) == 6 and tail[:3] == ("inventory", "equipped_inventory", "equipped") and tail[5] == "serial":
                equipped += 1
            elif tail == ("lostloot", "items", "-", "serial"):
                lost += 1
        elif top == "globals":
            if path[1:] == ("vault_hunter_level",):
                info["uvh"] = int(_num(value) or 0)
            elif path[1:] == ("mainmissioncomplete",):
                info["story_done"] = value.upper() == "TRUE"
        elif top == "save_game_header" and path[1:] == ("compatible_changelist",):
            info["build"] = int(_num(value) or 0)
    info["lost_loot"] = lost
    info["backpack"] = len(items)
    info["equipped"] = equipped
    info["items"] = sorted(items)
    return info


# The shared bank in profile.sav
BANK_PATH = ("domains", "local", "shared", "inventory", "items", "bank")


def _profile(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {"kind": "profile"}
    bank: list[str] = []
    for path, value in _walk(text):
        if path[: len(BANK_PATH)] == BANK_PATH and path[-1] == "serial" and len(path) == len(BANK_PATH) + 2:
            bank.append(_serial_hash(value))
        elif path == ("save_game_header", "compatible_changelist"):
            info["build"] = int(_num(value) or 0)
        elif path[:5] == ("domains", "local", "characters_selected"):
            info["selected"] = value
    info["bank"] = sorted(bank)
    block = bank_block(text)
    info["bank_sig"] = hashlib.sha1(block.encode()).hexdigest()[:16] if block is not None else ""
    return info


# endregion
# region The bank block


def _bank_span(text: str) -> tuple[int, int] | None:
    """[start, end) line numbers of the `bank:` key and everything under it, or None."""
    stack: list[tuple[int, str]] = []
    start = None
    start_indent = 0
    for number, indent, key, _ in _entries(text):
        if start is not None:
            if indent <= start_indent:
                return start, number
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if tuple(k for _, k in stack) == BANK_PATH:
            start, start_indent = number, indent
    if start is not None:
        return start, len(text.split("\n"))
    return None


def bank_block(text: str) -> str | None:
    """The bank's lines (its `bank:` line and everything under it), or None if the profile has none."""
    lines = text.split("\n")
    span = _bank_span(text)
    if span is None:
        return None
    return "\n".join(lines[span[0] : span[1]])


def with_bank(text: str, block: str) -> str:
    """text with its bank replaced by block (from bank_block). The same splice restore_helper.ps1 does."""
    lines = text.split("\n")
    span = _bank_span(text)
    if span is None:
        raise SaveError("no bank in this profile")
    new = block.split("\n")
    if _indent(new[0]) != _indent(lines[span[0]]):
        raise SaveError("the bank sits at another depth in this profile")
    return "\n".join(lines[: span[0]] + new + lines[span[1] :])


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


# endregion


def write_json(path: Path, data: Any) -> None:
    """temp file + rename: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)
