"""
The mission waypoints the ECHO map shows, read live (no map opening needed), with the map's own rules.

Why: the game's discovery provider the HUD uses (`GbxDiscoveryProvider_OakMissionWaypoints`, which
objectives.py reads) leaves out mission waypoints you're not near: a Rift Champion across the map
isn't in it. Opening the ECHO map makes a second provider of the same class for the map's viewer,
and that one has them all. Both are filled by the same code from the same source; only the viewer's
kind differs. So we read that source and apply the map's rules ourselves. Everything here is a
memory read (ReadProcessMemory through objectives._read: a bad address gives None, never a crash);
no game function is called.

What the game does (found by disassembling this build; RVAs below):

- The provider's rebuild (0x60E596) takes the source: OakShard (one, in OakSingletons) +0x178,
  then +0x140 (vtable RVA 0xB96BD70). It asks the source for its mission waypoint set (virtual
  +0x68: a copy of the source's own set at +0x20) and hands it to the sync (0x4A0355A).
- The set (a TSparseArray: data, num, max; allocation bits inline at +0x10 or at +0x20, their
  count at +0x28) holds 0x28-byte elements: a mission key (12 bytes), a record at +0x10 and its
  shared-pointer counter at +0x18. A record's entries are a sparse array at +0x10 of the same
  shape: entry +0x10 is a location holder whose level (0x60EDEE) must be the provider's level
  (provider +0x28), and whose +0xA0 is a TArray of shared pointers to waypoint data objects (the
  type objectives.py reads: vtable RVA 0xB400BE0, position at +0x50, mission type FName at +0x78).
- A waypoint whose flags have bit 2 is left out (virtual +0x20: the word at
  [[data+0x68]+0x38]+0xC). So is every waypoint of a mission whose kind the map hides: a mission's
  kind is one index into a 37-entry registry (RVA 0xCB86C40; entry i, 0x18 bytes, starts with the
  kind's FName), and the hidden kinds are a bit set ([RVA 0xCBCD5C8] +0x100 words, +0x108 count).
  World_P 2026-11-05: only DynamicEvent (index 6) is hidden. We read a waypoint's kind from its own
  mission type FName (equal to its mission's kind in every sample).
- Then the viewer's kind: the HUD's (2) keeps only what's near or tracked; the map's keeps all of
  the above. We keep all of it, as the map does.

Two of the sync's further checks need game calls (the data's virtual +0x48, and the mission
object's +0x50); they're left out. World_P 2026-11-05 with the map open, the rules above gave
exactly its list: 2 crawlers and 2 Rift Champions (ico_activity_waypoint, ico_world_events), none
of the 20 DynamicEvents, not the Rift Champion just finished (flag bit 2).

Every code site this relies on is checked against the bytes it had when this was written; if any
differ (a game patch), `broken` is set and the minimap uses the ECHO map's saved icons as before.

The picture the map gives a waypoint is the game's too (`pictures`). `OakDiscoveryGlobals`, a static
struct (RVA below), maps a mission kind to a discovery location type
(MissionWaypointLocationType_Default_SubTypes: {kind FName, GbxDefPtr {name, struct, def}} entries);
the location type's DiscoveryStatePresentations[2] (found) is a presentation def with the icon (the
layout discovery.py reads). Cowbell_P 2026-11-05: riftboss -> DiscoveryLocation_Primary_RiftBossWaypoint
-> ico_dlc_rift_boss_totem, the ECHO map's icon. A GbxDefPtr's def is filled in when the game first
uses it (6 of the 31 kinds then, every kind with a waypoint among them); until then there's no picture.
The struct pointers in the GbxDefPtrs are checked, so a patch that moves the struct turns this off.
"""

from __future__ import annotations

import struct
import threading
import time

import unrealsdk
from unrealsdk.unreal import UObject, WeakPointer

from . import discovery, objectives
from .objectives import _data_location, _read, _u64

READ_INTERVAL = 0.5  # s between reads (like objectives.READ_INTERVAL)
SEARCH_INTERVAL = 2.0  # s between searches for OakShard, when it isn't known yet
SHARD_SOURCE, SOURCE_SET = 0x178, 0x140  # OakShard +0x178, then +0x140: the waypoint source
SOURCE_VTABLE = 0xB96BD70
WAYPOINTS = 0x20  # the source's mission waypoint set
ELEMENT = 0x28  # set and record entries: {key 12 bytes, ..., object at +0x10, counter at +0x18}
RECORD_ENTRIES = 0x10  # a record's sparse array of entries
ENTRY_HOLDER = 0x10  # an entry's location holder
HOLDER_WAYPOINTS = 0xA0  # the holder's TArray of shared pointers to waypoint data
WAYPOINT_DATA_VTABLE = 0xB400BE0
DATA_TYPE = objectives.DATA_TYPE  # (0x78) the mission type FName
DATA_TARGET = 0x38  # FWeakObjectPtr to the target actor (zero: no actor)
FLAG_HIDDEN = 2
PROVIDER_LEVEL = 0x28  # a discovery provider's level FName (both providers: this map's)
HIDDEN_KINDS = 0xCBCD5C8  # pointer to the hidden-kinds holder: +0xF8 registry, +0x100 words, +0x108 count
KIND_REGISTRY = 0xCB86C40
KIND_ENTRY = 0x18
# Globals the level lookup (0x60EDEE) uses: an FName number to skip, the "no level" default
NAME_SKIP, NAME_DEFAULT, NAME_DEFAULT_NUMBER, NAME_SPECIAL = 0xC948000, 0xC948618, 0xC948620, 0xC948034
MAX_SLOTS = 100000  # a sanity bound on sparse array sizes
MAX_LEVELS = 4096  # MapWaypoints.levels is started over past this many
DISCOVERY_GLOBALS = 0xCBC83A8  # the static OakDiscoveryGlobals
SUBTYPE_ENTRY = 0x28  # a kind -> location type entry: {kind FName, GbxDefPtr {name, struct, def}, hash link}
DEF_PTR = 0x18  # a GbxDefPtr: {name FName, struct, def}
FOUND = 2  # the discovery state whose presentation the map shows for a waypoint
MAX_KINDS = 64

# (RVA, bytes) of the code this follows, as they were in this build (see the docstring)
CODE = (
    (0x40488, "488b8178010000c3"),  # OakShard +0x3A0: return [this+0x178]
    (0x60e680, "4c8bb040010000"),  # rebuild: source = [x+0x140]
    (0x60e69a, "498b06488dbc24800000004c89f14889da4989f8ff5048498b06488d9c24a00000004c89f14889daff5068"
     "498b064c8d7c24704c89f14c89faff50584889f14889da4989f84d89f9e8734e"),  # rebuild: source lists, sync
    (0x4a036d6, "e8e51acdfb"),  # sync: the hidden mission kinds (0x6d51c0)
    (0x4a03815, "488d93f0040000"),  # sync: a mission's kind at +0x4F0
    (0x4a0384c, "498b8424f8000000483b8424f801000048899c2408010000757949638424080100008b8c240802000039c8"
     "4189c9440f4cc84585c97e2b488b9424000200004d8b8424000100004589c94531d24f8b1cd049f7d34e851cd2753949ff"
     "c24d39d175eb"),  # sync: kinds compared with the hidden ones
    (0x4a0396b, "488d4e20448b46384585c048898c24180100000f8493130000488b46304885c0"),  # sync: a record's entries
    (0x4a03a23, "4c8b761048984c8d3c804b8b4cfe10488d9424b0010000"),  # sync: entry +0x10, its level (0x60edee)
    (0x4a03a66, "4c89c0488b8c2488000000483b41280f852c0200004b8d04fe4883c010488b004c63b0a80000004d85f60f84"
     "11020000488ba8a0000000"),  # sync: level compared, the entry's waypoints at +0xA0
    (0x4a03ae2, "ff502041b701a8027511488b064889f1ff504884c0"),  # sync: waypoint flags (+0x20) bit 2, +0x48
    (0x4a03b15, "80bc24e800000000"),  # sync: viewer kind (the map's keeps everything)
    (0x60edee, "4889d080797801750c8b5150895008488b4948eb3d488b4918448b41304585c0743a8b513485d27433443b05e2"
     "91330c742a83fa017f06837954007e19488b494883fa0174167e148b5114895008488b490c488908c34883c138ebe58b0d"
     "d097330c894808488b0dbe97330cebe4"),  # the level of a holder
    (0x1f92c6, "488b71704885f674318b460885c0742a8d5001f00fb1560875f2488b41684885c0741b488b40380fb7780c"),  # flags
    (0x6d51d4, "488b35ed834f0c"),  # the hidden kinds: [0xCBCD5C8]
    (0x1004a7, "4c8b49084c8b024d85c975074c8941084d89c131c04d85c974534d39c1754e8b7208b8ffffffff4839c674"),  # kinds
)  # fmt: skip


def _i32(address: int) -> int | None:
    raw = _read(address, 4)
    return struct.unpack("<i", raw)[0] if raw else None


def _alive(counter: int | None) -> bool:
    """A shared pointer's reference controller still holds the object (strong count > 0)."""
    count = _i32(counter + 8) if counter else None
    return bool(count and count > 0)


def _slots(array: int) -> tuple[int, list[int]]:
    """The data pointer and allocated indices of a TSparseArray at `array` (data, num, max, bits
    inline/heap, bit count). Inline bits (up to 128 slots) come with the head's read."""
    head = _read(array, 0x2C)
    if head is None:
        return 0, []
    data, num, cap = struct.unpack_from("<Qii", head, 0)
    heap_bits = struct.unpack_from("<Q", head, 0x20)[0]
    count = struct.unpack_from("<i", head, 0x28)[0]
    if not data or num <= 0 or not 0 < count <= cap <= MAX_SLOTS:
        return 0, []
    size = (count + 7) // 8
    bits = head[0x10 : 0x10 + size] if not heap_bits and size <= 0x10 else _read(heap_bits or array + 0x10, size)
    if bits is None:
        return 0, []
    return data, [i for i in range(count) if bits[i >> 3] >> (i & 7) & 1]


class MapWaypoints:
    broken = ""  # why we gave up ("" = working)
    base = 0
    checked = False
    shard = WeakPointer()
    next_search = 0.0
    next_read = 0.0
    kinds: dict[bytes, int] = {}  # noqa: RUF012  # mission kind FName bytes -> registry index
    # What the map shows: (x, y, z, mission type: lowercase objectives.MISSION_TYPES, "" unknown)
    points: list[tuple[float, float, float, str]] = []  # noqa: RUF012
    read_ok = False  # the last read went through (points is the map's list, not just empty)
    pictures: dict[str, str] = {}  # noqa: RUF012  # mission type -> the map's picture for its waypoints
    picture_broken = ""  # why pictures are off ("" = working or not set up yet)
    picture_layout: tuple[int, int, frozenset[int]] | None = None  # (subtypes map, found presentation, structs)
    # A holder's level info -> its level FName bytes (_level): neither changes while the level is
    # loaded (3 reads a holder, ~15 holders, each read before)
    levels: dict[int, bytes | None] = {}  # noqa: RUF012
    # The worker: one request waiting at most (a newer one replaces it), its answer a whole-object swap
    asked: tuple[int, int, int] | None = None  # (ticket, shard address, provider address)
    answer: tuple[int, list[tuple[float, float, float, str]] | None] | None = None  # (ticket, points)
    ticket = 0
    taken = 0  # answers to older tickets were asked in a world since left: dropped
    wake = threading.Event()
    generation = 0  # a worker of an older generation stops (mod disabled, reloaded)
    thread: threading.Thread | None = None

    @classmethod
    def _fail(cls, why: str) -> None:
        if not cls.broken:
            unrealsdk.logging.warning(f"[Minimap] map waypoints off: {why}")
        cls.broken = why
        cls.points, cls.read_ok = [], False

    @classmethod
    def _setup(cls) -> bool:
        cls.base = objectives._k32.GetModuleHandleW(None)  # noqa: SLF001
        for rva, code in CODE:
            if _read(cls.base + rva, len(code) // 2) != bytes.fromhex(code):
                cls._fail(f"game code changed at {rva:#x} (a patch?)")
                return False
        cls.checked = True
        return True

    @classmethod
    def _picture_setup(cls) -> bool:
        """The offsets of the picture lookup, from reflection; the static struct checked by its pointers."""
        globals_struct = unrealsdk.find_object("ScriptStruct", "/Script/OakGame.OakDiscoveryGlobals")
        location = unrealsdk.find_object("ScriptStruct", "/Script/OakGame.OakDiscoveryLocationTypeDef")
        gbx_location = unrealsdk.find_object("ScriptStruct", "/Script/GbxGame.GbxDiscoveryLocationTypeDef")
        default = globals_struct._find_prop("MissionWaypointLocationType_Default").Offset_Internal  # noqa: SLF001
        subtypes = globals_struct._find_prop("MissionWaypointLocationType_Default_SubTypes").Offset_Internal  # noqa: SLF001
        states = location._find_prop("DiscoveryStatePresentations")  # noqa: SLF001
        structs = frozenset(s._get_address() for s in (location, gbx_location))  # noqa: SLF001
        head = _read(cls.base + DISCOVERY_GLOBALS + default, DEF_PTR)
        if states.ArrayDim <= FOUND or head is None or struct.unpack_from("<Q", head, 8)[0] not in structs:
            cls.picture_broken = "discovery globals not where expected (a patch?)"
            unrealsdk.logging.warning(f"[Minimap] waypoint pictures off: {cls.picture_broken}")
            return False
        cls.picture_layout = (subtypes, states.Offset_Internal + FOUND * states.ElementSize, structs)
        return True

    @classmethod
    def _read_pictures(cls, wanted: set[str]) -> None:
        """The pictures of the mission types in `wanted` that the game has filled in (each read once)."""
        if cls.picture_layout is None and (cls.picture_broken or not cls._picture_setup()):
            return
        assert cls.picture_layout is not None
        subtypes, found, structs = cls.picture_layout
        subtypes_at = cls.base + DISCOVERY_GLOBALS + subtypes
        data, slots = _slots(subtypes_at)
        entries = _read(data, (max(slots) + 1) * SUBTYPE_ENTRY) if data and slots and max(slots) < MAX_KINDS else None
        if entries is None:
            return
        names = objectives.Objectives.type_names
        name_of = discovery.Discovery._name  # noqa: SLF001
        presentation_vtable = cls.base + discovery.PRESENTATION_VTABLE
        for i in (slot * SUBTYPE_ENTRY for slot in slots):
            kind = names.get(entries[i : i + 8])
            if kind not in wanted:
                continue
            location_struct, location = struct.unpack_from("<QQ", entries, i + 16)
            if location_struct not in structs or not location:
                continue  # (not filled in by the game yet)
            presentation = _u64(location + found + 16)
            look = _read(presentation, discovery.PRESENTATION_SIZE) if presentation else None
            if look is None or struct.unpack_from("<Q", look, 0)[0] != presentation_vtable:
                continue
            asset = look[discovery.ICON_ASSET : discovery.ICON_ASSET + 8]
            if any(asset):
                cls.pictures[kind] = f"{name_of(look[discovery.ICON_PACKAGE : discovery.ICON_PACKAGE + 8])}.{name_of(asset)}"

    @classmethod
    def _shard(cls, paused: bool, new_world: bool) -> int | None:
        """The OakShard's address (game thread: it's looked up through the SDK)."""
        shard = cls.shard()
        if shard is None:
            now = time.monotonic()
            first = cls.next_search == 0.0
            if not first and not new_world and (not paused or now < cls.next_search):
                return None
            cls.next_search = now + SEARCH_INTERVAL
            for found in unrealsdk.find_all("OakShard", exact=True):
                if "Default__" not in found._path_name():  # noqa: SLF001
                    cls.shard = WeakPointer(found)
                    shard = found
                    break
            if shard is None:
                return None
        return shard._get_address()  # noqa: SLF001

    @classmethod
    def _hidden_kinds(cls) -> tuple[int, ...] | None:
        """The bit set of mission kinds the map hides, after checking it uses the registry we read."""
        holder = _u64(cls.base + HIDDEN_KINDS)
        head = _read(holder + 0xF8, 0x14) if holder else None
        if head is None:
            return None
        registry, words, count = struct.unpack_from("<QQi", head, 0)
        if registry != cls.base + KIND_REGISTRY or not 0 <= count <= 64:
            return None
        raw = _read(words, count * 8) if count else b""
        if raw is None:
            return None
        if not cls.kinds:
            data, slots = _slots(registry)
            for i in slots:
                name = _read(data + i * KIND_ENTRY, 8) if data else None
                if name:
                    cls.kinds[name] = i
        return struct.unpack(f"<{count}Q", raw)

    @classmethod
    def _level(cls, holder: int, skip: int | None, levels: dict[int, bytes | None]) -> bytes | None:
        """The level FName (8 bytes) of a location holder: function 0x60EDEE, as reads. `skip`: the
        NAME_SKIP global. Found through the holder's level info: kept by info (`levels`)."""
        raw = _read(holder, 0x80)
        if raw is None:
            return None
        if raw[0x78] == 1:
            name, number = struct.unpack_from("<Qi", raw, 0x48)
        else:
            info = struct.unpack_from("<Q", raw, 0x18)[0]
            if info not in levels:
                levels[info] = cls._info_level(info, skip)
            return levels[info]
        out = struct.pack("<Qi", name, number)
        return out[4:12] if out[0:4] != b"\0\0\0\0" else None

    @classmethod
    def _info_level(cls, info: int, skip: int | None) -> bytes | None:
        """_level's lookup through a holder's level info (3 reads)."""
        name = number = None
        head = _read(info, 0x58) if info else None
        if head is not None:
            index, count = struct.unpack_from("<ii", head, 0x30)
            if index and count > 1 and index != skip:
                where = struct.unpack_from("<Q", head, 0x48)[0]
                found = _read(where + 0xC, 0xC) if where else None
                if found is not None:
                    name, number = struct.unpack_from("<Qi", found, 0)
        if name is None:
            name, number = _u64(cls.base + NAME_DEFAULT), _i32(cls.base + NAME_DEFAULT_NUMBER)
            if name is None or number is None:
                return None
        out = struct.pack("<Qi", name, number)
        return out[4:12] if out[0:4] != b"\0\0\0\0" else None

    @classmethod
    def update(cls, pawn: UObject, *, paused: bool, new_world: bool) -> None:
        """
        Every READ_INTERVAL, asks the worker to read the map's waypoints again; takes its answer
        (`points`, `read_ok`) on a later call. The worker only reads memory (~250 system calls:
        0.7-2 ms a read on the game thread, 2026-11-06); the SDK lookups stay here.
        """
        answer = cls.answer
        if answer is not None:
            cls.answer = None
            if answer[0] >= cls.taken:  # (not asked in a world since left)
                points = answer[1]
                cls.points, cls.read_ok = (points, True) if points is not None else ([], False)
                wanted = {p[3] for p in cls.points if p[3]} - cls.pictures.keys()
                if wanted:
                    cls._read_pictures(wanted)
        now = time.monotonic()
        if cls.broken or now < cls.next_read:
            return
        cls.next_read = now + READ_INTERVAL
        if new_world:
            cls.taken = cls.ticket + 1
            cls.levels = {}
            cls.points = []
        live = objectives.Objectives
        provider = live.provider()
        if not live.checked or live.broken or provider is None:
            cls.points, cls.read_ok = [], False
            return  # (objectives.py checks GUObjectArray and finds the provider: we use both)
        if not cls.checked and not cls._setup():
            return
        shard = cls._shard(paused, new_world)
        if shard is None:
            cls.points, cls.read_ok = [], False
            return
        cls.ticket += 1
        cls.asked = (cls.ticket, shard, provider._get_address())  # noqa: SLF001
        if cls.thread is None:
            cls.wake = threading.Event()  # (each worker its own: an old one can't take a new one's wake-up)
            cls.thread = threading.Thread(target=cls._run, args=(cls.generation, cls.wake), name="bl4_minimap waypoints", daemon=True)
            cls.thread.start()
        cls.wake.set()

    @classmethod
    def _run(cls, generation: int, wake: threading.Event) -> None:
        while True:
            wake.wait()
            wake.clear()
            if generation != cls.generation:
                return
            job, cls.asked = cls.asked, None
            if job is not None:
                cls.answer = (job[0], cls._points(job[1], job[2]))

    @classmethod
    def _points(cls, shard: int, provider: int) -> list[tuple[float, float, float, str]] | None:
        """The waypoints the map shows (worker: memory reads only); None if they can't be read."""
        holder = _u64(shard + SHARD_SOURCE)
        source = _u64(holder + SOURCE_SET) if holder else None
        if not source or _u64(source) != cls.base + SOURCE_VTABLE:
            return None  # (not there yet: loading)
        hidden = cls._hidden_kinds()
        level = _read(provider + PROVIDER_LEVEL, 8)
        if hidden is None or level is None:
            return None
        special = _read(cls.base + NAME_SPECIAL, 8)
        skip = _i32(cls.base + NAME_SKIP)
        levels = cls.levels  # (this world's: update swaps in a new one for the next)
        if len(levels) > MAX_LEVELS:
            levels = cls.levels = {}
        data_vtable = cls.base + WAYPOINT_DATA_VTABLE
        points: list[tuple[float, float, float, str]] = []
        # (Each sparse array's elements in one read: a read is a system call, ~4 us)
        elements, slots = _slots(source + WAYPOINTS)
        table = _read(elements, (slots[-1] + 1) * ELEMENT) if slots else None
        for i in slots if table else ():
            record, counter = struct.unpack_from("<QQ", table, i * ELEMENT + 0x10)
            if not record or not _alive(counter):
                continue
            entries, rows = _slots(record + RECORD_ENTRIES)
            holders = _read(entries, (rows[-1] + 1) * ELEMENT) if rows else None
            for j in rows if holders else ():
                holder = struct.unpack_from("<Q", holders, j * ELEMENT + ENTRY_HOLDER)[0]
                name = cls._level(holder, skip, levels) if holder else None
                if name is None or name != level or name == special:
                    continue  # another map's (or one we can't place)
                head = _read(holder + HOLDER_WAYPOINTS, 12)
                if head is None:
                    continue
                array, count = struct.unpack_from("<Qi", head, 0)
                pointers = _read(array, count * 16) if array and 0 < count < 1000 else None
                for k in range(count if pointers else 0):
                    data_at, data_counter = struct.unpack_from("<QQ", pointers, k * 16)
                    if not data_at or not _alive(data_counter):
                        continue
                    point = cls._point(data_at, data_vtable, hidden)
                    if point is not None and not any(objectives._near(point[:3], p[:3]) for p in points):  # noqa: SLF001
                        points.append(point)
        return points

    @classmethod
    def _point(cls, data_at: int, data_vtable: int, hidden: tuple[int, ...]) -> tuple[float, float, float, str] | None:
        """A waypoint the map shows: its position and mission type; None if the map leaves it out."""
        data = _read(data_at, 0x80)
        if data is None or struct.unpack_from("<Q", data, 0)[0] != data_vtable:
            return None
        # Flags (the data's virtual +0x20): the word at [[data+0x68]+0x38]+0xC, while +0x70 holds it
        owner, owner_counter = struct.unpack_from("<QQ", data, 0x68)
        info = _u64(owner + 0x38) if owner and _alive(owner_counter) else None
        flags = _read(info + 0xC, 2) if info else None
        if flags is None or struct.unpack("<H", flags)[0] & FLAG_HIDDEN:
            return None
        kind_name = data[DATA_TYPE : DATA_TYPE + 8]
        kind = cls.kinds.get(kind_name)
        if kind is None or (kind >> 6 < len(hidden) and hidden[kind >> 6] >> (kind & 63) & 1):
            return None  # a kind the map hides (no kind: an empty set, which the map hides too)
        live = objectives.Objectives
        where = None
        index, serial = struct.unpack_from("<ii", data, DATA_TARGET)
        target = live._object_at(index) if index or serial else None  # noqa: SLF001
        if target is not None and target[1] == serial and target[0]:
            where = live._location(target[0])  # noqa: SLF001
        if where is None:
            where = _data_location(data)
        if where is None:
            return None
        return (*where, live.type_names.get(kind_name, ""))

    @classmethod
    def clear(cls) -> None:
        """Stops the worker (it ends after a read in progress) and drops what it was asked."""
        cls.generation += 1
        cls.asked = cls.answer = None
        cls.thread = None
        cls.wake.set()
        cls.taken = cls.ticket + 1
        cls.shard = WeakPointer()
        cls.levels = {}
        cls.points, cls.read_ok = [], False
        cls.next_read = 0.0
