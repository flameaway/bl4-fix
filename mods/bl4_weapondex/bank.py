"""
Take from the bank (transfer.py): one bank item into the backpack, with the game's own internal
inventory functions. The game reflects no withdraw (only a deposit), and the bank lives in
profile.sav, which the running game holds, so it's done live, the way Reroll changes items
(mods/bl4_reroll/native.py, where these functions and offsets were found; keep them in step):

1. copy the bank item's identity (FInventoryIdentity);
2. add the copy to the backpack (the game's add, through the controller's inventory context): a
   new item with a new handle;
3. once it's in the backpack, remove the bank item from the bank's container (the game's remove).

That order never loses the item: if the remove fails it's in both. Tested 2026-11-06: the
game's next profile.sav had the bank one item shorter (its slots renumbered by the game) and
nothing else changed; the character's save had the item. The function addresses are for one game
build, each checked against the start of its code before use; after a patch this stays off.

Call everything here on the game thread.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import c_bool, c_int, c_void_p
from typing import Any

from . import owned

# Each function by RVA, with the first bytes of its code in the 2026-10-22 build
FUNCTIONS = {
    "copy_identity": (0x6FD608, "415741565657534883ec204889d74889ce488b0248890148"),
    "destroy_identity": (0x368B56, "56574883ec284889cee8a2020000488d8ea0000000e8200f"),
    "add_item": (0x88C68B6, "415741564154565755534881ec500100004489cb4489c649"),
    "remove_item": (0x119E7CC, "4157415641554154565755534881ec380100004c89c6488b"),
}
CONTEXT_VTABLE = 0xB993710  # the controller's inventory context (PC + CONTEXT_OFFSET)
CONTEXT_OFFSET = 0xE18  # OakPlayerController
IDENTITY_SIZE = 0xD8
SLOT_IDENTITY = 0x18  # GbxItemContainerSlot: the item's identity
SLOT_CONTAINER = 0x100  # GbxItemContainerSlot: its container

_k32 = ctypes.windll.kernel32
_k32.GetModuleHandleW.restype = c_void_p
_k32.GetCurrentProcess.restype = c_void_p
_k32.ReadProcessMemory.argtypes = [c_void_p, c_void_p, c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_process = _k32.GetCurrentProcess()


class BankError(Exception):
    """Nothing moved."""


class BankKept(BankError):
    """It arrived in the backpack, but the bank kept its copy too."""


def _u64(address: int) -> int:
    buffer = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_process, c_void_p(address), buffer, 8, ctypes.byref(got)) or got.value != 8:
        raise BankError("couldn't read the game's memory")
    return struct.unpack("<Q", buffer.raw)[0]


class _Api:
    def __init__(self) -> None:
        base = _k32.GetModuleHandleW(None)
        for name, (rva, prologue) in FUNCTIONS.items():
            if ctypes.string_at(base + rva, len(prologue) // 2).hex() != prologue:
                raise BankError("this game version isn't supported yet (the game was updated)")

        def fn(name: str, res: Any, *args: Any) -> Any:
            return ctypes.CFUNCTYPE(res, *args)(base + FUNCTIONS[name][0])

        self.base = base
        self.copy_identity = fn("copy_identity", c_void_p, c_void_p, c_void_p)
        self.destroy_identity = fn("destroy_identity", None, c_void_p)
        # (context, identity, gear slot, active slot, 1, false)
        self.add_item = fn("add_item", c_void_p, c_void_p, c_void_p, c_int, c_int, c_int, c_bool)
        # (container, &handle, context, quantity: 0 = all)
        self.remove_item = fn("remove_item", ctypes.c_uint8, c_void_p, c_void_p, c_void_p, c_int)


_api: _Api | None = None


def _get_api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def withdraw(pc: Any, serial: str) -> None:
    """One bank item with this serial into the backpack. Raises BankError (with nothing lost) if not."""
    a = _get_api()
    state = pc.PlayerState
    bank = state.BankContainer._get_address()  # noqa: SLF001
    entry = next((e for e in state.BankItems.items if owned.serial_of(e.InventoryItem) == serial), None)
    if entry is None:
        raise BankError("it isn't in the bank anymore")
    slot = entry._get_address()  # noqa: SLF001
    if _u64(slot + SLOT_CONTAINER) != bank:
        raise BankError("that item isn't in the bank's container")
    ctx = pc._get_address() + CONTEXT_OFFSET  # noqa: SLF001
    if _u64(ctx) != a.base + CONTEXT_VTABLE:
        raise BankError("this game version isn't supported yet (the game was updated)")
    handle = int(entry.InventoryItem.Handle.Handle)
    before = {int(e.InventoryItem.Handle.Handle) for e in state.BackpackItems.items}

    raw = ctypes.create_string_buffer(IDENTITY_SIZE + 0x40)
    copy = (ctypes.addressof(raw) + 15) & ~15
    a.copy_identity(copy, slot + SLOT_IDENTITY)
    try:
        a.add_item(ctx, copy, -1, -1, 1, False)
    finally:
        a.destroy_identity(copy)
    arrived = any(
        int(e.InventoryItem.Handle.Handle) not in before and owned.serial_of(e.InventoryItem) == serial
        for e in state.BackpackItems.items
    )
    if not arrived:
        raise BankError("it didn't arrive in your backpack (full?); it's still in the bank")
    h = c_int(handle)
    a.remove_item(bank, ctypes.byref(h), ctx, 0)
    if any(int(e.InventoryItem.Handle.Handle) == handle for e in state.BankItems.items):
        raise BankKept("the bank kept its copy too, so you have two now")
