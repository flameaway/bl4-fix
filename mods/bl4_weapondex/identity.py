"""
An item identity (FInventoryIdentity) of our own, made from a serial with the game's own functions:
the item's full stats with no pickup, no actor and nothing in the world (CLAUDE.md "Stats from a
serial, nothing spawned").

1. copy a live identity (an empty backpack slot's, see source()) with the game's copy constructor;
2. decode the serial into it with the game's save-load decoder (it resets everything from the
   serial; a decoded weapon already has its own built stats container);
3. gear: build the stats container with the game's lazy getter, and drop the extra reference it
   returns, so destroying the identity frees it;
4. read what we need (stats.py), destroy it, all in the same call on the game thread.

The same functions Reroll uses for its swaps (bl4_reroll/native.py), pinned here by their first
bytes for the build of 2026-10-22: after a game patch the check fails and rolls say so instead of
calling anything.
"""

from __future__ import annotations

import ctypes
from ctypes import c_void_p
from typing import Any

from . import stats

FUNCTIONS = {
    "copy_identity": (0x6FD608, "415741565657534883ec204889d74889ce488b0248890148"),
    "destroy_identity": (0x368B56, "56574883ec284889cee8a2020000488d8ea0000000e8200f"),
    "identity_from_serial": (0xCD2106, "4157415641554154565755534881ece80000000f29b424d0"),
    "get_stats": (0xC65C56, "4157415641554154565755534883ec68"),
}
IDENTITY_SIZE = 0xD8
IDENTITY_STATS = 0x90  # TSharedPtr: object, then its controller (refcount at +8)
CONTROLLER_REFS = 0x8
SLOT_IDENTITY = 0x18  # in a backpack entry (GbxItemContainerSlot)
PICKUP_IDENTITY = 0x3F0  # in an InventoryPickup

_k32 = ctypes.windll.kernel32
_k32.GetModuleHandleW.restype = c_void_p


class Unavailable(Exception):
    """This game build doesn't have the functions where we expect them, or nothing to copy from."""


class _StdString(ctypes.Structure):
    """An MSVC std::string pointing at our own text (the heap form: capacity >= 16)."""

    _fields_ = [("ptr", c_void_p), ("unused", ctypes.c_uint64), ("size", ctypes.c_uint64), ("capacity", ctypes.c_uint64)]  # noqa: RUF012


class _Api:
    def __init__(self) -> None:
        base = _k32.GetModuleHandleW(None)
        for name, (rva, head) in FUNCTIONS.items():
            if stats._read(base + rva, len(head) // 2) != bytes.fromhex(head):  # noqa: SLF001
                raise Unavailable(f"{name} isn't where this game build had it (game patched?)")

        def fn(name: str, res: Any, *args: Any) -> Any:
            return ctypes.CFUNCTYPE(res, *args)(base + FUNCTIONS[name][0])

        self.copy_identity = fn("copy_identity", c_void_p, c_void_p, c_void_p)
        self.destroy_identity = fn("destroy_identity", None, c_void_p)
        self.identity_from_serial = fn("identity_from_serial", ctypes.c_uint8, c_void_p, c_void_p)
        # (identity, TSharedPtr* out): builds the container if needed, returns a new reference
        self.get_stats = fn("get_stats", c_void_p, c_void_p, c_void_p)


class _State:
    api: _Api | None = None
    error: str | None = None


def api() -> _Api:
    if _State.api is None and _State.error is None:
        try:
            _State.api = _Api()
        except Unavailable as ex:
            _State.error = str(ex)
    if _State.api is None:
        raise Unavailable(_State.error or "not loaded")
    return _State.api


class _Source:
    state = 0  # the PlayerState (address) and backpack index of the empty slot last used
    index = -1


def source(pc: Any) -> int:
    """
    A live identity to copy from. Game thread. In order:
    - an empty backpack slot: the backpack keeps fixed slots, and an empty one still holds an
      identity the game built and cleared (arrays empty, no stats, empty string). No item at all;
      decoding from one gave the same values as from an item (15 of 15, 2026-10-24);
    - an item in the backpack;
    - any pickup in the world (cash, ammo, loot: InventoryPickup+0x3F0).
    """
    used = None
    try:
        state = pc.PlayerState
        items = state.BackpackItems.items
        # The empty slot found last time, if it's still empty (one read; the walk to the first
        # empty slot past 84 items took 0.5-1 ms)
        if _Source.state == state._get_address() and 0 <= _Source.index < len(items):  # noqa: SLF001
            entry = items[_Source.index]
            if int(entry.InventoryItem.Handle.Handle) == -1:
                return entry._get_address() + SLOT_IDENTITY  # noqa: SLF001
        for i, entry in enumerate(items):
            if int(entry.InventoryItem.Handle.Handle) == -1:
                _Source.state, _Source.index = state._get_address(), i  # noqa: SLF001
                return entry._get_address() + SLOT_IDENTITY  # noqa: SLF001
            if used is None:
                used = entry._get_address() + SLOT_IDENTITY  # noqa: SLF001
    except Exception:  # noqa: BLE001 - no backpack (yet): try the world
        pass
    if used is not None:
        return used
    try:
        import unrealsdk  # noqa: PLC0415
        from mods_base import ENGINE  # noqa: PLC0415

        found = unrealsdk.find_class("GameplayStatics").ClassDefaultObject.GetAllActorsOfClass(
            ENGINE.GameViewport.World, unrealsdk.find_class("InventoryPickup"), [])  # fmt: skip
        for pickup in found[1] if isinstance(found, tuple) else found:
            return pickup._get_address() + PICKUP_IDENTITY  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    raise Unavailable("nothing to start an item from yet (pick something up first)")


class Identity:
    """An identity we own: Identity(source).load(serial), read it, then destroy() (always)."""

    def __init__(self, source: int) -> None:
        self._raw = ctypes.create_string_buffer(IDENTITY_SIZE + 0x40)
        self.address = (ctypes.addressof(self._raw) + 15) & ~15
        self._alive = False
        api().copy_identity(self.address, source)
        self._alive = True

    def load(self, serial: str) -> bool:
        """Makes this the item `serial` describes, with its stats built. False if the game refused it."""
        a = api()
        text = ctypes.create_string_buffer(serial.encode("ascii"))
        string = _StdString(ctypes.addressof(text), 0, len(serial), max(len(serial), 16))
        if not a.identity_from_serial(self.address, ctypes.byref(string)):
            return False
        if not stats._u64(self.address + IDENTITY_STATS):  # noqa: SLF001 - gear: the decoder leaves it to the getter
            out = (ctypes.c_uint64 * 2)()
            a.get_stats(self.address, ctypes.addressof(out))
            controller = stats._u64(self.address + IDENTITY_STATS + 8)  # noqa: SLF001
            # Ours is the only owner: the identity's reference plus the one returned (2) -> 1
            if out[0] and controller and out[1] == controller:
                refs = ctypes.c_int32.from_address(controller + CONTROLLER_REFS)
                if refs.value == 2:  # noqa: PLR2004
                    refs.value = 1
        return stats.has_stats(self.address)

    def destroy(self) -> None:
        if self._alive:
            self._alive = False
            api().destroy_identity(self.address)


def check_parts(identity: int, comp: int, parts: list[tuple[int, int | str]]) -> bool:
    """
    The decoded identity holds exactly our composition and parts (as (table, index) pairs; a part
    written as text has no serial index: its def reads -1).
    """
    from collections import Counter  # noqa: PLC0415

    if stats.comp_index(identity) != comp:
        return False
    want = Counter((t, -1 if isinstance(i, str) else i) for t, i in parts)
    return Counter((t, i) for i, t in stats.part_owners(identity)) == want
