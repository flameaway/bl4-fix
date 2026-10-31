"""
The game's item pictures (inventory thumbnails), kept ready.

How the game does it (RE'd 2026-10-29 on the 2026-10-22 exe; see CLAUDE.md):
- `OakThumbnailManager` (a game instance subsystem, nothing reflected) renders an item's serial in
  its own little scene into a 512x256 render target (`OakInventoryThumbnail_*`, 0.5 MiB of video
  memory each as the engine counts it) and keeps them in a map {serial, size} -> render target (manager+0x48).
  A card asks it with GetThumbnail (a map lookup) and, when missing, RequestThumbnail.
- Renders are slow on purpose: one per `RenderInterval` (1/15 s), one per tick, each after
  `NumPreRenderItemThumbnails` (5) frames, plus the item's asset loads. Nothing renders ahead of
  time (`EnablePrerendering` is read by no code at all).
- The sweep keeps only thumbnails still waiting in the render queue: every other picture goes,
  whenever the map holds more than `MaxThumbnails` (100), and every `MaxSweepInterval` (300 s)
  anyway. A backpack plus a bank is over 100, so the game kept throwing everything away and
  rendering it again.

What we do:
- `MaxSweepInterval` -> never, `MaxThumbnails` -> what we keep + room for everything else
  (loot, vendors), so the game no longer wipes the cache.
- Warm it: for each item in the backpack (equipped first; the bank too with the option) that has
  no picture, ask the manager ourselves, through the same native request the cards use, with the
  same size (the cvar's own storage, so the picture is exactly the one a card would get). One at a
  time, only while the game has nothing of its own queued.
- When the other pictures (loot, vendors) pile up, sweep at a quiet moment (no menu up) and warm
  ours again, rather than letting the game wipe everything in the middle of a menu.

Game thread only (unreal calls, the native request). Memory reads go through ReadProcessMemory, so
a stale pointer reads as nothing instead of crashing.
"""

from __future__ import annotations

import ctypes
import struct
import time
from dataclasses import dataclass, field
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

# region Pins (the exe of 2026-10-22; each checked by its bytes before anything is called)

REQUEST_RVA = 0x11BD35E  # RequestThumbnail(manager, out ThumbnailID*, const FString* serial, const FIntPoint* size)
REQUEST_HEAD = bytes.fromhex("41574156415541545657534881ecd00000004c89cb4889d6")
# A card's wrapper: RequestThumbnail(manager, out, serial, &TextureSize), TextureSize at SIZE_RVA
CALLER_RVA = 0x11BCE48
CALLER_HEAD = bytes.fromhex("564883ec204889d64c8d0de177a20be802050000")
SIZE_RVA = 0xCBE4638  # FIntPoint: the storage of OakUI.Thumbnails.TextureSizeX / Y
MANAGER_VTABLE_RVA = 0xB7FCB60

# OakThumbnailManager
RENDERED = 0x48  # TMap<ThumbnailID, render target>: TSparseArray data pointer, ArrayNum at +8
RENDERED_FREE = 0x7C  # the sparse array's NumFreeIndices (live entries = ArrayNum - this)
LOADING = 0x100  # TArray, 0x50-byte entries: requests whose item assets are loading
QUEUE = 0x110  # TArray, 0x38-byte entries: loaded, waiting for their render
ELEMENT_SIZE = 0x48  # {FString serial; FIntPoint size; ...; UTextureRenderTarget2D* @+0x38; hash links}
ELEMENT_SIZE_AT = 0x10
ELEMENT_TARGET = 0x38
MAX_ELEMENTS = 8192

# FInventoryIdentity: the serial's text pointer and length (as Reroll reads them)
SERIAL_PTR = 0xA0
SERIAL_LEN = 0xB0
MAX_SERIAL = 4096
EQUIPPED_FLAG = 1

# endregion

CVAR = "OakUI.Thumbnails."
NEVER_S = 1.0e9  # MaxSweepInterval: the periodic wipe, pushed out of any session
GAME_MAX = 100  # the game's own MaxThumbnails
ROOM = 100  # pictures we leave for everything else (loot, vendors, other cards) on top of ours
SWEEP_AT = 75  # other pictures: sweep at the next quiet moment once there are this many
SWEEP_WAIT_S = 15.0  # the game sweeps once its timer is past MinSweepInterval (10 s)
TARGET_BYTES = 512 * 1024  # one thumbnail, as the engine counts it (Texture.Blueprint_GetMemorySize, 2026-10-29)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetModuleHandleW.restype = ctypes.c_void_p
_k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
_k32.ReadProcessMemory.restype = ctypes.c_int
_PROCESS = _k32.GetCurrentProcess()
_REQUEST_TYPE = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def log(msg: str) -> None:
    logging.info(f"[Instant Item Pictures] {msg}")


def _read(address: int, size: int) -> bytes | None:
    if not 0x10000 <= address < 0x7FFFFFFFFFFF or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_PROCESS, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)) or got.value != size:
        return None
    return buffer.raw


# region Console variables


class Cvars:
    original: dict[str, float] = {}  # noqa: RUF012 - name -> the game's value, read before our first change
    ours: dict[str, float] = {}  # noqa: RUF012 - name -> what we last set
    failed: set[tuple[str, float]] = set()  # noqa: RUF012
    kismet: Any = None


def _kismet() -> Any:
    if Cvars.kismet is None:
        Cvars.kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject  # lives as long as the game
    return Cvars.kismet


def cvar(name: str) -> float:
    return float(_kismet().GetConsoleVariableFloatValue(CVAR + name))


def original(name: str) -> float:
    """The game's own value of a cvar (read before our first change)."""
    if name not in Cvars.original:
        Cvars.original[name] = cvar(name)
    return Cvars.original[name]


def set_cvar(name: str, value: float) -> bool:
    """Sets one of the thumbnail cvars (remembering the game's value first). True if it took."""
    if Cvars.ours.get(name) == value:
        return True
    if (name, value) in Cvars.failed:
        return False
    original(name)
    if not _execute(name, value):
        Cvars.failed.add((name, value))  # not tried again this session (a console command per tick)
        log(f"couldn't set {CVAR}{name} to {value}")
        return False
    Cvars.ours[name] = value
    return True


def _execute(name: str, value: float) -> bool:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return False
    text = str(int(value)) if float(value).is_integer() else f"{value:.4f}"
    _kismet().ExecuteConsoleCommand(world, f"{CVAR}{name} {text}", get_pc(possibly_loading=True))
    return abs(cvar(name) - value) <= 1e-3 * max(1.0, abs(value))


def restore_cvars() -> None:
    """Puts back every cvar we changed."""
    for name in list(Cvars.ours):
        del Cvars.ours[name]
        try:
            if not _execute(name, Cvars.original[name]):
                logging.error(f"[Instant Item Pictures] couldn't put {CVAR}{name} back")
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Instant Item Pictures] couldn't put {CVAR}{name} back: {ex!r}")


# endregion

# region The manager


class Native:
    checked = False
    why: str | None = None  # why the native request can't be used (None: it can)
    base = 0
    request: Any = None
    manager: WeakPointer | None = None
    keep: list[Any] = []  # noqa: RUF012 - the last out buffers the game wrote into


def _check() -> str | None:
    base = int(_k32.GetModuleHandleW(None) or 0)
    if not base:
        return "no game image"
    if _read(base + REQUEST_RVA, len(REQUEST_HEAD)) != REQUEST_HEAD:
        return "the thumbnail request isn't where this build had it (game patched?)"
    if _read(base + CALLER_RVA, len(CALLER_HEAD)) != CALLER_HEAD:
        return "the card's thumbnail call isn't where this build had it (game patched?)"
    size = _read(base + SIZE_RVA, 8)
    if size is None or struct.unpack("<ii", size) != (int(cvar("TextureSizeX")), int(cvar("TextureSizeY"))):
        return "the thumbnail size isn't where this build had it (game patched?)"
    Native.base = base
    Native.request = _REQUEST_TYPE(base + REQUEST_RVA)
    return None


def native_problem() -> str | None:
    """Why we can't ask for pictures ourselves (checked once a session); None if we can."""
    if not Native.checked:
        Native.checked = True
        Native.why = _check()
        if Native.why:
            log(f"no warming: {Native.why}")
    return Native.why


def manager() -> Any:
    """The game instance's thumbnail manager (cached; it lives as long as the game)."""
    found = Native.manager() if Native.manager else None
    if found is not None:
        return found
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return None
    found = unrealsdk.find_class("SubsystemBlueprintLibrary").ClassDefaultObject.GetGameInstanceSubsystem(
        pc, unrealsdk.find_class("OakThumbnailManager"))  # fmt: skip
    if found is None:
        return None
    if Native.base and _read(found._get_address(), 8) != struct.pack("<Q", Native.base + MANAGER_VTABLE_RVA):  # noqa: SLF001
        Native.why = "the thumbnail manager isn't the class this build had"
        return None
    Native.manager = WeakPointer(found)
    return found


@dataclass
class Counts:
    pictures: int  # finished thumbnails in the map
    loading: int
    queued: int


def counts(address: int) -> Counts | None:
    # one read for all three (each ReadProcessMemory hands the GIL to any waiting thread)
    raw = _read(address + RENDERED, QUEUE + 12 - RENDERED)
    if raw is None:
        return None
    number, free, loading, queued = (struct.unpack_from("<I", raw, at - RENDERED)[0]
                                     for at in (RENDERED + 8, RENDERED_FREE, LOADING + 8, QUEUE + 8))  # fmt: skip
    return Counts(number - free, loading, queued)


class Names:
    # (text pointer, length, render target) of a live map entry -> its serial. Reading every
    # serial's text again (one ReadProcessMemory each) cost 1.3 ms a refresh at 250 pictures.
    # Rebuilt from the live entries each refresh, so it never holds more than the map.
    seen: dict[tuple[int, int, int], str] = {}  # noqa: RUF012


def forget_names() -> None:
    """After entries were removed: a freed text (or render target) could be reused by another one."""
    Names.seen = {}
    Paths.by_target = {}


def rendered(address: int, size: tuple[int, int]) -> set[str] | None:
    """The serials the manager holds a finished picture of, at this size."""
    head = _read(address + RENDERED, 16)
    if head is None:
        return None
    array, number = struct.unpack_from("<QI", head)
    if not 0 <= number <= MAX_ELEMENTS:
        return None
    if number == 0:
        Names.seen = {}
        return set()
    raw = _read(array, number * ELEMENT_SIZE)
    if raw is None:
        return None
    want_size = struct.pack("<ii", *size)
    seen, old = {}, Names.seen
    for at in range(0, len(raw), ELEMENT_SIZE):
        text, length = struct.unpack_from("<Qi", raw, at)
        # free slots: their first 16 bytes are free-list links, which don't read as a serial
        if not 3 <= length <= MAX_SERIAL or raw[at + ELEMENT_SIZE_AT : at + ELEMENT_SIZE_AT + 8] != want_size:
            continue
        target = struct.unpack_from("<Q", raw, at + ELEMENT_TARGET)[0]
        if not target:
            continue
        key = (text, length, target)
        serial = old.get(key)
        if serial is None:
            chars = _read(text, (length - 1) * 2)
            if chars is None or chars[:4] != b"@\0U\0":
                continue
            serial = chars.decode("utf-16-le", "replace")
        seen[key] = serial
    Names.seen = seen
    return set(seen.values())


# region Picture paths (for the menu page, see ui/pictures.js)

THUMB_CLASS = "TextureRenderTarget2D"
THUMB_NAME = "OakInventoryThumbnail"  # each picture: <the manager's capture component>:OakInventoryThumbnail_<n>
OUTER_CLASS = "SceneCaptureComponent2D"
OUTER_NAME = "/Engine/Transient.SceneCaptureComponent2D"
UOBJECT_NAME = 0x18  # FName {ComparisonIndex, Number}; the outer pointer follows at +0x20


class Paths:
    by_target: dict[int, str | None] = {}  # noqa: RUF012 - render target address -> its path (None: not resolved)
    outer: tuple[int, str | None] = (0, None)  # the capture component every picture is outered to


def _suffix(number: int) -> str:
    return f"_{number - 1}" if number else ""  # an FName's Number is the suffix + 1 (0: none)


def _object_at(cls: str, path: str, address: int) -> bool:
    """True if `path` names the object at `address` (the guess is checked, never trusted)."""
    try:
        return unrealsdk.find_object(cls, path)._get_address() == address  # noqa: SLF001
    except ValueError:
        return False


def _path(target: int) -> str | None:
    raw = _read(target + UOBJECT_NAME, 16)
    if raw is None:
        return None
    _index, number, outer = struct.unpack("<IIQ", raw)
    if Paths.outer[0] != outer:
        head = _read(outer + UOBJECT_NAME, 8)
        guess = OUTER_NAME + _suffix(struct.unpack_from("<I", head, 4)[0]) if head else None
        Paths.outer = (outer, guess if guess and _object_at(OUTER_CLASS, guess, outer) else None)
    if Paths.outer[1] is None:
        return None
    guess = f"{Paths.outer[1]}:{THUMB_NAME}{_suffix(number)}"
    return guess if _object_at(THUMB_CLASS, guess, target) else None


def paths(budget: int) -> tuple[list[str], int]:
    """
    The path of every picture in the map (as of the last rendered()), resolving at most `budget`
    new ones (~40 us each). Returns (paths, how many are still to resolve).
    """
    old, known, left = Paths.by_target, {}, 0
    for _text, _length, target in Names.seen:
        if target in known:
            continue
        if target in old:
            known[target] = old[target]
        elif budget > 0:
            budget -= 1
            known[target] = _path(target)
        else:
            left += 1
    Paths.by_target = known
    return sorted(path for path in known.values() if path), left


# endregion


def request(address: int, serial: str) -> None:
    """Asks the manager for this serial's picture, exactly as a card does (it skips one already asked for)."""
    text = ctypes.create_unicode_buffer(serial)  # FString data: UTF-16 with its NUL
    fstring = ctypes.create_string_buffer(struct.pack("<QII", ctypes.addressof(text), len(serial) + 1, len(serial) + 1))
    out = ctypes.create_string_buffer(0x40)  # ThumbnailID {FString; FIntPoint} + padding, zeroed
    Native.request(address, ctypes.addressof(out), ctypes.addressof(fstring), Native.base + SIZE_RVA)
    # The game put its own copy of the serial into `out` (its allocation, never freed by us: a few
    # bytes). Kept a while anyway, like Kairosdex does.
    Native.keep.append(out)
    del Native.keep[:-16]


# endregion

# region Our items


@dataclass
class Wanted:
    key: tuple[int, int] | None = None  # (PlayerState address, ArrayReplicationKey) of the last walk
    serials: list[str] = field(default_factory=list)  # equipped first
    by_handle: dict[int, str] = field(default_factory=dict)


def read_serial(identity: Any) -> str | None:
    head = _read(identity._get_address(), SERIAL_LEN + 8)  # noqa: SLF001
    if head is None:
        return None
    pointer, = struct.unpack_from("<Q", head, SERIAL_PTR)
    length, = struct.unpack_from("<Q", head, SERIAL_LEN)
    if not 2 < length <= MAX_SERIAL:
        return None  # not encoded yet (the game writes it when something asks for it)
    text = _read(pointer, length)
    if text is None:
        return None
    serial = text.split(b"\0", 1)[0].decode("ascii", "replace")
    return serial if serial.startswith("@U") else None


def walk(wanted: Wanted, ps: Any, container: Any) -> bool:
    """Reads the container's serials again if it changed. True if the list did."""
    key = (ps._get_address(), int(container.ArrayReplicationKey))  # noqa: SLF001
    if key == wanted.key:
        return False
    if wanted.key is None or wanted.key[0] != key[0]:
        wanted.by_handle = {}  # another player state: handles mean other items
    equipped, other, seen = [], [], {}
    for slot in container.items:
        inv = slot.InventoryItem
        handle = inv.Handle.Handle
        if handle == -1:
            continue
        serial = wanted.by_handle.get(handle)
        if serial is None:
            serial = read_serial(inv.Item.data.Identity)
            if serial is None:
                continue  # not encoded yet: the next change of the container tries again
        seen[handle] = serial
        (equipped if int(inv.Flags) & EQUIPPED_FLAG and inv.EquipSlot >= 0 else other).append(serial)
    wanted.by_handle = seen
    wanted.key = key
    serials = list(dict.fromkeys(equipped + other))
    changed = serials != wanted.serials
    wanted.serials = serials
    return changed


# endregion
