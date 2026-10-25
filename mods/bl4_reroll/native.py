"""
Changing an item in place, with the game's own internal functions, the way its Firmware Transfer
does (FirmwareTransferBlueprintLibrary.TransferFirmware's machine code shows the steps):

1. build the changed item's identity (FInventoryIdentity) from its serial, with the function the
   game uses when it reads items from a save: so it's exactly what loading that serial gives,
   level and seed included, and every cache it fills later (the level the card shows, the stats)
   comes from the serial;
2. if the item is equipped, take it off its gear slot;
3. remove the old item from the backpack container and add the new identity, with the old item's
   gear slot so an equipped item goes straight back on.

Those are the game's own add and remove, so the inventory screen follows them like any other
change, and there's no mail package and no vending machine. We also hand the new item the old
item's handle (the game takes handles from one counter), so it's the same item to everything
that refers to it.

The function addresses are for one game build (2026-10-22), each checked against the start of its
code before use. After a game patch the checks fail and this stays off; game.py then falls back to
the slower way (sell + mail).

Lessons: building the identity by editing a copy's part list (as the firmware transfer does)
dropped the level, and copying an identity carries its caches along (a copy of a damaged item
kept showing level 1). Decoding the serial avoids both.

Call everything here on the game thread. This file holds no hooks, so it can be reloaded.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import c_bool, c_int, c_void_p
from typing import Any

from unrealsdk import logging

from . import reroll

# Each function by RVA, with the first bytes of its code in the build this was made against
FUNCTIONS = {
    "copy_identity": (0x6FD608, "415741565657534883ec204889d74889ce488b0248890148"),
    "destroy_identity": (0x368B56, "56574883ec284889cee8a2020000488d8ea0000000e8200f"),
    "identity_from_serial": (0xCD2106, "4157415641554154565755534881ece80000000f29b424d0"),
    "add_item": (0x88C68B6, "415741564154565755534881ec500100004489cb4489c649"),
    "remove_item": (0x119E7CC, "4157415641554154565755534881ec380100004c89c6488b"),
    "set_equip_slot": (0x1069B66, "56574883ec284889ce488b81a00000004863882001000048"),
    "refresh_gear": (0x47F69B6, "4157415641554154565755534883ec284889ce8b49148d41"),
}
# add_item reads the next handle from a global: `mov ecx, [rip + rel32]` at this offset
HANDLE_COUNTER_LOAD = 0x76
# vtables we call through or check, by RVA
CONTEXT_VTABLE = 0xB993710  # the controller's inventory context (PC + CONTEXT_OFFSET)
GEAR_VTABLE = 0xB6FDAC0  # the character's equipment (OakCharacter + GEAR_OFFSET)
# Native-only members
CONTEXT_OFFSET = 0xE18  # OakPlayerController
GEAR_OFFSET = 0x3BE8  # OakCharacter
GEAR_STATE_OFFSET = 0x5A20  # OakCharacter, refreshed after a gear change
GEAR_SLOT_SOURCE = 0xC  # in a gear slot: the backpack handle it came from
GEAR_GET_SLOT, GEAR_UNEQUIP, GEAR_ACTIVE = 0x10, 0x50, 0x80  # gear vtable offsets
IDENTITY_SIZE = 0xD8
IDENTITY_SERIAL = 0xA0  # std::string
# GbxItemContainerSlot
SLOT_ITEM = 0x10  # InventoryItem
SLOT_CONTAINER = 0x100  # the item's container (the backpack's GbxItemContainer)
SLOT_IDENTITY = 0x18

_k32 = ctypes.windll.kernel32
_k32.GetModuleHandleW.restype = c_void_p
_k32.GetCurrentProcess.restype = c_void_p
_k32.ReadProcessMemory.argtypes = [c_void_p, c_void_p, c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_process = _k32.GetCurrentProcess()


class Unavailable(Exception):
    """The native path can't be used here (another game build, or something didn't check out)."""


class NotTaken(Exception):
    """The game didn't take the changed item (the old one was put back)."""


def _read(address: int, size: int) -> bytes:
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_process, c_void_p(address), buffer, size, ctypes.byref(got)) or got.value != size:
        raise Unavailable(f"couldn't read {size} bytes at {address:#x}")
    return buffer.raw


def _u64(address: int) -> int:
    return struct.unpack("<Q", _read(address, 8))[0]


class _StdString(ctypes.Structure):
    """An MSVC std::string pointing at our own text (the heap form: capacity >= 16)."""

    _fields_ = [  # noqa: RUF012
        ("ptr", c_void_p), ("unused", ctypes.c_uint64), ("size", ctypes.c_uint64), ("capacity", ctypes.c_uint64),
    ]  # fmt: skip


# region Functions


class _Api:
    def __init__(self) -> None:
        base = _k32.GetModuleHandleW(None)
        for name, (rva, prologue) in FUNCTIONS.items():
            if ctypes.string_at(base + rva, len(prologue) // 2).hex() != prologue:
                raise Unavailable(f"{name} isn't where this game build had it")
        add = base + FUNCTIONS["add_item"][0]
        load = ctypes.string_at(add + HANDLE_COUNTER_LOAD, 6)
        if load[:2] != b"\x8b\x0d":
            raise Unavailable("the handle counter isn't where it was")

        def fn(name: str, res: Any, *args: Any) -> Any:
            return ctypes.CFUNCTYPE(res, *args)(base + FUNCTIONS[name][0])

        self.base = base
        self.handle_counter = add + HANDLE_COUNTER_LOAD + 6 + struct.unpack("<i", load[2:])[0]
        self.copy_identity = fn("copy_identity", c_void_p, c_void_p, c_void_p)
        self.destroy_identity = fn("destroy_identity", None, c_void_p)
        # (identity, const std::string& serial) -> whether it decoded to a complete item
        self.identity_from_serial = fn("identity_from_serial", ctypes.c_uint8, c_void_p, c_void_p)
        # (context, identity, gear slot, active slot, 1, false)
        self.add_item = fn("add_item", c_void_p, c_void_p, c_void_p, c_int, c_int, c_int, c_bool)
        # (container, &handle, context, quantity: 0 = all)
        self.remove_item = fn("remove_item", ctypes.c_uint8, c_void_p, c_void_p, c_void_p, c_int)
        # (container, &handle, gear slot)
        self.set_equip_slot = fn("set_equip_slot", None, c_void_p, c_void_p, c_int)
        self.refresh_gear = fn("refresh_gear", None, c_void_p)


_api: _Api | None = None
_api_error: str | None = None


def api() -> _Api:
    global _api, _api_error
    if _api is None and _api_error is None:
        try:
            _api = _Api()
        except Unavailable as ex:
            _api_error = str(ex)
            logging.info(f"[Reroll] Swapping items in place is off for this game version ({ex}).")
    if _api is None:
        raise Unavailable(_api_error or "not loaded")
    return _api


def available() -> bool:
    try:
        api()
    except Unavailable:
        return False
    return True


def _vfunc(obj: int, offset: int, res: Any, *args: Any) -> Any:
    return ctypes.CFUNCTYPE(res, c_void_p, *args)(_u64(_u64(obj) + offset))


# endregion
# region Swapping


def _context(pc: Any) -> int:
    ctx = pc._get_address() + CONTEXT_OFFSET
    if _u64(ctx) != api().base + CONTEXT_VTABLE:
        raise Unavailable("the controller's inventory context isn't what it was")
    return ctx


class _Identity:
    """An FInventoryIdentity we own (copied from one the game has, so it starts out valid)."""

    def __init__(self, source: int) -> None:
        self._raw = ctypes.create_string_buffer(IDENTITY_SIZE + 0x40)
        self.address = (ctypes.addressof(self._raw) + 15) & ~15
        api().copy_identity(self.address, source)

    def load(self, serial: str) -> bool:
        """Makes this the item `serial` describes, the way the game reads one from a save."""
        text = ctypes.create_string_buffer(serial.encode("ascii"))
        string = _StdString(ctypes.addressof(text), 0, len(serial), max(len(serial), 16))
        return bool(api().identity_from_serial(self.address, ctypes.byref(string)))

    def serial(self) -> str:
        data, _, size, capacity = struct.unpack("<4Q", _read(self.address + IDENTITY_SERIAL, 32))
        if capacity < 16:  # short strings live inside the struct
            return _read(self.address + IDENTITY_SERIAL, size).decode("ascii", "replace")
        return _read(data, size).decode("ascii", "replace")

    def destroy(self) -> None:
        api().destroy_identity(self.address)


def _find_entry(pc: Any, handle: int) -> Any:
    for entry in pc.PlayerState.BackpackItems.items:
        if entry.InventoryItem.Handle.Handle == handle:
            return entry
    return None


def _add(a: _Api, ctx: int, identity: int, handle: int, gear_slot: int, active: int) -> None:
    """Adds an item with this handle (the game takes it from its next-handle counter)."""
    counter = ctypes.c_int.from_address(a.handle_counter)
    saved = counter.value
    counter.value = handle
    try:
        a.add_item(ctx, identity, gear_slot, active, 1, False)
    finally:
        used = counter.value
        if used in (handle, handle + 1):
            counter.value = saved
        else:
            # It took more than one handle: never wind the counter back past what it handed out
            counter.value = max(saved, used)
            logging.error(f"[Reroll] Adding an item took handles {handle}..{used - 1}; expected only {handle}.")


def replace(pc: Any, entry: Any, item: reroll.Item, serial: str) -> int:
    """
    Makes the backpack item in `entry` (a GbxItemContainerSlot) the item `serial` describes, in
    place: same handle, and still equipped if it was. Returns the handle. Raises Unavailable
    before changing anything if a check fails.
    """
    a = api()
    slot = entry._get_address()
    identity = slot + SLOT_IDENTITY
    container = _u64(slot + SLOT_CONTAINER)
    if container != pc.PlayerState.BackpackContainer._get_address():
        raise Unavailable("that item isn't in the backpack container")
    ctx = _context(pc)
    handle = int(entry.InventoryItem.Handle.Handle)
    marks = int(entry.InventoryItem.Item.State.Flags)
    gear_slot = int(entry.InventoryItem.EquipSlot)
    character = pc.OakPlayerCharacter
    gear = character._get_address() + GEAR_OFFSET if character is not None else 0
    if gear_slot >= 0 and (not gear or _u64(gear) != a.base + GEAR_VTABLE):
        raise Unavailable("the character's equipment isn't what it was")

    original = _Identity(identity)  # put back if the changed one doesn't arrive
    changed = _Identity(identity)
    arrived = False
    try:
        if not changed.load(serial) or changed.serial() != serial:
            raise Unavailable("the game didn't read the new serial")

        active = -1
        if gear_slot >= 0:
            # Take it off its gear slot first, like the firmware transfer does
            active = _vfunc(gear, GEAR_ACTIVE, c_int, c_int)(gear, gear_slot)
            source = _vfunc(gear, GEAR_GET_SLOT, c_void_p, c_int)(gear, gear_slot)
            if source:
                ctypes.c_int.from_address(source + GEAR_SLOT_SOURCE).value = -1
            h = ctypes.c_int(handle)
            a.set_equip_slot(container, ctypes.byref(h), -1)
            _vfunc(gear, GEAR_UNEQUIP, None, c_int, c_void_p, c_bool)(gear, gear_slot, slot + SLOT_ITEM, True)

        h = ctypes.c_int(handle)
        a.remove_item(container, ctypes.byref(h), ctx, 0)
        if _find_entry(pc, handle) is not None:
            # Adding now would leave two items with one handle
            if gear_slot >= 0 and character is not None:
                a.refresh_gear(character._get_address() + GEAR_STATE_OFFSET)
            raise NotTaken(
                "The game kept the old item; nothing was changed"
                + (" (it was taken off, so equip it again)." if gear_slot >= 0 else "."),
            )
        try:
            _add(a, ctx, changed.address, handle, gear_slot, active)
        except Exception as ex:  # noqa: BLE001 - the old item is out: it has to go back in whatever happened
            logging.error(f"[Reroll] Adding the changed item failed: {ex!r}")
        arrived = _find_entry(pc, handle) is not None
        if not arrived:
            # Logged first, so the serial survives even if putting it back fails too
            logging.error(f"[Reroll] The changed item didn't arrive; putting the old one back ({item.serial})")
            _add(a, ctx, original.address, handle, gear_slot, active)
        if gear_slot >= 0 and character is not None:
            a.refresh_gear(character._get_address() + GEAR_STATE_OFFSET)
    finally:
        changed.destroy()
        original.destroy()

    back = _find_entry(pc, handle)
    if not arrived:
        raise NotTaken("The game didn't take the changed item" + ("; you have the old one back." if back is not None else "."))
    back.InventoryItem.Item.State.Flags = marks
    return handle


# endregion
