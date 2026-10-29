"""
Finds two of the game's camera mode definitions in memory and edits them.

BL4's camera modes are data (the camera_mode NCS records), loaded at startup into native structs
that the camera code reads each time a mode starts. Nothing reaches them through reflection, so we
find the two we use by their contents:

- camMode_exo_heavyarms (Exo Soldier's heavy-weapon camera, our hip camera) and
  ThirdPerson_OverShoulder (not used in normal play, our aim-in camera) both have a
  CameraModeBehavior_RelativeOffset of (-420, 40, 30). The behavior is a native struct
  {vtable, label, flags, FVector LocationOffset (doubles), ...}, so pass 1 looks for those three
  doubles right after a vtable that points into the game's exe.
- A mode definition holds its behaviors as an array of 24-byte {data, header, label} entries in
  NCS order, with the header 0x18 bytes before the data. Pass 2 finds the entries that point at
  our two offsets. The entry's index tells the modes apart: heavyarms has InterpCrouch before the
  offset (index 5), OverShoulder doesn't (index 4). Index 2 in both is CameraModeBehavior_DefaultFOV
  ({vtable, label, flags, float Scale, float MaxFOV}). Scale works like a weapon's ZoomFOVScale.

Edits take effect the next time the mode starts (SetActorCameraMode with bForceResetMode).

Reads go through ReadProcessMemory on our own process, so a page that goes away mid-scan fails
the read instead of crashing the game. A scan reads every small private block twice (2026-10-24:
95k blocks, 6.2 GB; pass 1 ~9 s, pass 2 ~24 s, up to 70 s with the game busy), so it runs on a
worker thread that lets go of the GIL about every millisecond (the game thread's hooks wait for the
GIL while it holds it) and, while nobody waits for it, rests 2 ms each time (see REST_S).
Before every write we check that both vtables are still in place, in case the game rebuilt its
definitions (e.g. after applying hotfixes); if not, we scan again.
"""

from __future__ import annotations

import ctypes
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from unrealsdk import logging

VANILLA_OFFSET = (-420.0, 40.0, 30.0)
OFFSET_FIELD = 24  # CameraModeBehavior_RelativeOffset.LocationOffset
FOV_SCALE_FIELD = 24  # CameraModeBehavior_DefaultFOV.Scale
HEADER_SIZE = 0x18
ENTRY_SIZE = 24
DEFAULT_FOV_INDEX = 2
HIP_OFFSET_INDEX = 5  # camMode_exo_heavyarms
AIM_OFFSET_INDEX = 4  # ThirdPerson_OverShoulder
# The definitions live in small heap blocks; skipping big ones cuts the scan from ~50 s to ~5 s
MAX_REGION = 1 << 17

MEM_COMMIT, MEM_PRIVATE, PAGE_READWRITE = 0x1000, 0x20000, 0x04

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _MemoryInfo(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


_kernel32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MemoryInfo), ctypes.c_size_t]
_kernel32.VirtualQuery.restype = ctypes.c_size_t
_kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.WriteProcessMemory.argtypes = _kernel32.ReadProcessMemory.argtypes
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_process = _kernel32.GetCurrentProcess()
YIELD_S = 0.001  # the scan lets other Python threads (the game thread's hooks) in this often
# In the background (nobody waiting) it also rests this long each time: at full speed its ~95k
# VirtualQuery + ReadProcessMemory calls made the game thread's hooks 3x slower with 5-9 ms ticks
# (2026-10-24), so they're spread out. Don't lower the thread's priority instead: preempted while
# holding the GIL or the heap lock, it stalled the game thread 40-100 ms (priority inversion).
REST_S = 0.002
_last_yield = 0.0


def _yield_gil() -> None:
    global _last_yield
    now = time.perf_counter()
    if now - _last_yield >= YIELD_S:
        # Even in a hurry a real sleep: sleep(0) takes the GIL straight back (starves the game thread)
        time.sleep(0.001 if Finder._urgent else REST_S)  # noqa: SLF001
        _last_yield = time.perf_counter()


def log(msg: str) -> None:
    logging.info(f"[Third Person] {msg}")


def _read(address: int, size: int) -> bytes | None:
    buffer = ctypes.create_string_buffer(size)
    done = ctypes.c_size_t()
    if not _kernel32.ReadProcessMemory(_process, address, buffer, size, ctypes.byref(done)):
        return None
    return buffer.raw


def _write(address: int, data: bytes) -> bool:
    done = ctypes.c_size_t()
    return bool(_kernel32.WriteProcessMemory(_process, address, data, len(data), ctypes.byref(done)))


def _qword(address: int) -> int | None:
    data = _read(address, 8)
    return struct.unpack("<Q", data)[0] if data else None


def _exe_range() -> tuple[int, int]:
    base = _kernel32.GetModuleHandleW(None)
    pe = base + struct.unpack("<I", _read(base + 0x3C, 4))[0]
    size_of_image = struct.unpack("<I", _read(pe + 0x50, 4))[0]
    return base, base + size_of_image


# One buffer for every block of a scan (a new 128 KB bytes per block was ~190k allocations a scan)
_block = bytearray(MAX_REGION)
_block_c = (ctypes.c_char * MAX_REGION).from_buffer(_block)


def _small_heap_blocks():  # noqa: ANN202
    """
    (base, size) of every small committed read-write private block, its contents in _block[:size]
    until the next one (search with _block.find(x, start, size)).
    """
    address = 0
    info = _MemoryInfo()
    done = ctypes.c_size_t()
    while _kernel32.VirtualQuery(address, ctypes.byref(info), ctypes.sizeof(info)):
        base, size = info.BaseAddress or 0, info.RegionSize
        if (
            info.State == MEM_COMMIT
            and info.Protect == PAGE_READWRITE
            and info.Type == MEM_PRIVATE
            and size <= MAX_REGION
        ):
            if _kernel32.ReadProcessMemory(_process, base, _block_c, size, ctypes.byref(done)):
                yield base, size
            _yield_gil()
        address = base + size


@dataclass
class ModeDef:
    """One mode's offset and FOV behaviors, and their vtables, to check before writing."""

    offset: int
    fov: int
    offset_vtable: int
    fov_vtable: int

    def intact(self) -> bool:
        return _qword(self.offset) == self.offset_vtable and _qword(self.fov) == self.fov_vtable

    def set(self, x: float, y: float, z: float, fov_scale: float) -> bool:
        if not self.intact():
            return False
        return _write(self.offset + OFFSET_FIELD, struct.pack("<3d", x, y, z)) and _write(
            self.fov + FOV_SCALE_FIELD, struct.pack("<f", fov_scale),
        )


@dataclass
class Defs:
    hip: ModeDef
    aim: ModeDef

    def intact(self) -> bool:
        return self.hip.intact() and self.aim.intact()

    def restore(self) -> None:
        for mode in (self.hip, self.aim):
            mode.set(*VANILLA_OFFSET, 1.0)


def _array_entry(address: int) -> tuple[int, int] | None:
    """(data, label) if a {data, header, label} behavior entry is at address."""
    raw = _read(address, ENTRY_SIZE)
    if raw is None:
        return None
    data, header, label = struct.unpack("<QQQ", raw)
    if data == 0 or data % 8 or header != data - HEADER_SIZE or label == 0:
        return None
    return data, label


def _scan() -> Defs | None:
    started = time.monotonic()
    exe_lo, exe_hi = _exe_range()

    # Pass 1: RelativeOffset behaviors with the vanilla (-420, 40, 30)
    pattern = struct.pack("<3d", *VANILLA_OFFSET)
    offsets: list[int] = []
    data = _block
    for base, size in _small_heap_blocks():
        i = data.find(pattern, 0, size)
        while i >= 0:
            start = base + i - OFFSET_FIELD
            if start % 8 == 0 and i >= OFFSET_FIELD:
                vtable = struct.unpack_from("<Q", data, i - OFFSET_FIELD)[0]
                if exe_lo <= vtable < exe_hi:
                    offsets.append(start)
            i = data.find(pattern, i + 1, size)
    if len(offsets) < 2:  # noqa: PLR2004
        log(f"Camera data not found ({len(offsets)} candidates).")
        return None

    # Pass 2: the definitions' behavior arrays that point at them
    wanted = {struct.pack("<QQ", o, o - HEADER_SIZE): o for o in offsets}
    found: dict[int, ModeDef] = {}
    for base, size in _small_heap_blocks():
        for needle, offset in wanted.items():
            i = data.find(needle, 0, size)
            while i >= 0:
                entry = base + i
                if entry % 8 == 0:
                    mode = _mode_def(entry, offset, exe_lo, exe_hi)
                    if mode is not None:
                        found[mode[0]] = mode[1]
                i = data.find(needle, i + 1, size)
    if HIP_OFFSET_INDEX not in found or AIM_OFFSET_INDEX not in found:
        log(f"Camera modes not found (got behavior indexes {sorted(found)}).")
        return None
    log(f"Found the camera modes in {time.monotonic() - started:.1f} s.")
    return Defs(hip=found[HIP_OFFSET_INDEX], aim=found[AIM_OFFSET_INDEX])


def _mode_def(entry: int, offset: int, exe_lo: int, exe_hi: int) -> tuple[int, ModeDef] | None:
    """(index of the offset behavior, ModeDef) for the behavior array holding entry."""
    index = 0
    while index < 32 and _array_entry(entry - ENTRY_SIZE) is not None:  # noqa: PLR2004
        entry -= ENTRY_SIZE
        index += 1
    fov_entry = _array_entry(entry + DEFAULT_FOV_INDEX * ENTRY_SIZE)
    if fov_entry is None:
        return None
    fov = fov_entry[0]
    raw = _read(fov, FOV_SCALE_FIELD + 8)
    offset_vtable = _qword(offset)
    if raw is None or offset_vtable is None:
        return None
    fov_vtable = struct.unpack_from("<Q", raw)[0]
    scale, max_fov = struct.unpack_from("<2f", raw, FOV_SCALE_FIELD)
    if not exe_lo <= fov_vtable < exe_hi or scale != 1.0 or max_fov != 0.0:
        return None
    return index, ModeDef(offset=offset, fov=fov, offset_vtable=offset_vtable, fov_vtable=fov_vtable)


class Finder:
    """Runs the scan on a worker thread; the game thread polls `defs`."""

    defs: Defs | None = None
    _thread: threading.Thread | None = None
    _urgent = False  # the player is waiting: no rests
    _failed_at = 0.0
    _failures = 0
    _checked_at = 0.0
    RETRY_AFTER = 60.0
    # A scan costs 35-70 s of background work that slows every mod's hooks. After this many finding
    # nothing, it's this game build (a patch moved the data), not timing: stop for the session.
    MAX_FAILURES = 3
    # get() runs every frame while third person is on; ModeDef.set checks before every write anyway
    CHECK_S = 1.0

    @classmethod
    def get(cls) -> Defs | None:
        """The definitions if found and still intact; otherwise starts (or retries) a scan."""
        now = time.monotonic()
        if cls.defs is not None and now - cls._checked_at >= cls.CHECK_S:
            cls._checked_at = now
            if not cls.defs.intact():
                log("Camera data moved; looking again.")
                cls.defs = None
        if cls.defs is None:
            cls.start(urgent=True)
        return cls.defs

    @classmethod
    def start(cls, *, urgent: bool = False) -> None:
        """Starts a scan if none ran yet. urgent: the player is waiting (full speed)."""
        if cls._thread is not None and cls._thread.is_alive():
            cls._urgent = cls._urgent or urgent
            return
        if cls.defs is not None or time.monotonic() - cls._failed_at < cls.RETRY_AFTER:
            return
        if cls.gave_up():
            return
        cls._urgent = urgent
        cls._thread = threading.Thread(target=cls._work, name="bl4_third_person scan", daemon=True)
        cls._thread.start()

    @classmethod
    def _work(cls) -> None:
        try:
            cls.defs = _scan()
        except Exception as ex:  # noqa: BLE001
            log(f"Scan failed: {ex!r}")
            cls.defs = None
        if cls.defs is None:
            cls._failed_at = time.monotonic()
            cls._failures += 1
            if cls.gave_up():
                log("Third person doesn't work on this game version (its camera data wasn't found "
                    f"{cls._failures} times; a game patch?): H does nothing until the mod is updated.")
        else:
            cls._failures = 0

    @classmethod
    def gave_up(cls) -> bool:
        return cls._failures >= cls.MAX_FAILURES
