"""
Reads and writes Unreal .pak files (format version 11, what BL4 uses).

BL4 mounts any pak in `OakGame/Content/Paks/~mods/` at startup, and a file in a `_P` pak replaces
the game's copy of that path. We only write paks holding our own few UI files, so the writer has
no compression or encryption support. The reader only looks at indexes and entry headers: enough
to tell which pak supplies a file and the SHA1 Unreal stored for it, without decompressing.

Layout: [entry header + data]... [primary index] [path hash index] [full directory index] [footer]

A copy of mods/bl4_respec/pak.py, so each mod works on its own. Keep them in sync. This mod only
uses the reader (winning_entry), to check the game data parts.json was made from.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PAK_MAGIC = 0x5A6F12E1
PAK_VERSION = 11
COMPRESSION_SLOTS = 5
COMPRESSION_NAME_LEN = 32

# Encoded entry flags: offset / uncompressed size / size each fit in 32 bits
_FITS_U32 = (1 << 31) | (1 << 30) | (1 << 29)


def _fstring(text: str) -> bytes:
    if text == "":
        return struct.pack("<i", 0)
    if text.isascii():
        raw = text.encode("ascii") + b"\0"
        return struct.pack("<i", len(raw)) + raw
    raw = text.encode("utf-16-le") + b"\0\0"
    return struct.pack("<i", -(len(raw) // 2)) + raw


def path_hash(path: str, seed: int) -> int:
    """FNV-1a 64 over the lowercased UTF-16LE path, offset by the pak's seed (v11 'Fnv64BugFix')."""
    value = (0xCBF29CE484222325 + seed) & 0xFFFFFFFFFFFFFFFF
    for byte in path.lower().encode("utf-16-le"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def build_pak(mount_point: str, files: dict[str, bytes], pak_name: str) -> bytes:
    """
    Builds a pak containing `files` (paths relative to `mount_point`, '/'-separated).

    `mount_point` is relative to the game's Engine folder, e.g. '../../../OakGame/Content/uiresources/'.
    `pak_name` seeds the path hashes the same way Unreal does (CRC32 of the lowercased file name).
    """
    seed = zlib.crc32(pak_name.lower().encode("utf-16-le"))
    out = bytearray()
    encoded = bytearray()
    entry_locations: dict[str, int] = {}

    for path, data in sorted(files.items()):
        offset = len(out)
        if offset > 0xFFFFFFFF or len(data) > 0xFFFFFFFF:
            raise ValueError("pak too large for this writer")
        # Full entry header in front of the data; its own Offset field is always written as 0
        out += struct.pack("<qqqI", 0, len(data), len(data), 0)
        out += hashlib.sha1(data).digest()
        out += struct.pack("<BI", 0, 0)
        out += data
        entry_locations[path] = len(encoded)
        encoded += struct.pack("<III", _FITS_U32, offset, len(data))

    # Directory index: every directory down to the root appears, with trailing slashes
    directories: dict[str, list[tuple[str, int]]] = {"/": []}
    for path, location in entry_locations.items():
        parent = PurePosixPath(path).parent
        dir_key = "/" if str(parent) == "." else f"{parent}/"
        directories.setdefault(dir_key, []).append((PurePosixPath(path).name, location))
        while str(parent) != ".":
            directories.setdefault(f"{parent}/", [])
            parent = parent.parent

    full_dir_index = bytearray(struct.pack("<I", len(directories)))
    for dir_name, dir_files in directories.items():
        full_dir_index += _fstring(dir_name) + struct.pack("<I", len(dir_files))
        for file_name, location in dir_files:
            full_dir_index += _fstring(file_name) + struct.pack("<i", location)

    path_hash_index = bytearray(struct.pack("<I", len(entry_locations)))
    for path, location in entry_locations.items():
        path_hash_index += struct.pack("<Qi", path_hash(path, seed), location)
    path_hash_index += struct.pack("<I", 0)

    # The primary index refers to the two secondary indexes by absolute offset, so size it first
    index_offset = len(out)

    def primary(phi_offset: int, fdi_offset: int) -> bytes:
        return (
            _fstring(mount_point)
            + struct.pack("<IQ", len(entry_locations), seed)
            + struct.pack("<IQQ", 1, phi_offset, len(path_hash_index))
            + hashlib.sha1(path_hash_index).digest()
            + struct.pack("<IQQ", 1, fdi_offset, len(full_dir_index))
            + hashlib.sha1(full_dir_index).digest()
            + struct.pack("<I", len(encoded))
            + encoded
            + struct.pack("<I", 0)
        )

    index_size = len(primary(0, 0))
    phi_offset = index_offset + index_size
    fdi_offset = phi_offset + len(path_hash_index)
    index = primary(phi_offset, fdi_offset)

    out += index + path_hash_index + full_dir_index
    out += bytes(16) + b"\0"  # encryption key guid, index not encrypted
    out += struct.pack("<IIQQ", PAK_MAGIC, PAK_VERSION, index_offset, len(index))
    out += hashlib.sha1(index).digest()
    out += bytes(COMPRESSION_SLOTS * COMPRESSION_NAME_LEN)
    return bytes(out)


# region Reading

FOOTER_SIZE = 16 + 1 + 4 + 4 + 8 + 8 + 20 + COMPRESSION_SLOTS * COMPRESSION_NAME_LEN
DELETED_ENTRY = -0x80000000  # a patch pak's way of saying "this file no longer exists"
ENGINE_RELATIVE = "../../../"
_PATCH_NUMBER = re.compile(r"_(\d+)_P\.pak$", re.IGNORECASE)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, fmt: str) -> tuple[int, ...]:
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return values

    def skip(self, count: int) -> None:
        self.pos += count

    def fstring(self) -> str:
        (length,) = self.take("<i")
        if length == 0:
            return ""
        if length < 0:
            raw = self.data[self.pos : self.pos - length * 2]
            self.pos -= length * 2
            return raw.decode("utf-16-le").rstrip("\0")
        raw = self.data[self.pos : self.pos + length]
        self.pos += length
        return raw.decode("latin-1").rstrip("\0")


@dataclass
class PakEntryInfo:
    pak: Path
    deleted: bool
    # SHA1 of the stored (possibly compressed) bytes, as recorded by Unreal; empty if deleted
    sha1: str
    # Where the entry's full header (followed by its data) starts in the pak; -1 if deleted
    offset: int = -1


def _entry_offset(encoded: bytes, location: int) -> int:
    (flags,) = struct.unpack_from("<I", encoded, location)
    pos = location + 4
    if flags & 0x3F == 0x3F:  # block size didn't fit the flag bits, stored separately
        pos += 4
    if flags & (1 << 31):
        (offset,) = struct.unpack_from("<I", encoded, pos)
    else:
        (offset,) = struct.unpack_from("<Q", encoded, pos)
    return offset


@dataclass
class _Summary:
    """What a lookup needs from one pak, kept so checking many paths doesn't reread every pak."""

    stamp: tuple[int, int]  # (size, mtime_ns) it was read at
    seed: int = 0
    prefix: str = ""  # mount point relative to the game folder, lowercased
    # The path hash index: count, then (u64 hash, i32 location) pairs; empty for a pak we can't read
    hashes: bytes = b""


# Opening and reading 360 game paks takes most of a second, so a check of many paths in a row
# (e.g. every source file of a generated data file) reads each pak once. The summaries are about
# 15 MB, so they're dropped once nothing has asked for a while.
_summaries: dict[str, _Summary] = {}  # by lowercased path
_FORGET_AFTER = 10.0
_forget_timer: threading.Timer | None = None


def _forget_soon() -> None:
    global _forget_timer
    if _forget_timer is not None:
        _forget_timer.cancel()
    # Only clears a dict, so it's fine on its own thread
    _forget_timer = threading.Timer(_FORGET_AFTER, _summaries.clear)
    _forget_timer.daemon = True
    _forget_timer.start()


def _stamps(paks: list[Path]) -> dict[str, tuple[int, int]]:
    """
    (size, mtime_ns) of each pak by its lowercased path, from one directory listing per folder
    (much cheaper than a stat per pak).
    """
    folders = {str(pak.parent) for pak in paks}
    stamps: dict[str, tuple[int, int]] = {}
    for folder in folders:
        try:
            with os.scandir(folder) as listing:
                for item in listing:
                    info = item.stat()
                    stamps[item.path.lower()] = (info.st_size, info.st_mtime_ns)
        except OSError:
            continue
    return stamps


def _summarize(pak: Path, stamp: tuple[int, int]) -> _Summary:
    with pak.open("rb") as file:
        size = file.seek(0, 2)
        file.seek(size - FOOTER_SIZE)
        footer = file.read(FOOTER_SIZE)
        magic, version, index_offset, index_size = struct.unpack_from("<IIQQ", footer, 17)
        if magic != PAK_MAGIC or version != PAK_VERSION or footer[16]:
            return _Summary(stamp)  # unknown format or encrypted index
        file.seek(index_offset)
        # The path hash index location comes right after the mount point, count and seed
        head = _Reader(file.read(min(index_size, 4096)))
        mount = head.fstring()
        head.take("<I")
        (seed,) = head.take("<Q")
        (has_path_hash_index,) = head.take("<I")
        if not has_path_hash_index:
            return _Summary(stamp)
        phi_offset, phi_size = head.take("<QQ")
        file.seek(phi_offset)
        hashes = file.read(phi_size)
    return _Summary(stamp, seed, mount.removeprefix(ENGINE_RELATIVE).lower(), hashes)


def _location(summary: _Summary, game_path: str) -> int | None:
    """The path hash index's location for `game_path`, or None if the pak doesn't list it."""
    if not summary.hashes or not game_path.lower().startswith(summary.prefix):
        return None
    wanted = struct.pack("<Q", path_hash(game_path[len(summary.prefix) :], summary.seed))
    (count,) = struct.unpack_from("<I", summary.hashes)
    end = 4 + count * 12
    at = summary.hashes.find(wanted, 4, end)
    while at >= 0:
        if (at - 4) % 12 == 0:
            return struct.unpack_from("<i", summary.hashes, at + 8)[0]
        at = summary.hashes.find(wanted, at + 1, end)
    return None


def find_entry(pak: Path, game_path: str) -> PakEntryInfo | None:
    """
    Looks up `game_path` (e.g. 'OakGame/Content/uiresources/x.html') in one pak's path hash index.

    Returns None if the pak doesn't mention the file at all.
    """
    with pak.open("rb") as file:
        size = file.seek(0, 2)
        file.seek(size - FOOTER_SIZE)
        footer = file.read(FOOTER_SIZE)
        magic, version, index_offset, index_size = struct.unpack_from("<IIQQ", footer, 17)
        if magic != PAK_MAGIC or version != PAK_VERSION or footer[16]:
            return None  # unknown format or encrypted index

        file.seek(index_offset)
        index = _Reader(file.read(index_size))
        mount = index.fstring()
        index.take("<I")
        (seed,) = index.take("<Q")
        (has_path_hash_index,) = index.take("<I")
        if not has_path_hash_index:
            return None
        phi_offset, phi_size = index.take("<QQ")
        index.skip(20)
        (has_full_dir_index,) = index.take("<I")
        if has_full_dir_index:
            index.skip(8 + 8 + 20)
        (encoded_size,) = index.take("<I")
        encoded = index.data[index.pos : index.pos + encoded_size]
        index.skip(encoded_size)

        prefix = mount.removeprefix(ENGINE_RELATIVE)
        if not game_path.lower().startswith(prefix.lower()):
            return None
        wanted = path_hash(game_path[len(prefix) :], seed)

        file.seek(phi_offset)
        phi = _Reader(file.read(phi_size))
        (count,) = phi.take("<I")
        location = None
        for _ in range(count):
            entry_hash, entry_location = phi.take("<Qi")
            if entry_hash == wanted:
                location = entry_location
                break
        if location is None:
            return None
        if location == DELETED_ENTRY:
            return PakEntryInfo(pak, deleted=True, sha1="")

        if location < 0:
            # Entries that couldn't be bit-packed are stored in full after the encoded ones
            (legacy_count,) = index.take("<I")
            for legacy_index in range(legacy_count):
                offset, _size, _usize, method = index.take("<QQQI")
                sha1 = index.data[index.pos : index.pos + 20].hex()
                index.skip(20)
                if method:
                    (blocks,) = index.take("<I")
                    index.skip(blocks * 16)
                index.skip(1 + 4)
                if legacy_index == -location - 1:
                    return PakEntryInfo(pak, deleted=False, sha1=sha1, offset=offset)
            return None

        # The full entry header sits in front of the data: offset, size, usize, method, then SHA1
        offset = _entry_offset(encoded, location)
        file.seek(offset + 8 + 8 + 8 + 4)
        return PakEntryInfo(pak, deleted=False, sha1=file.read(20).hex(), offset=offset)


def patch_number(pak: Path) -> int:
    match = _PATCH_NUMBER.search(pak.name)
    return int(match.group(1)) if match else -1


def winning_entry(paks: list[Path], game_path: str) -> PakEntryInfo | None:
    """Of the given paks, the entry the game ends up using for `game_path` (highest patch wins)."""
    best: PakEntryInfo | None = None
    stamps = _stamps(paks)
    for pak in sorted(paks, key=patch_number):
        key = str(pak).lower()
        if key not in stamps:
            continue
        try:
            summary = _summaries.get(key)
            if summary is None or summary.stamp != stamps[key]:
                summary = _summaries[key] = _summarize(pak, stamps[key])
            if _location(summary, game_path) is None:
                continue
            # Rare (a pak that has the file): read its entry in full
            entry = find_entry(pak, game_path)
        except (OSError, struct.error, UnicodeDecodeError):
            continue
        if entry is not None:
            best = entry
    _forget_soon()
    if best is not None and best.deleted:
        return None
    return best


# endregion
