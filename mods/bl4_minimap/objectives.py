"""
The tracked mission's objectives, live (what the compass shows), read from native memory.

None of this is reflected, so it's read with ReadProcessMemory (a bad address gives None, never a
crash) and every step checks itself; if anything doesn't add up, `Objectives.broken` is set and
the minimap falls back to the ECHO map's copy (refreshed when you open the map).

- `GbxDiscoveryProvider_OakMissionWaypoints` (one, in /Engine/Transient) keeps its active waypoints
  in a TArray at +0x38: 24-byte entries {GbxDiscoveryProviderObject_OakMissionWaypoint*, id, 0};
  the id equals the waypoint's own +0x30. (+0x48 is the pool of unused ones.)
- A waypoint's +0xC8 is a shared pointer to its data; the data's +0x38 is a FWeakObjectPtr
  (object index, serial) to the target actor (an NPC to talk to, an object to use, ...). Waypoints
  without an actor (go to an area) have the weak pointer zeroed and their own position at data
  +0x50 (an FVector of doubles). Data with the giver vtable (or kind 4 at +0x30, Cowbell_P; World_P's
  givers read 2 there) aren't objectives: they're the mission givers
  the ECHO map marks (`ico_side_mission_npc[_dlc]`, same spots to the metre) and contract posters by
  bounty boards. Kept apart in `givers`: they come and go as you take and finish missions.
- The weak pointer resolves through GUObjectArray, whose address we take from the code that
  resolves it (the waypoint's own virtual at RVA 0xC8A2D4: `cmp [rip+X], edx` = NumElements,
  `mov r9, [rip+Y]` = the chunk table; 65536 items of 24 bytes per chunk: object*, flags, ...,
  serial at +0x10). Checked by resolving our own pawn.
- The actor's position: its RootComponent's RelativeLocation (reflected offsets), when the root
  isn't attached to anything (then relative = world).

Found by disassembling OakEcho4Statics.WasWaypointPathFound and the waypoint class's virtuals.
"""

from __future__ import annotations

import ctypes
import math
import struct
import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject, WeakPointer

READ_INTERVAL = 0.5  # s between reads of the objective list
SEARCH_INTERVAL = 2.0  # s between searches for the provider (each walks every object), paused only
# A sanity bound, not a real limit: World_P lists 93 (mostly mission givers). It was 32, and on
# reaching World_P live objectives turned off for the session (markers only changed with the ECHO map)
MAX_WAYPOINTS = 2048
DATA_SIZE = 0x80  # what we read of a waypoint's data
DATA_LOCATION = 0x50  # its own position (doubles), for waypoints without a target actor
# What kind of waypoint (a guess from what each showed): the tracked mission's objectives (on the
# compass) were 1 (an NPC, or an area) and 2 (an area across the map); 4 = mission givers and the
# contract posters by bounty boards (not on the compass; their data has another vtable, RVA
# 0xB41AC40), with or without an actor. Cowbell_P, 2026-10-28: five kind-4 points sat exactly on the
# ECHO map's five `ico_side_mission_npc_dlc` icons, three more 2-3 m from its bounty board.
# World_P, 2026-10-28: all 93 listed points (givers and posters) had +0x30 = **2** with that same
# giver vtable, so the kind alone drew every giver as an objective (rows of them on the rim). The
# data's vtable is what tells them apart: givers/posters 0xB41AC40, objectives 0xB400BE0, in both maps.
DATA_KIND = 0x30
KIND_GIVER = 4
# On objectives (not givers) +0x30 looks like the mission's type: 1 for World_P's side mission "Old
# Lads", 2 for the Cowbell_P objective drawn as ico_main_mission_dlc (2026-10-28; two samples)
KIND_SIDE, KIND_MAIN = 1, 2
GIVER_DATA_VTABLE = 0xB41AC40  # RVA in this build (pinned like _NUM_INSN below)
OBJECTIVE_DATA_VTABLE = 0xB400BE0  # the tracked mission's objectives
# An objective's data +0x78 is an FName: its mission's NCS `missiontype` (a tracked bounty read
# "contract", World_P 2026-10-28). Compared as raw FName bytes against the names below (made once
# through a GameplayTag), so unknown bytes are never decoded. Lowercase: FNames ignore case.
DATA_TYPE = 0x78
MISSION_TYPES = (
    "Contract", "SideMission", "MicroMission", "DLC_SideMission", "DLC_MicroMission", "DLC_MainStory",
    "FactionMission", "DynamicEvent", "Event", "Safehouse", "Crawler", "Silo",
    "DrillSite", "OrderBunker", "Mine", "Volcano", "PearlGenerator", "Raid", "TikiBar", "Colosseum",
    "DigispaceBoss", "Takedown", "Takedown_Hardcore", "Takedown_Timer", "FuelSiphon", "TedioreGrotto",
    "TedioreLockdown", "Airship", "Meteor", "RiftBoss", "DahlBunker",
)  # fmt: skip
# The waypoint's own +0x44 holds flags; 0x20 = the mission you're tracking. Two samples, World_P:
# 2026-10-28 three accepted bounties, the tracked one (where the ECHO map's route ended, the only one
# on the compass) read 0x20 there and +0xD8 = 1, the others 0 and 0. 2026-10-29 two bounties, the
# same again. (+0x40 bit 4 was used first: 0 on the tracked one the first time, but 4 on both the
# second, which hid the tracked bounty and its route.)
WAYPOINT_FLAGS = 0x44
FLAG_TRACKED = 0x20
MAX_COORD = 2e7  # world units: anything further out isn't a position
SAME_SPOT = 500.0  # waypoints closer than this (5 m) show as one

# Where GUObjectArray's fields are referenced (RVAs in this build), and the bytes we expect there
_NUM_INSN = 0xC8A31F  # cmp dword ptr [rip+disp32], edx
_NUM_OPCODE = b"\x39\x15"
_CHUNKS_INSN = 0xC8A32B  # mov r9, qword ptr [rip+disp32]
_CHUNKS_OPCODE = b"\x4c\x8b\x0d"

_k32 = ctypes.PyDLL("kernel32")
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.GetModuleHandleW.restype = ctypes.c_void_p
_k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
_k32.ReadProcessMemory.restype = ctypes.c_int
_process = _k32.GetCurrentProcess()


def _read(address: int, size: int) -> bytes | None:
    if not 0x10000 <= address < 0x7FFFFFFFFFFF or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)):
        return None
    return buffer.raw if got.value == size else None


def _u64(address: int) -> int | None:
    raw = _read(address, 8)
    return struct.unpack("<Q", raw)[0] if raw else None


def _data_location(data: bytes) -> tuple[float, float, float] | None:
    """The waypoint data's own position (an FVector of doubles at +0x50), if it looks like one."""
    where = struct.unpack_from("<3d", data, DATA_LOCATION)
    if not all(math.isfinite(v) and abs(v) < MAX_COORD for v in where) or not any(where):
        return None
    return where


def _near(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2 < SAME_SPOT**2


def _offset(class_name: str, prop: str) -> int:
    return int(unrealsdk.find_class(class_name)._find(prop).Offset_Internal)  # noqa: SLF001


class Objectives:
    provider = WeakPointer()
    next_search = 0.0
    next_read = 0.0
    broken = ""  # why we gave up ("" = working)
    checked = False
    num_addr = 0
    chunks_addr = 0
    waypoint_vtable = 0
    giver_vtable = 0  # address of the giver/poster data's vtable
    root_off = attach_off = location_off = 0
    points: list[tuple[float, float, float]] = []  # noqa: RUF012  # world positions
    types: list[str] = []  # noqa: RUF012  # each point's mission type (lowercase MISSION_TYPES, "" unknown)
    untracked: list[bool] = []  # noqa: RUF012  # each point: from a mission you aren't tracking
    type_names: dict[bytes, str] = {}  # noqa: RUF012  # FName bytes -> lowercase mission type
    objective_vtable = 0
    mission_kind = 0  # KIND_SIDE / KIND_MAIN of the first tracked objective (0: none or unknown)
    givers: list[tuple[float, float, float]] = []  # noqa: RUF012  # mission givers / contract posters
    read_ok = False  # the last read went through (givers is this level's list, not just empty)
    seen_givers: dict[tuple[int, int], tuple[float, float, float] | None] = {}  # noqa: RUF012  # (waypoint, id) -> spot

    @classmethod
    def _fail(cls, why: str) -> None:
        if not cls.broken:
            unrealsdk.logging.warning(f"[Minimap] live objectives off: {why}")
        cls.broken = why
        cls.points = []
        cls.types = []
        cls.untracked = []
        cls.givers = []
        cls.read_ok = False

    @classmethod
    def _setup(cls, pawn: UObject) -> bool:
        """Finds GUObjectArray and the offsets, and checks them on our own pawn."""
        base = _k32.GetModuleHandleW(None)
        num_code = _read(base + _NUM_INSN, 6)
        chunks_code = _read(base + _CHUNKS_INSN, 7)
        if not num_code or not chunks_code or not num_code.startswith(_NUM_OPCODE) or not chunks_code.startswith(_CHUNKS_OPCODE):
            cls._fail("game code changed (a patch?)")
            return False
        cls.num_addr = base + _NUM_INSN + 6 + struct.unpack_from("<i", num_code, 2)[0]
        cls.chunks_addr = base + _CHUNKS_INSN + 7 + struct.unpack_from("<i", chunks_code, 3)[0]
        index = struct.unpack("<i", _read(pawn._get_address() + 0xC, 4) or b"\xff\xff\xff\xff")[0]  # noqa: SLF001
        resolved = cls._object_at(index)
        if resolved is None or resolved[0] != pawn._get_address():  # noqa: SLF001
            cls._fail("object table check failed")
            return False
        cdo = unrealsdk.find_class("GbxDiscoveryProviderObject_OakMissionWaypoint").ClassDefaultObject
        cls.waypoint_vtable = _u64(cdo._get_address()) or 0  # noqa: SLF001
        cls.giver_vtable = base + GIVER_DATA_VTABLE
        cls.objective_vtable = base + OBJECTIVE_DATA_VTABLE
        cls.type_names = {}
        for name in MISSION_TYPES:
            tag = unrealsdk.make_struct("GameplayTag", TagName=name)
            cls.type_names[ctypes.string_at(tag._get_address(), 8)] = name.lower()  # noqa: SLF001
        cls.root_off = _offset("Actor", "RootComponent")
        cls.attach_off = _offset("SceneComponent", "AttachParent")
        cls.location_off = _offset("SceneComponent", "RelativeLocation")
        cls.checked = True
        return True

    @classmethod
    def _object_at(cls, index: int) -> tuple[int, int] | None:
        """(address, serial) of GUObjectArray item `index`."""
        num = _read(cls.num_addr, 4)
        if num is None or not 0 <= index < struct.unpack("<i", num)[0]:
            return None
        chunks = _u64(cls.chunks_addr)
        chunk = _u64(chunks + (index >> 16) * 8) if chunks else None
        item = _read(chunk + (index & 0xFFFF) * 24, 24) if chunk else None
        if item is None:
            return None
        return struct.unpack_from("<Q", item, 0)[0], struct.unpack_from("<i", item, 0x10)[0]

    @classmethod
    def _location(cls, actor: int) -> tuple[float, float, float] | None:
        root = _u64(actor + cls.root_off)
        if not root or _u64(root + cls.attach_off):
            return None  # attached: its relative location isn't the world's
        raw = _read(root + cls.location_off, 24)
        return struct.unpack("<3d", raw) if raw else None

    @classmethod
    def _find_provider(cls, paused: bool, new_world: bool) -> UObject | None:
        provider = cls.provider()
        if provider is not None:
            return provider
        now = time.monotonic()
        first = cls.next_search == 0.0  # never searched (just loaded or reloaded)
        if not first and not new_world and (not paused or now < cls.next_search):
            return None
        cls.next_search = now + SEARCH_INTERVAL
        for found in unrealsdk.find_all("GbxDiscoveryProvider_OakMissionWaypoints", exact=True):
            if "Default__" not in found._path_name():  # noqa: SLF001
                cls.provider = WeakPointer(found)
                return found
        return None

    @classmethod
    def update(cls, pawn: UObject, *, paused: bool, new_world: bool) -> None:
        """Re-reads the objectives every READ_INTERVAL. Read `points` afterwards."""
        now = time.monotonic()
        if cls.broken or now < cls.next_read:
            return
        cls.next_read = now + READ_INTERVAL
        provider = cls._find_provider(paused, new_world)
        if provider is None:
            cls.points, cls.types, cls.untracked, cls.givers, cls.read_ok = [], [], [], [], False
            return
        if not cls.checked and not cls._setup(pawn):
            return
        head = _read(provider._get_address() + 0x38, 16)  # noqa: SLF001
        if head is None:
            cls.points, cls.types, cls.untracked, cls.givers, cls.read_ok = [], [], [], [], False
            return
        data, count = struct.unpack_from("<Qi", head, 0)
        if not 0 <= count <= MAX_WAYPOINTS:
            cls._fail(f"odd waypoint count {count}")
            return
        entries = _read(data, count * 24) if count else b""
        if entries is None:
            cls.points, cls.types, cls.untracked, cls.givers, cls.read_ok = [], [], [], [], False
            return
        points: list[tuple[float, float, float]] = []
        types: list[str] = []
        untracked: list[bool] = []
        givers: list[tuple[float, float, float]] = []
        kind = other_kind = 0
        seen: dict[tuple[int, int], tuple[float, float, float] | None] = {}
        for i in range(count):
            waypoint, entry_id = struct.unpack_from("<QQ", entries, i * 24)
            # Givers stay put: each is read once while it's in the list (World_P has ~90)
            if (waypoint, entry_id) in cls.seen_givers:
                where = seen[waypoint, entry_id] = cls.seen_givers[waypoint, entry_id]
                if where is not None:
                    givers.append(where)
                continue
            head = _read(waypoint, 0xD0)
            if head is None or struct.unpack_from("<Q", head, 0)[0] != cls.waypoint_vtable:
                cls._fail("waypoint list layout changed")
                return
            if struct.unpack_from("<Q", head, 0x30)[0] != entry_id:
                cls._fail("waypoint ids don't match")
                return
            shared = struct.unpack_from("<Q", head, 0xC8)[0]
            data = _read(shared, DATA_SIZE) if shared else None
            if data is None:
                continue
            data_kind = struct.unpack_from("<I", data, DATA_KIND)[0]
            giver = data_kind == KIND_GIVER or struct.unpack_from("<Q", data, 0)[0] == cls.giver_vtable
            index, serial = struct.unpack_from("<ii", data, 0x38)
            target = cls._object_at(index) if index or serial else None
            where = None
            if target is not None and target[1] == serial and target[0]:
                where = cls._location(target[0])
            if where is None:
                where = _data_location(data)  # no actor (go to an area), or it's attached
            if giver:
                seen[waypoint, entry_id] = where
                if where is not None:
                    givers.append(where)
            elif where is not None and not any(_near(where, p) for p in points):
                points.append(where)
                objective = struct.unpack_from("<Q", data, 0)[0] == cls.objective_vtable
                types.append(cls.type_names.get(data[DATA_TYPE : DATA_TYPE + 8], "") if objective else "")
                other = not struct.unpack_from("<I", head, WAYPOINT_FLAGS)[0] & FLAG_TRACKED
                untracked.append(other)
                found = data_kind if data_kind in (KIND_SIDE, KIND_MAIN) else 0
                if other:
                    other_kind = other_kind or found
                else:
                    kind = kind or found
        cls.seen_givers = seen
        cls.points, cls.types, cls.untracked, cls.givers, cls.read_ok = points, types, untracked, givers, True
        cls.mission_kind = kind or other_kind

    @classmethod
    def clear(cls) -> None:
        cls.provider = WeakPointer()
        cls.points = []
        cls.types = []
        cls.untracked = []
        cls.givers = []
        cls.seen_givers = {}
        cls.read_ok = False
        cls.next_read = 0.0
