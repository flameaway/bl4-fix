"""
Disk Space: makes a Borderlands 4 install smaller. It lives on the setup app's own Disk Space page
(and `--disk-scan` / `--disk-free` on the command line) and is never part of Install. The idea and
both methods come from nanafy/bl4-slim (MIT); this is our own implementation.

1. Voice languages. Steam installs all 8 (~9 GB each). A voice chunk is a pak chunk whose .pak only
   holds `WwiseAudio/<language>/...` and whose IoStore container (.utoc/.ucas) holds nothing but its
   own header. Deleting every file of a language's chunks removes it.
2. Old patch data. Each patch adds `pakchunkN-Windows_<patch>_P` files, and the game reads a pak
   file (by path) or an IoStore chunk (by chunk id) from the highest patch that has it. The older
   copies stay on disk inside files the game still reads other data from. Their byte ranges are
   deallocated in place (NTFS sparse file + FSCTL_SET_ZERO_DATA): the file keeps its size and every
   byte the game reads; the dead ranges read back as zeros and take no disk space.

Only the top level of Paks/ is read: mod paks (`~mods`) are never touched, and leaving them out can
only keep more. Anything unexpected (an index format we don't know, a dead range that overlaps live
data, a file changed since the scan) stops before a byte changes. Undo: Steam > Properties >
Installed Files > Verify integrity of game files. A game update brings old copies back: run again.
"""

from __future__ import annotations

import bisect
import ctypes
import hashlib
import msvcrt
import os
import re
import struct
import tempfile
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

PAKS_REL = Path("OakGame/Content/Paks")
STEAM_APP_ID = 1285190

# Wwise's names for the voice languages, as the voice chunks' folders spell them
LANGUAGES = (
    "English(US)", "French(France)", "German", "Italian", "Spanish(Spain)", "Japanese", "Korean", "Chinese(Simplified)",
)
# Windows UI language (primary LANGID) -> the matching voice language, to suggest keeping it
_WINDOWS_LANGUAGE = {0x09: "English(US)", 0x0C: "French(France)", 0x07: "German", 0x10: "Italian",
                     0x0A: "Spanish(Spain)", 0x11: "Japanese", 0x12: "Korean", 0x04: "Chinese(Simplified)"}

_CHUNK_FILE = re.compile(r"^pakchunk(\d+)-Windows_(\d+)_P\.(pak|ucas|utoc)$", re.IGNORECASE)
_TOC_MAGIC = b"-==--==--==--==-"
_TOC_HEADER_CHUNK = 6  # EIoChunkType ContainerHeader: each container reads its own
_PAK_MAGIC = 0x5A6F12E1
_PAK_FOOTER = 221
_NO_ENTRY = -(1 << 31)  # a directory index location for a deleted file
_PROBE_NAME = "bl4fix-sparse-probe.tmp"
# Kept bytes on each side of a blanked range that are hashed before and after it is blanked
_CHECK_MARGIN = 1 << 16

Log = Callable[[str], None]


class SlimError(Exception):
    pass


def _bad(path: Path, why: str) -> SlimError:
    return SlimError(f"{path.name}: {why}")


# region Reading the indexes


@dataclass
class _Toc:
    ids: list[bytes]  # 12-byte chunk ids
    header: list[bool]  # the container's own header chunk (always kept)
    spans: list[tuple[int, int]]  # (offset, length) in the container's uncompressed space
    block_size: int  # uncompressed bytes per compression block
    blocks: list[tuple[int, int]]  # (offset, compressed size) of each block in the .ucas


def _read_toc(path: Path) -> _Toc:
    """An IoStore TOC, version 8 (UE 5.5): header, chunk ids, offsets+lengths, hash seeds, blocks, ..."""
    d = path.read_bytes()
    if len(d) < 144 or d[:16] != _TOC_MAGIC or d[16] != 8:
        raise _bad(path, "not a version 8 IoStore TOC")
    header_size, n, nblocks, block_entry, methods, method_len, block_size, dir_size, partitions = struct.unpack_from("<9I", d, 20)
    (seeds,) = struct.unpack_from("<I", d, 84)
    (unhashed,) = struct.unpack_from("<I", d, 96)
    if block_entry != 12:
        raise _bad(path, f"compression block entries of {block_entry} bytes")
    if d[80] & 6 or partitions != 1:  # Encrypted | Signed, or split over several .ucas
        raise _bad(path, "encrypted, signed or split containers aren't supported")
    ids_at = header_size
    spans_at = ids_at + n * 12
    blocks_at = spans_at + n * 10 + seeds * 4 + unhashed * 4
    end = blocks_at + nblocks * 12 + methods * method_len + dir_size
    if len(d) - end != n * 24:  # what's left is one meta entry per chunk
        raise _bad(path, "TOC sections don't add up to the file size")
    ids = [d[ids_at + i * 12 : ids_at + i * 12 + 12] for i in range(n)]
    spans = [
        (int.from_bytes(d[at : at + 5], "big"), int.from_bytes(d[at + 5 : at + 10], "big"))
        for at in range(spans_at, spans_at + n * 10, 10)
    ]
    blocks = [
        (int.from_bytes(d[at : at + 5], "little"), int.from_bytes(d[at + 5 : at + 8], "little"))
        for at in range(blocks_at, blocks_at + nblocks * 12, 12)
    ]
    return _Toc(ids, [i[11] == _TOC_HEADER_CHUNK for i in ids], spans, block_size, blocks)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, fmt: str) -> tuple:
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return values

    def fstring(self) -> str:
        (n,) = self.take("<i")
        size = -n * 2 if n < 0 else n
        raw = self.data[self.pos : self.pos + size]
        self.pos += size
        return (raw.decode("utf-16-le") if n < 0 else raw.decode("latin-1")).rstrip("\0")


@dataclass
class _Pak:
    mount: str
    entries: dict[str, int]  # path under the mount point -> where its entry header + data start
    data_end: int  # where the entries' data region ends (the indexes follow)


def _encoded_offset(encoded: bytes, at: int) -> int:
    (flags,) = struct.unpack_from("<I", encoded, at)
    at += 8 if flags & 0x3F == 0x3F else 4  # block size didn't fit the flag bits: stored after them
    return struct.unpack_from("<I" if flags >> 31 else "<Q", encoded, at)[0]


def _read_pak(path: Path) -> _Pak | None:
    """A version 11 pak's entries with their data offsets; None if its index is encrypted or has no
    full directory index (then nothing in it is touched, and nothing it holds counts as newer)."""
    with path.open("rb") as f:
        size = f.seek(0, 2)
        if size < _PAK_FOOTER:
            raise _bad(path, "too small to be a pak")
        f.seek(size - _PAK_FOOTER)
        footer = f.read(_PAK_FOOTER)
        magic, version, index_offset, index_size = struct.unpack_from("<IiQQ", footer, 17)
        if magic != _PAK_MAGIC:
            raise _bad(path, "no pak footer")
        if version != 11:
            raise _bad(path, f"pak version {version}, expected 11")
        if footer[16]:
            return None
        f.seek(index_offset)
        r = _Reader(f.read(index_size))
        mount = r.fstring()
        r.take("<iQ")  # entry count, path hash seed
        data_end = min(index_offset, size - _PAK_FOOTER)
        (has_hash_index,) = r.take("<I")
        if has_hash_index:
            hash_index_offset, _ = r.take("<QQ")
            r.pos += 20
            data_end = min(data_end, hash_index_offset)
        (has_dir_index,) = r.take("<I")
        if not has_dir_index:
            return None
        dir_offset, dir_size = r.take("<QQ")
        r.pos += 20
        data_end = min(data_end, dir_offset)
        (encoded_size,) = r.take("<I")
        encoded = r.data[r.pos : r.pos + encoded_size]
        r.pos += encoded_size
        (count,) = r.take("<I")
        plain = []  # entries that couldn't be bit-packed, stored in full
        for _ in range(count):
            offset, _size, _usize, method = r.take("<QQQI")
            r.pos += 20
            if method:
                (nblocks,) = r.take("<I")
                r.pos += nblocks * 16
            r.pos += 5  # flags, compression block size
            plain.append(offset)
        if r.pos != len(r.data):
            raise _bad(path, "index layout doesn't add up to its size")
        f.seek(dir_offset)
        r = _Reader(f.read(dir_size))
    pak = _Pak(mount, {}, data_end)
    (folders,) = r.take("<i")
    for _ in range(folders):
        folder = r.fstring()
        (files,) = r.take("<i")
        for _ in range(files):
            name = folder + r.fstring()
            (location,) = r.take("<i")
            if location == _NO_ENTRY:
                continue
            pak.entries[name] = plain[-location - 1] if location < 0 else _encoded_offset(encoded, location)
    return pak


# endregion
# region NTFS


class _Ranges(ctypes.Structure):
    _fields_ = (("offset", ctypes.c_int64), ("length", ctypes.c_int64))


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.GetCompressedFileSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
_k32.GetCompressedFileSizeW.restype = wintypes.DWORD
_k32.GetVolumeInformationW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD)
_k32.GetVolumeInformationW.restype = wintypes.BOOL

_FSCTL_SET_SPARSE = 0x900C4
_FSCTL_SET_ZERO_DATA = 0x980C8
_FSCTL_QUERY_ALLOCATED_RANGES = 0x940CF
_ERROR_MORE_DATA = 234


def _ioctl(f, code: int, data: ctypes.Structure | None = None, out=None) -> int:  # noqa: ANN001
    got = wintypes.DWORD()
    ok = _k32.DeviceIoControl(
        msvcrt.get_osfhandle(f.fileno()), code,
        ctypes.byref(data) if data is not None else None, ctypes.sizeof(data) if data is not None else 0,
        ctypes.byref(out) if out is not None else None, ctypes.sizeof(out) if out is not None else 0,
        ctypes.byref(got), None,
    )
    if not ok:
        err = ctypes.get_last_error()
        if err != _ERROR_MORE_DATA:
            raise ctypes.WinError(err)
        return -got.value
    return got.value


def _zero(path: Path, ranges: list[tuple[int, int]]) -> None:
    with path.open("r+b") as f:
        _ioctl(f, _FSCTL_SET_SPARSE)
        for lo, hi in ranges:
            _ioctl(f, _FSCTL_SET_ZERO_DATA, _Ranges(lo, hi))  # FILE_ZERO_DATA_INFORMATION: from, beyond


def _allocated(path: Path) -> list[tuple[int, int]]:
    """The byte ranges of the file that hold disk space (one range for a normal file)."""
    out: list[tuple[int, int]] = []
    buf = (_Ranges * 1024)()
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        start = 0
        while start < size:
            got = _ioctl(f, _FSCTL_QUERY_ALLOCATED_RANGES, _Ranges(start, size - start), buf)
            more = got < 0
            out += [(r.offset, r.offset + r.length) for r in buf[: abs(got) // ctypes.sizeof(_Ranges)]]
            if not more or not got:
                break
            start = out[-1][1]
    return out


def _probe_unit(folder: Path) -> int:
    """
    The size of the pieces this drive can free inside a file (64 KB on NTFS), found by blanking all
    but the ends of an 8 MB test file; 0 where it can't (FAT32/exFAT, no space for the test).
    """
    probe = folder / _PROBE_NAME
    n = 8 << 20
    try:
        probe.write_bytes(b"\1" * n)
        _zero(probe, [(1, n - 1)])
        held = _allocated(probe)
        if len(held) == 2 and held[0][0] == 0 and held[1][1] == n and held[0][1] == n - held[1][0]:
            return held[0][1]
        return 0
    except OSError:
        return 0
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _can_write(folder: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def _is_ntfs(folder: Path) -> bool:
    name = ctypes.create_unicode_buffer(32)
    root = os.path.splitdrive(str(folder.resolve()))[0] + "\\"
    ok = _k32.GetVolumeInformationW(root, None, 0, None, None, None, name, len(name))
    return bool(ok) and name.value.upper() == "NTFS"


def size_on_disk(folder: Path) -> int:
    """What a folder really takes on disk (sparse files count only their allocated parts)."""
    total = 0
    high = wintypes.DWORD()
    for root, _dirs, files in os.walk(folder):
        for name in files:
            low = _k32.GetCompressedFileSizeW(os.path.join(root, name), ctypes.byref(high))
            if low == 0xFFFFFFFF and ctypes.get_last_error():
                continue
            total += (high.value << 32) | low
    return total


# endregion
# region Ranges


def _merge(ranges) -> list[tuple[int, int]]:  # noqa: ANN001
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _overlap(merged: list[tuple[int, int]], starts: list[int], lo: int, hi: int) -> int:
    """Bytes of [lo, hi) inside `merged` (sorted, disjoint; `starts` = their starts)."""
    total = 0
    j = max(0, bisect.bisect_right(starts, lo) - 1)
    while j < len(merged) and merged[j][0] < hi:
        total += max(0, min(hi, merged[j][1]) - max(lo, merged[j][0]))
        j += 1
    return total


def _subtract(ranges: list[tuple[int, int]], cut: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """`ranges` minus `cut` (both merged)."""
    out = []
    j = 0
    for lo, hi in ranges:
        while j < len(cut) and cut[j][1] <= lo:
            j += 1
        k = j
        while lo < hi and k < len(cut) and cut[k][0] < hi:
            if cut[k][0] > lo:
                out.append((lo, cut[k][0]))
            lo = max(lo, cut[k][1])
            k += 1
        if lo < hi:
            out.append((lo, hi))
    return out


def _upto_next(starts: list[int], lo: int, end: int) -> tuple[int, int]:
    """[lo, the next start after lo): a dead entry plus any padding up to the next entry."""
    k = bisect.bisect_right(starts, lo)
    return lo, starts[k] if k < len(starts) else end


# endregion
# region Scan, plan, apply


@dataclass
class Plan:
    keep: list[str]
    old: bool
    remove: list[Path] = field(default_factory=list)  # voice chunk files to delete
    voice_bytes: int = 0
    todo: dict[Path, list[tuple[int, int]]] = field(default_factory=dict)  # ranges to blank, per file
    old_bytes: int = 0  # disk space blanking them frees

    @property
    def total(self) -> int:
        return self.voice_bytes + self.old_bytes


class Scanner:
    """Reads the game's pak and IoStore indexes once; `plan()` then works out a choice from them."""

    def __init__(self, game_dir: Path, log: Log = print) -> None:
        self.game_dir = game_dir
        self.paks_dir = game_dir / PAKS_REL
        if not self.paks_dir.is_dir():
            raise SlimError(f"No Paks folder in {game_dir}")
        self.patch: dict[Path, int] = {}
        chunks: dict[int, list[Path]] = {}
        for entry in os.scandir(self.paks_dir):
            if entry.is_file() and (m := _CHUNK_FILE.match(entry.name)):
                path = Path(entry.path)
                self.patch[path] = int(m.group(2))
                chunks.setdefault(int(m.group(1)), []).append(path)
        self.stamps = {p: _stamp(p) for p in self.patch}
        log(f"Reading {len(self.patch)} game files...")
        self.paks: dict[Path, _Pak | None] = {}
        self.tocs: dict[Path, _Toc] = {}
        for path in sorted(self.patch):
            ext = path.suffix.lower()
            if ext == ".pak":
                self.paks[path] = _read_pak(path)
            elif ext == ".utoc":
                self.tocs[path] = _read_toc(path)
        # language -> the files of its voice chunks, .pak last (so a stopped delete is found again)
        self.voices: dict[str, list[Path]] = {}
        for files in chunks.values():
            if language := self._chunk_language(files):
                self.voices.setdefault(language, []).extend(files)
        for files in self.voices.values():
            files.sort(key=lambda p: (p.suffix.lower() == ".pak", p.name))
        self.voice_sizes = {lang: sum(self.stamps[p][0] for p in files) for lang, files in self.voices.items()}
        # Where this user can't write (an elevated copy of the app does the freeing), assume NTFS's
        # 64 KB for the estimate
        self.writable = _can_write(self.paks_dir)
        self.unit = _probe_unit(self.paks_dir) if self.writable else (1 << 16) * _is_ntfs(self.paks_dir)
        self._held: dict[Path, list[tuple[int, int]]] = {}
        self._io_dead: dict[Path, list[tuple[int, int]]] | None = None
        self.on_disk = size_on_disk(game_dir)

    def _chunk_language(self, files: list[Path]) -> str | None:
        languages: set[str | None] = set()
        for path in files:
            ext = path.suffix.lower()
            if ext == ".pak":
                pak = self.paks.get(path)
                if pak is None or pak.entries and not pak.mount.rstrip("/").endswith("WwiseAudio"):
                    return None
                for name in pak.entries:
                    parts = name.strip("/").split("/")
                    if parts[0] == "Media":
                        parts = parts[1:]
                    languages.add(parts[0] if len(parts) > 1 else None)
            elif ext == ".utoc" and not all(self.tocs[path].header):
                return None
        return languages.pop() if len(languages) == 1 and next(iter(languages)) in LANGUAGES else None

    def default_keep(self) -> list[str]:
        """English, plus Windows' own language when it has a voice pack."""
        keep = ["English(US)"]
        windows = _WINDOWS_LANGUAGE.get(ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF)
        if windows and windows not in keep:
            keep.append(windows)
        keep = [k for k in keep if k in self.voices]
        return keep or sorted(self.voices)[:1]

    def plan(self, keep: list[str], old: bool = True) -> Plan:
        """What to remove when keeping the voice languages `keep` (and blanking old copies if `old`)."""
        unknown = [k for k in keep if k not in self.voices]
        if unknown and self.voices:
            raise SlimError(f"No {', '.join(unknown)} voice pack is installed. Found: {', '.join(sorted(self.voices))}")
        plan = Plan(sorted(keep), old)
        for language, files in sorted(self.voices.items()):
            if language not in keep:
                plan.remove += files
                plan.voice_bytes += self.voice_sizes[language]
        if not old or not self.unit:
            return plan
        gone = set(plan.remove)
        dead = self._iostore_dead(gone)
        self._plan_paks(gone, dead)
        for path, ranges in dead.items():
            held = self._held.get(path)
            if held is None:
                held = self._held[path] = _merge(_allocated(path))
            starts = [lo for lo, _ in held]
            unit = self.unit
            gains = [_overlap(held, starts, -(-lo // unit) * unit, hi // unit * unit) for lo, hi in ranges]
            if any(gains):
                plan.todo[path] = ranges
                plan.old_bytes += sum(gains)
        return plan

    def _iostore_dead(self, gone: set[Path]) -> dict[Path, list[tuple[int, int]]]:
        """
        The dead .ucas ranges. A voice chunk's container holds nothing but its own header (always
        kept), so this doesn't depend on which languages stay: worked out once (~1-2 s).
        """
        if self._io_dead is None:
            self._io_dead = {}
            self._plan_iostore(self._io_dead)
        return {p: r for p, r in self._io_dead.items() if p not in gone and p.with_suffix(".utoc") not in gone}

    def _plan_iostore(self, dead: dict) -> None:  # noqa: ANN001
        newest: dict[bytes, int] = {}
        for path, toc in self.tocs.items():
            patch = self.patch[path]
            for cid in toc.ids:
                if newest.get(cid, -1) < patch:
                    newest[cid] = patch
        for path, toc in self.tocs.items():
            patch = self.patch[path]
            bs = toc.block_size
            # Runs of compression blocks [first, last + 1) each chunk spans
            keep_runs, dead_runs = [], []
            for cid, header, (offset, length) in zip(toc.ids, toc.header, toc.spans, strict=True):
                if length:
                    run = (offset // bs, (offset + length - 1) // bs + 1)
                    (keep_runs if header or newest[cid] <= patch else dead_runs).append(run)
            if not dead_runs:
                continue
            keep_runs = _merge(keep_runs)
            runs = _subtract(_merge(dead_runs), keep_runs)  # a block shared with a live chunk stays
            if not runs:
                continue
            blocks = toc.blocks
            if all(blocks[i][0] + blocks[i][1] <= blocks[i + 1][0] for i in range(len(blocks) - 1)):
                # Blocks laid out in order (what the game's containers do): a run is one byte range,
                # up to the next block's start (so the padding after it goes too)
                def end(b: int) -> int:
                    return blocks[b][0] if b < len(blocks) else sum(blocks[b - 1])

                old = [(blocks[b0][0], end(b1)) for b0, b1 in runs]
                kept = [(blocks[b0][0], sum(blocks[b1 - 1])) for b0, b1 in keep_runs]
            else:  # any other layout: block by block (slow, same result)
                starts = sorted({off for off, _ in blocks})
                old = [_upto_next(starts, blocks[b][0], sum(blocks[b])) for b0, b1 in runs for b in range(b0, b1)]
                kept = [(blocks[b][0], sum(blocks[b])) for b0, b1 in keep_runs for b in range(b0, b1)]
            self._check(path.with_suffix(".ucas"), old, kept, dead)

    def _plan_paks(self, gone: set[Path], dead: dict) -> None:  # noqa: ANN001
        live = {p: pak for p, pak in self.paks.items() if pak is not None and p not in gone}
        newest: dict[str, int] = {}
        for path, pak in live.items():
            patch = self.patch[path]
            for name in pak.entries:
                key = pak.mount + name
                if newest.get(key, -1) < patch:
                    newest[key] = patch
        for path, pak in live.items():
            patch = self.patch[path]
            used = {off for name, off in pak.entries.items() if newest[pak.mount + name] <= patch}
            starts = sorted(set(pak.entries.values()))
            old = [_upto_next(starts, s, pak.data_end) for s in starts if s not in used]
            if old:
                self._check(path, old, [(s, s + 1) for s in used], dead)

    def _check(self, path: Path, old, kept, dead: dict) -> None:  # noqa: ANN001
        old = _merge(old)
        if not old:
            return
        starts = [lo for lo, _ in old]
        for lo, hi in kept:
            if _overlap(old, starts, lo, hi):
                raise _bad(path, f"an old range covers data the game reads at byte {lo}")
        if old[-1][1] > self.stamps[path][0]:
            raise _bad(path, "an old range runs past the end of the file")
        dead[path] = old

    def apply(self, plan: Plan, log: Log = print) -> int:
        """Deletes and blanks what `plan` lists. Returns the disk space freed."""
        for path in [*plan.remove, *plan.todo]:
            if not path.exists() or _stamp(path) != self.stamps[path]:
                raise _bad(path, "changed since the scan (a Steam update?). Scan again.")
        for path in plan.remove:
            path.unlink()
        if plan.remove:
            log(f"Removed {len(plan.remove)} voice files ({gb(plan.voice_bytes)}).")
        total = len(plan.todo)
        for i, (path, ranges) in enumerate(plan.todo.items(), 1):
            log(f"[{i}/{total}] {path.name}")
            size = self.stamps[path][0]
            # The live bytes next to every range: must read back the same once it's blanked
            near = _merge((max(0, lo - _CHECK_MARGIN), min(size, hi + _CHECK_MARGIN)) for lo, hi in ranges)
            check = _subtract(near, ranges)
            before = _hash(path, check)
            try:
                _zero(path, ranges)
            finally:
                if _hash(path, check) != before:
                    raise _bad(path, "data the game reads changed. Run Verify integrity of game files in Steam.")
        after = size_on_disk(self.game_dir)
        return self.on_disk - after


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _hash(path: Path, ranges: list[tuple[int, int]]) -> bytes:
    h = hashlib.sha1()  # noqa: S324 - a change check, not security
    with path.open("rb") as f:
        for lo, hi in ranges:
            f.seek(lo)
            data = f.read(hi - lo)
            if len(data) != hi - lo:
                raise _bad(path, f"file ends before byte {hi}")
            h.update(data)
    return h.digest()


def gb(n: int) -> str:
    return f"{n / 2**30:.2f} GB" if n >= 2**30 or n == 0 else f"{n / 2**20:.1f} MB"


def match_language(text: str, installed: list[str]) -> str:
    """A voice language from what a user typed: 'German', 'english', 'English(US)'."""
    want = text.strip().lower()
    found = [n for n in installed if n.lower() == want] or [n for n in installed if n.lower().startswith(want)]
    if len(found) != 1:
        raise SlimError(f"No single voice pack matches '{text}'. Installed: {', '.join(installed)}")
    return found[0]


def steam_busy(game_dir: Path) -> bool:
    """Whether Steam is updating or checking the game (its manifest isn't 'fully installed')."""
    manifest = game_dir.parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False  # not a Steam install
    m = re.search(r'"StateFlags"\s+"(\d+)"', text)
    return bool(m) and m.group(1) != "4"


# endregion
