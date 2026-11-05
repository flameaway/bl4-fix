"""
The ECHO map's fixed icons (fast travel, echo logs, collectables, points of interest, ...), read live.

Why: the ECHO map's saved copy (mapdata.py) only changes when the map is opened, so an echo log you
picked up stayed blue (not the green `_completed` icon) and a place you found showed nowhere. The
game keeps these in one discovery provider, `GbxDiscoveryProvider_OakDiscoveryLocations`, alive with
the map closed and holding every location of the level (near or not). Everything here is a memory
read (objectives._read: a bad address gives None, never a crash); no game function is called.

The layout (this build; the vtables are checked, so a patch that moves them turns this off):

- Provider +0x38: a TArray of 24-byte entries {object, id, ...}. The object
  (GbxDiscoveryProviderObject_OakDiscoveryLocation) has its id at +0x30, its discovery state at
  +0x48 (0 not found yet, 2 found), its presentation at +0x50 (vtable RVA 0xB7446B0; the icon
  texture's package and asset FNames at +0x20 and +0x28: "package.asset"), and its place: a data
  object at +0xC8 (vtable RVA 0xB127A60, position: 3 doubles at +0xB8), or, when that's empty, an
  actor (FWeakObjectPtr at +0xD0: grapple points, silo sliders).
- The presentation is swapped when the icon changes (an echo log picked up: _completed).

What the map shows, checked in Cowbell_P against a snapshot of the map taken the same day: the
entries that have a presentation with an icon, exactly (5 of 129; none of the 116 not yet found,
none of the 4 found ones with an empty icon). An icon of the map's saved copy that came from this
provider sits on its entry's position (within 0.5 m in Cowbell_P), or away from it when the map moved
icons that stand close together apart (Cowbell_P, 2026-11-05: the bank, respec station, golden chest
and firmware station saved 17 to 38 m from their entries, a point of interest 62 m; so no distance
says which saved icon is which entry). What does: every saved icon with a picture the provider uses
is one of its entries (Cowbell_P: per picture, as many saved icons as live ones, or fewer: the copy
is older). `covers` drops those for the live ones.
"""

from __future__ import annotations

import ctypes
import struct
import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject, WeakPointer

from . import objectives
from .objectives import _read

READ_INTERVAL = 0.5  # s between reads (like objectives.READ_INTERVAL)
SEARCH_INTERVAL = 2.0  # s between searches for the provider when it isn't known (paused only)
PROVIDER_CLASS = "GbxDiscoveryProvider_OakDiscoveryLocations"
ENTRIES = 0x38
ENTRY = 24
MAX_ENTRIES = 8192
OBJECT_SIZE = 0xD8
OBJECT_ID, OBJECT_PRESENTATION, OBJECT_DATA, OBJECT_ACTOR = 0x30, 0x50, 0xC8, 0xD0
PRESENTATION_VTABLE = 0xB7446B0
PRESENTATION_SIZE = 0x30
ICON_PACKAGE, ICON_ASSET = 0x20, 0x28
DATA_VTABLE = 0xB127A60
DATA_SIZE = 0xD0
DATA_LOCATION = 0xB8
CELL = 100.0  # world units (1 m): a saved icon this close to an entry is that entry's (grid for `covers`)
BUCKET = 10000.0  # world units (100 m): grid of the fixed entries, so a read only visits those in range
READ_SLACK = 5000.0  # world units (50 m) read beyond the shown range: what comes into it next

Key = tuple[int, int]  # (object address, entry id): ids only grow, so a reused address is a new key
Place = tuple[float, float, float]


def _cell(x: float, y: float, size: float) -> tuple[int, int]:
    return int(x // size), int(y // size)


def _kind(texture: str) -> str:
    """An icon's picture, the same found or not (ico_collectables_echo_log[_completed], ..._dahl_cache[_complete])."""
    return texture.rsplit(".", 1)[-1].lower().removesuffix("_completed").removesuffix("_complete")


class Discovery:
    broken = ""  # why we gave up ("" = working)
    checked = False
    provider = WeakPointer()
    next_search = 0.0
    next_read = 0.0
    object_vtable = presentation_vtable = data_vtable = 0
    names: dict[bytes, str] = {}  # noqa: RUF012  # FName bytes -> string (each decoded once)
    entries = b""  # the entry list as last read (the grid below is rebuilt when it changes)
    places: dict[Key, Place] = {}  # noqa: RUF012  # the entries' positions (read once: they don't move)
    buckets: dict[tuple[int, int], list[tuple[Key, Place]]] = {}  # noqa: RUF012  # BUCKET squares -> placed entries
    others: list[Key] = []  # noqa: RUF012  # entries not placed yet (their actor isn't loaded): tried each read
    fixed_cells: set[tuple[int, int]] = set()  # noqa: RUF012  # `cells` of the grid
    cells: set[tuple[int, int]] = set()  # noqa: RUF012  # CELL squares holding any entry (found or not)
    pictures: set[str] = set()  # noqa: RUF012  # `_kind` of every icon of the level's entries seen so far
    icons: list[dict[str, Any]] = []  # noqa: RUF012  # what the map shows in range: {"x", "y", "z", "texture"}
    read_ok = False  # the last read went through (icons and cells are this level's, not just empty)

    @classmethod
    def _none(cls) -> None:
        cls.icons, cls.cells, cls.read_ok = [], set(), False

    @classmethod
    def _fail(cls, why: str) -> None:
        if not cls.broken:
            unrealsdk.logging.warning(f"[Minimap] live map icons off: {why}")
        cls.broken = why
        cls._none()

    @classmethod
    def _setup(cls) -> None:
        base = objectives._k32.GetModuleHandleW(None)  # noqa: SLF001
        cdo = unrealsdk.find_class("GbxDiscoveryProviderObject_OakDiscoveryLocation").ClassDefaultObject
        cls.object_vtable = objectives._u64(cdo._get_address()) or 0  # noqa: SLF001
        cls.presentation_vtable, cls.data_vtable = base + PRESENTATION_VTABLE, base + DATA_VTABLE
        cls.checked = True

    @classmethod
    def _find_provider(cls, paused: bool, new_world: bool) -> UObject | None:
        provider = cls.provider()
        if provider is not None:
            return provider
        now = time.monotonic()
        first = cls.next_search == 0.0
        if not first and not new_world and (not paused or now < cls.next_search):
            return None
        cls.next_search = now + SEARCH_INTERVAL
        for found in unrealsdk.find_all(PROVIDER_CLASS, exact=True):
            if "Default__" not in found._path_name():  # noqa: SLF001
                cls.provider = WeakPointer(found)
                return found
        return None

    @classmethod
    def _name(cls, raw: bytes) -> str:
        name = cls.names.get(raw)
        if name is None:
            tag = unrealsdk.make_struct("GameplayTag", TagName="None")
            ctypes.memmove(tag._get_address(), raw, 8)  # noqa: SLF001
            name = cls.names[raw] = str(tag.TagName)
        return name

    @classmethod
    def _object(cls, key: Key) -> bytes | None:
        """The entry's object, checked (a mismatch turns us off: `broken`)."""
        raw = _read(key[0], OBJECT_SIZE) if key[0] else None
        if raw is None:
            return None
        if struct.unpack_from("<Q", raw, 0)[0] != cls.object_vtable:
            cls._fail("location layout changed")
            return None
        if struct.unpack_from("<Q", raw, OBJECT_ID)[0] != key[1]:
            cls._fail("location ids don't match")
            return None
        return raw

    @classmethod
    def _fixed(cls, obj: bytes) -> Place | None:
        """The position of an entry placed by its data object (None: placed by an actor, or unreadable)."""
        data = struct.unpack_from("<Q", obj, OBJECT_DATA)[0]
        raw = _read(data, DATA_SIZE) if data else None
        if raw is None or struct.unpack_from("<Q", raw, 0)[0] != cls.data_vtable:
            return None
        return struct.unpack_from("<3d", raw, DATA_LOCATION)

    @classmethod
    def _actor_place(cls, obj: bytes) -> Place | None:
        live = objectives.Objectives
        index, serial = struct.unpack_from("<ii", obj, OBJECT_ACTOR)
        target = live._object_at(index) if index or serial else None  # noqa: SLF001
        if target is None or target[1] != serial or not target[0]:
            return None  # (its actor isn't loaded)
        return live._location(target[0])  # noqa: SLF001

    @classmethod
    def _rebuild(cls, entries: bytes) -> bool:
        """The grid of placed entries, for a new entry list (or new places). False if we gave up."""
        places: dict[Key, Place] = {}
        buckets: dict[tuple[int, int], list[tuple[Key, Place]]] = {}
        others: list[Key] = []
        for obj_at, entry_id, _ in struct.iter_unpack("<QQQ", entries):
            key = (obj_at, entry_id)
            place = cls.places.get(key)
            if place is None:
                obj = cls._object(key)
                if cls.broken:
                    return False
                place = cls._fixed(obj) if obj is not None else None
                icon = cls._icon(obj, (0.0, 0.0, 0.0)) if obj is not None else None
                if icon is not None:
                    cls.pictures.add(_kind(icon["texture"]))  # (read once per entry: the rest come with reads)
            if place is None:
                others.append(key)
                continue
            places[key] = place
            buckets.setdefault(_cell(place[0], place[1], BUCKET), []).append((key, place))
        cls.entries, cls.places, cls.buckets, cls.others = entries, places, buckets, others
        cls.fixed_cells = {_cell(p[0], p[1], CELL) for p in places.values()}
        return True

    @classmethod
    def _icon(cls, obj: bytes, place: Place) -> dict[str, Any] | None:
        """The entry's icon as the map shows it, or None (not found yet, or found with no icon)."""
        presentation = struct.unpack_from("<Q", obj, OBJECT_PRESENTATION)[0]
        look = _read(presentation, PRESENTATION_SIZE) if presentation else None
        if look is None or struct.unpack_from("<Q", look, 0)[0] != cls.presentation_vtable:
            return None
        asset = look[ICON_ASSET : ICON_ASSET + 8]
        if not any(asset):
            return None
        texture = f"{cls._name(look[ICON_PACKAGE : ICON_PACKAGE + 8])}.{cls._name(asset)}"
        return {"x": place[0], "y": place[1], "z": place[2], "texture": texture}

    @classmethod
    def update(cls, x: float, y: float, reach: float, *, paused: bool, new_world: bool) -> None:
        """
        Re-reads the icons within `reach` of (x, y) every READ_INTERVAL: only those are shown, and
        reading the whole list took 0.7 ms for Cowbell_P's 129 entries (World_P has ~860). An
        entry's position is read once. Read `icons` / `covers` afterwards.
        """
        now = time.monotonic()
        if cls.broken or now < cls.next_read:
            return
        cls.next_read = now + READ_INTERVAL
        live = objectives.Objectives
        provider = cls._find_provider(paused, new_world)
        if not live.checked or live.broken or provider is None:
            cls._none()
            return  # (objectives.py checks GUObjectArray: actor-placed entries need it)
        if not cls.checked:
            cls._setup()
        head = _read(provider._get_address() + ENTRIES, 12)  # noqa: SLF001
        array, count = struct.unpack("<Qi", head) if head else (0, -1)
        if not 0 <= count <= MAX_ENTRIES:
            cls._fail(f"odd entry count {count}")
            return
        entries = _read(array, count * ENTRY) if count else b""
        if entries is None:
            cls._none()
            return
        if entries != cls.entries and not cls._rebuild(entries):
            return
        far = reach + READ_SLACK
        r2 = far * far
        icons: list[dict[str, Any]] = []
        x0, x1 = int((x - far) // BUCKET), int((x + far) // BUCKET)
        y0, y1 = int((y - far) // BUCKET), int((y + far) // BUCKET)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= len(cls.buckets):
            squares = [cls.buckets.get((bx, by), ()) for bx in range(x0, x1 + 1) for by in range(y0, y1 + 1)]
        else:
            squares = list(cls.buckets.values())  # (a range wider than the level: every bucket once)
        for square in squares:
            for key, place in square:
                if (place[0] - x) ** 2 + (place[1] - y) ** 2 > r2:
                    continue
                obj = cls._object(key)
                icon = cls._icon(obj, place) if obj is not None else None
                if icon is not None:
                    icons.append(icon)
        placed = False
        for key in cls.others:
            obj = cls._object(key)
            if obj is None:
                continue
            where = cls._fixed(obj) if struct.unpack_from("<Q", obj, OBJECT_DATA)[0] else cls._actor_place(obj)
            if where is None:
                continue  # (its actor isn't loaded yet: tried again next read)
            # Actor-placed entries are fixtures (grapple points, silo sliders): kept like fixed ones
            cls.places[key] = where
            placed = True
            if (where[0] - x) ** 2 + (where[1] - y) ** 2 <= r2:
                icon = cls._icon(obj, where)
                if icon is not None:
                    icons.append(icon)
        if cls.broken:
            return
        if placed:
            cls.entries = b""  # (into the grid with the next read)
        cls.icons, cls.cells, cls.read_ok = icons, cls.fixed_cells, True
        cls.pictures.update(_kind(i["texture"]) for i in icons)

    @classmethod
    def covers(cls, icon: dict[str, Any]) -> bool:
        """A saved ECHO map icon that is one of this provider's entries (drawn live instead): one with
        a picture the provider uses, or one standing on an entry."""
        if _kind(icon["texture"]) in cls.pictures:
            return True
        cx, cy = _cell(icon["x"], icon["y"], CELL)
        return any((cx + dx, cy + dy) in cls.cells for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    @classmethod
    def near(cls, x: float, y: float, reach: float) -> list[dict[str, Any]]:
        r2 = reach * reach
        return [i for i in cls.icons if (i["x"] - x) ** 2 + (i["y"] - y) ** 2 <= r2]

    @classmethod
    def clear(cls) -> None:
        cls.provider = WeakPointer()
        cls.entries, cls.places, cls.buckets, cls.others, cls.fixed_cells = b"", {}, {}, [], set()
        cls.pictures = set()
        cls._none()
        cls.next_read = 0.0
