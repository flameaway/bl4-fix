"""
The game's own "equip this backpack item into that slot" call.

Nothing reflected equips an item, and the inventory screen's equips don't go through any UFunction
we could hook or call (on the host they never reach ProcessEvent). They go through an inventory
transaction component the player controller owns (not a reflected property). Its equip method
fills in the component's pending transaction and commits it: on the host that runs the
transaction right away, on a client it's sent as ExecuteInventoryTransactionOnServer. That's the
method we call. It checks everything the game checks for a manual equip (the item is in the
container and not equipped, the slot exists and isn't locked, the item fits the slot), and an
item already in the slot goes back to the backpack, like in the menu.

The machine code (verified against the running game, 2026-10-23):

    ExecuteInventoryTransactionOnServer's exec thunk (the UFunction's native pointer):
        call [rax+<validate>] ... call [rax+<implementation>]           FF 90 <off32>, twice
    the implementation (player controller vtable at <implementation>):
        mov rcx, [rcx+<component>]                                      48 8B 89 <off32>
    the equip method, equip(component, &handle, &slot owner, &container owner, &container name,
    slot index):
        mov eax, [rcx+0x1DC]          its owner (a weak pointer) must be alive    8B 81 DC 01 00 00
        lea rax, [r12+0x108]          each container slot's item handle           49 8D 84 24 08 01 00 00
        imul rdx, rdx, -0x150         container slots are 0x150 bytes             48 69 D2 B0 FE FF FF
        cmp byte [rax+5], 0xFF        the item's equip slot: must be "none"       80 78 05 FF
        ...
        mov byte [rsi+0x60], 5        transaction type: equip                     C6 46 60 05
        mov rcx, rsi
        call <commit>                                                           48 89 F1 E8

The type-5 store is our anchor (it must match exactly once in the game's code), the function
around it comes from the exe's unwind table, and the other instructions must be in it. The
offsets they hardcode must also agree with what reflection says about the backpack's slots
(GbxItemContainerSlot). If a game patch changes any of that, find() raises NotFound and the mod
won't equip anything, rather than call a wrong address.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import ctypes
import struct
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject, WrappedStruct

UFUNCTION_FUNC_OFFSET = 0xD8  # UFunction::Func
THUNK_SCAN = 0x100
SERVER_RPC = "/Script/GbxGame.GbxPlayerController:ExecuteInventoryTransactionOnServer"
TRANSACTION = "/Script/GbxGame.GbxInventoryTransaction"
SLOT_STRUCT = "/Script/GbxGame.GbxItemContainerSlot"
CONTAINER = "Backpack"

ANCHOR = b"\xc6\x46\x60\x05\x48\x89\xf1\xe8"
# Instructions the equip method must contain, and what they say about the container's slots
SLOT_SIZE = 0x150
HANDLE_OFFSET = 0x108
EQUIP_SLOT_OFFSET = 0x10D
REQUIRED = {
    b"\x8b\x81\xdc\x01\x00\x00": "the owner check",
    b"\x49\x8d\x84\x24\x08\x01\x00\x00": "the item handle offset",
    b"\x48\x69\xd2\xb0\xfe\xff\xff": "the container slot size",
    b"\x80\x78\x05\xff": "the equip slot check",
}

_kernel32 = ctypes.windll.kernel32
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.RtlLookupFunctionEntry.restype = ctypes.c_void_p
_kernel32.RtlLookupFunctionEntry.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p]
_kernel32.GetCurrentProcess.restype = ctypes.c_void_p
_kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
_kernel32.ReadProcessMemory.restype = ctypes.c_int
_process = _kernel32.GetCurrentProcess()
SCAN_CHUNK = 0x1000000  # the code section is ~160 MB: search it in pieces
OWNER_WEAK_PTR = 0x1D8  # the component's owner: FWeakObjectPtr (index, serial), see "the owner check"
OBJECT_INDEX = 0xC  # UObjectBase::InternalIndex

_Equip = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_int)  # fmt: skip


class NotFound(Exception):
    pass


def _u64(address: int) -> int | None:
    """A pointer-sized read that fails softly (ReadProcessMemory) instead of crashing the game."""
    if not 0x10000 <= address < 0x7FFFFFFFFFFF:
        return None
    value = ctypes.c_uint64()
    got = ctypes.c_size_t()
    if not _kernel32.ReadProcessMemory(_process, ctypes.c_void_p(address), ctypes.byref(value), 8, ctypes.byref(got)):
        return None
    return value.value if got.value == 8 else None  # noqa: PLR2004


def _module() -> tuple[int, int]:
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    (size,) = struct.unpack("<I", ctypes.string_at(base + pe + 0x50, 4))
    return base, base + size


def _text_section() -> tuple[int, int]:
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    count, opt_size = struct.unpack("<H12xH", ctypes.string_at(base + pe + 6, 16))
    sections = base + pe + 24 + opt_size
    for i in range(count):
        name, vsize, rva = struct.unpack("<8sII", ctypes.string_at(sections + 40 * i, 16))
        if name.rstrip(b"\0") == b".text":
            return base + rva, vsize
    raise NotFound("the game's code section")


def _function_around(addr: int) -> tuple[int, int]:
    """Start and end of the function containing addr, from the exe's unwind table."""
    image = ctypes.c_uint64()
    entry = _kernel32.RtlLookupFunctionEntry(addr, ctypes.byref(image), None)
    if not entry:
        raise NotFound("the equip method's bounds")
    begin, end = struct.unpack("<II", ctypes.string_at(entry, 8))
    return image.value + begin, image.value + end


def _component_offset(pc: UObject, lo: int, hi: int) -> int:
    """Where the player controller keeps its inventory transaction component."""
    func = unrealsdk.find_object("Function", SERVER_RPC)
    thunk = ctypes.c_uint64.from_address(func._get_address() + UFUNCTION_FUNC_OFFSET).value
    if not lo <= thunk < hi:
        raise NotFound("ExecuteInventoryTransactionOnServer has no native pointer")
    code = ctypes.string_at(thunk, THUNK_SCAN)
    vcalls = [struct.unpack_from("<i", code, i + 2)[0] for i in range(len(code) - 6) if code[i : i + 2] == b"\xff\x90"]
    if len(vcalls) != 2:  # noqa: PLR2004 - validate, then the implementation
        raise NotFound("the transaction implementation's vtable slot")
    vtable = ctypes.c_uint64.from_address(pc._get_address()).value
    impl = ctypes.c_uint64.from_address(vtable + vcalls[1]).value
    if not lo <= impl < hi:
        raise NotFound("the transaction implementation")
    code = ctypes.string_at(impl, 0x20)
    at = code.find(b"\x48\x8b\x89")
    if at < 0:
        raise NotFound("the transaction component offset")
    (offset,) = struct.unpack_from("<i", code, at + 3)
    # It's a native member, so somewhere inside the controller object and pointer-aligned
    if not 0 < offset <= pc.Class._get_struct_size() - 8 or offset % 8:
        raise NotFound(f"the transaction component offset ({offset:#x}) doesn't fit the controller")
    return offset


def _check_slot_layout() -> None:
    """The offsets the equip method hardcodes must match the backpack's reflected slot struct."""
    slot = unrealsdk.find_object("ScriptStruct", SLOT_STRUCT)
    inv = slot._find_prop("InventoryItem")
    item = inv.Struct
    handle = inv.Offset_Internal + item._find_prop("Handle").Offset_Internal
    equip_slot = inv.Offset_Internal + item._find_prop("EquipSlot").Offset_Internal
    if (slot._get_struct_size(), handle, equip_slot) != (SLOT_SIZE, HANDLE_OFFSET, EQUIP_SLOT_OFFSET):
        raise NotFound("the backpack's slot layout changed")


def _object_index(obj: UObject) -> int:
    """UObjectBase::InternalIndex (its slot in GUObjectArray)."""
    return ctypes.c_uint32.from_address(obj._get_address() + OBJECT_INDEX).value


class Equipper:
    def __init__(self, func: int, component_offset: int, module: tuple[int, int]) -> None:
        self.func_addr = func
        self.component_offset = component_offset
        self.module = module
        self._equip = _Equip(func)

    def equip(self, pc: UObject, pawn: UObject, handle: int, slot: int) -> None:
        """Equips the backpack item with this handle into this equip slot (the game's checks apply)."""
        component = _u64(pc._get_address() + self.component_offset)
        if not component:
            raise NotFound("the player has no inventory transaction component")
        # The component is a plain struct (no vtable). The equip method's first check is its owner
        # weak pointer (object index, serial): it must name this controller, or it isn't the component.
        owner = _u64(component + OWNER_WEAK_PTR)
        if owner is None or (owner & 0xFFFFFFFF) != _object_index(pc) or not owner >> 32:
            raise NotFound("the inventory transaction component isn't what it was")
        # The interface pointers (object + its interface's vtable), as reflection computes them
        owners = WrappedStruct(unrealsdk.find_object("ScriptStruct", TRANSACTION))
        owners.SourceContainerOwner = pc
        owners.SourceEquippedSlotOwner = pawn
        container_owner = _copy(owners, "SourceContainerOwner", 16)
        slot_owner = _copy(owners, "SourceEquippedSlotOwner", 16)
        sell = pc.ServerSellItem.func
        names = WrappedStruct(sell)
        names.ItemContainerName = CONTAINER
        container = _copy(names, "ItemContainerName", 8, sell)
        item = ctypes.c_int32(handle)
        self._equip(component, ctypes.addressof(item), ctypes.addressof(slot_owner),
                    ctypes.addressof(container_owner), ctypes.addressof(container), slot)  # fmt: skip


def _copy(struct_: WrappedStruct, field: str, size: int, owner: Any = None) -> ctypes.Array[ctypes.c_char]:
    prop = (owner or struct_._type)._find_prop(field)
    return ctypes.create_string_buffer(ctypes.string_at(struct_._get_address() + prop.Offset_Internal, size), size)


def find(pc: UObject) -> Equipper:
    lo, hi = _module()
    _check_slot_layout()
    offset = _component_offset(pc, lo, hi)

    start, size = _text_section()
    found: list[int] = []
    for chunk in range(0, size, SCAN_CHUNK):
        # Overlap the pieces so an anchor across a boundary is still seen (once)
        text = ctypes.string_at(start + chunk, min(SCAN_CHUNK + len(ANCHOR) - 1, size - chunk))
        at = text.find(ANCHOR)
        while 0 <= at < SCAN_CHUNK and len(found) < 2:  # noqa: PLR2004
            found.append(start + chunk + at)
            at = text.find(ANCHOR, at + 1)
        if len(found) > 1:
            break
    if len(found) != 1:
        raise NotFound("the equip method (anchor not unique)")
    begin, end = _function_around(found[0])
    body = ctypes.string_at(begin, end - begin)
    for pattern, what in REQUIRED.items():
        if body.count(pattern) != 1:
            raise NotFound(f"the equip method ({what})")
    return Equipper(begin, offset, (lo, hi))
