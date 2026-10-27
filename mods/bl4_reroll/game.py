"""
The game side: reading the backpack's serials, swapping an item for one with a new serial, and
Eridium. Call everything here on the game thread.

Reading serials. An item's identity (FInventoryIdentity) has no reflected fields, but it keeps its
serial as text: a pointer at +0xA0 and the length at +0xB0. We read that with ReadProcessMemory, so
a bad pointer after a game patch gives an error instead of a crash, and only accept text that
decodes as a serial.

Replacing an item. native.py does it the way the game's Firmware Transfer does, with the game's
own internal functions: the item keeps its handle and its gear slot, and the inventory screen
follows along. That needs this game build; on another one we fall back to the slow way, built
from what's reflected:

1. sell the old item to any loaded vending machine (ServerSellItem works at any distance), take
   the money back off and take the item off the machine's buyback list;
2. give ourselves a one-item reward package with the new serial and open it (the game then shows
   its rewards screen), so the new item arrives with a new handle;
3. copy the old item's favourite / seen marks onto it.

That way an equipped item comes back unequipped. Editing backpack entries directly isn't an
option: the inventory screen tracks adds and removes itself, and showed the old item next to the
new one. Only the host's backpack is real, so this only works in your own game.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import copy
import ctypes
import math
import struct
import time
from dataclasses import dataclass
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, WrappedStruct

from . import legendary, native, reroll, serialgen

# FInventoryIdentity: the serial's text pointer and length
SERIAL_PTR = 0xA0
SERIAL_LEN = 0xB0
MAX_SERIAL = 4096
# A reward package holding one random item and nothing else (a side mission's reward)
CARRIER_REWARD = "Reward_ItemPool_AAAForEffort"
ERIDIUM = "eridium"
CASH = "Cash"
BACKPACK = "Backpack"
EQUIPPED_FLAG = 1

_k32 = ctypes.windll.kernel32
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
_k32.ReadProcessMemory.restype = ctypes.c_int
_process = _k32.GetCurrentProcess()


class RerollError(Exception):
    pass


def _read(address: int, size: int) -> bytes | None:
    if not 0x10000 <= address < 0x7FFFFFFFFFFF or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)):
        return None
    return buffer.raw[: got.value]


def read_serial(identity: Any) -> str | None:
    """The serial an item identity holds, or None."""
    head = _read(identity._get_address(), SERIAL_LEN + 8)
    if head is None or len(head) < SERIAL_LEN + 8:
        return None
    pointer = int.from_bytes(head[SERIAL_PTR : SERIAL_PTR + 8], "little")
    length = int.from_bytes(head[SERIAL_LEN : SERIAL_LEN + 8], "little")
    if not 2 < length <= MAX_SERIAL:
        return None
    text = _read(pointer, length)
    if text is None:
        return None
    serial = text.split(b"\0", 1)[0].decode("ascii", "replace")
    return serial if serial.startswith("@U") else None


def decode(serial: str) -> reroll.Item | legendary.Legend | None:
    """What Reroll can do with an item: a class mod / enhancement's stat lines, or a whole legendary / pearl."""
    return reroll.read(serial) or legendary.read(serial)


# region Backpack


@dataclass
class Slot:
    index: int
    handle: int
    equipped: bool
    item: reroll.Item | legendary.Legend


class _Acting:
    """The player a host-side request is for (None: the local player)."""

    pc: Any = None


@contextlib.contextmanager
def acting_for(pc: Any) -> Iterator[None]:
    """On the host: everything here reads and changes this player (a co-op friend) instead of ours."""
    old, _Acting.pc = _Acting.pc, pc
    try:
        yield
    finally:
        _Acting.pc = old


def _pc() -> Any:
    return _Acting.pc or get_pc()


def _player_state() -> Any:
    pc = _pc()
    ps = pc.PlayerState if pc else None
    if ps is None or pc.Pawn is None:
        raise RerollError("Load into the game first.")
    return ps


_kismet: Any = None


def is_host() -> bool:
    global _kismet
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return False
    if _kismet is None:
        # A class default object lives as long as the game: fine to keep
        _kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    return bool(_kismet.IsServer(world))


def _equipped(inv: Any) -> bool:
    return bool(int(inv.Flags) & EQUIPPED_FLAG) and inv.EquipSlot >= 0


def _sorted(slots: list[Slot]) -> list[Slot]:
    slots.sort(key=lambda s: (not s.equipped, s.item.kind, s.index))
    return slots


def items() -> list[Slot]:
    """
    The class mods, enhancements, legendaries and pearls in the backpack (equipped ones too),
    equipped first, read live
    (every serial from memory). For showing things, cached_items() is much cheaper.
    """
    out = []
    for index, slot in enumerate(_player_state().BackpackItems.items):
        inv = slot.InventoryItem
        if inv.Handle.Handle == -1:
            continue
        serial = read_serial(inv.Item.data.Identity)
        item = decode(serial) if serial else None
        if item is not None:
            out.append(Slot(index, inv.Handle.Handle, _equipped(inv), item))
    return _sorted(out)


def find_serial(serial: str) -> Slot | None:
    """The first rerollable item with this serial, read live (a friend's item, on the host)."""
    return next((slot for slot in items() if slot.item.serial == serial), None)


def find(handle: int) -> Slot | None:
    """The item with this handle, read live: its serial comes from memory now, never a cache."""
    for index, entry in enumerate(_player_state().BackpackItems.items):
        inv = entry.InventoryItem
        if inv.Handle.Handle != handle:
            continue
        serial = read_serial(inv.Item.data.Identity)
        item = decode(serial) if serial else None
        cached = Backpack.serials.get(handle)
        if cached is not None and cached[0] != serial:
            # The cache had another serial for it: look at everything again next time
            invalidate(handle)
        return Slot(index, handle, _equipped(inv), item) if item is not None else None
    return None


# region Backpack cache
#
# The card script and the mod menu page need the list of rerollable items often, but the backpack
# is 200+ fixed slots, and reading every one (plus each serial from memory) costs milliseconds. So:
#
# - Nothing reads it in gameplay: only the card state (while the menu page polls) and the mod menu
#   page (while it shows) ask, through cached_items().
# - A cheap key first: the backpack's ArrayReplicationKey (a FastArray: the game bumps it when it
#   marks entries dirty for replication) and the PlayerState. Same key and recent enough: the
#   last list is returned without touching the slots.
# - Otherwise one walk that reads only handles and flags, and a serial from memory only for a
#   handle it hasn't seen. A handle keeps its serial: the game hands every new item a new handle;
#   only our own in-place swap keeps the handle with a new serial, and replace() forgets it.
# - The key isn't trusted alone (that the game bumps it on every add, remove and equip is
#   unverified), so a re-check every FULL_S while someone keeps asking catches a missed change, and
#   logs it once so we learn whether the key is enough.
#
# Showing only. Anything that changes an item reads it live first (find(), replace()).

FULL_S = 5.0
# Bounded: handles that are no longer in the backpack are dropped on each walk
_MAX_SERIALS = 4096


class Backpack:
    key: Any = None
    slots: list[Slot] | None = None
    walked_at = -1e9
    # handle -> (serial, decoded item or None for any other item)
    serials: dict[int, tuple[str | None, reroll.Item | legendary.Legend | None]] = {}  # noqa: RUF012
    walks = 0
    stale = False  # invalidate() was called since the last walk
    missed_logged = False


def _key(ps: Any) -> Any:
    container = ps.BackpackItems
    try:
        replication = int(container.ArrayReplicationKey)
    except (AttributeError, ValueError):
        replication = None
    return (ps._get_address(), replication)


def invalidate(handle: int | None = None) -> None:
    """Forget one handle's serial (or all of them); the next cached_items() walks again."""
    if handle is None:
        Backpack.serials = {}
    else:
        Backpack.serials.pop(handle, None)
    Backpack.walked_at = -1e9
    Backpack.stale = True


def _walk(ps: Any) -> list[Slot]:
    known = Backpack.serials
    seen: dict[int, tuple[str | None, reroll.Item | legendary.Legend | None]] = {}
    out = []
    for index, entry in enumerate(ps.BackpackItems.items):
        inv = entry.InventoryItem
        handle = inv.Handle.Handle
        if handle == -1:
            continue
        hit = known.get(handle)
        if hit is None:
            serial = read_serial(inv.Item.data.Identity)
            hit = (serial, decode(serial) if serial else None)
        if hit[0] is not None:
            # (An unreadable serial, say an item still arriving, is read again next walk)
            seen[handle] = hit
        if hit[1] is not None:
            out.append(Slot(index, handle, _equipped(inv), hit[1]))
    Backpack.serials = seen if len(seen) <= _MAX_SERIALS else {}
    Backpack.walks += 1
    return _sorted(out)


def _same(a: list[Slot], b: list[Slot]) -> bool:
    return [(s.handle, s.item.serial, s.equipped) for s in a] == [(s.handle, s.item.serial, s.equipped) for s in b]


def cached_items(max_age: float = FULL_S) -> list[Slot]:
    """
    items() for showing: the last list while the backpack's key is unchanged and the list is under
    max_age seconds old, else one walk. Never use it to decide what to change: find() reads live.
    """
    ps = _player_state()
    key = _key(ps)
    now = time.monotonic()
    old = Backpack.slots
    if old is not None and key == Backpack.key and now - Backpack.walked_at < max_age:
        return old
    if Backpack.key is None or key[0] != Backpack.key[0]:
        # Another PlayerState (a new map or character): handles may mean other items now
        Backpack.serials = {}
    # Only a re-check of an unchanged key (not one after invalidate()) says the key missed something
    recheck = old is not None and key == Backpack.key and not Backpack.stale
    slots = _walk(ps)
    if recheck and not Backpack.missed_logged and not _same(old, slots):
        Backpack.missed_logged = True
        logging.dev_warning("[Reroll] The backpack changed without its ArrayReplicationKey changing; the periodic re-check caught it.")
    Backpack.key, Backpack.slots, Backpack.walked_at, Backpack.stale = key, slots, now, False
    return slots


# endregion


def _open_package_with(serial: str) -> None:
    pc = _pc()
    manager = pc.RewardsManager
    if manager is None:
        raise RerollError("The game's reward mail isn't available.")
    lib = unrealsdk.find_class("GbxRewards_BlueprintFunctions").ClassDefaultObject
    func = unrealsdk.find_object("Function", "/Script/GbxGame.GbxRewards_BlueprintFunctions:GiveRewardPackage")
    before = len(manager.packages)
    lib.GiveRewardPackage(FGbxDefPtr(CARRIER_REWARD, func._find_prop("RewardDef").Struct), pc)
    if len(manager.packages) != before + 1:
        raise RerollError("The game didn't create the item package.")
    index = before
    package = manager.packages[index]
    if package.RewardsDef._name != CARRIER_REWARD or len(package.contents) != 1 or len(package.contents[0].SerialNumbers) != 1:
        # Not the package we asked for: leave it alone (it's the player's)
        raise RerollError("The item package didn't look right; nothing was changed.")
    package.contents[0].SerialNumbers[0] = serial
    manager.Server_OpenPackage(index)
    if len(manager.packages) > before:
        # Opening failed; don't leave our package in the player's mail
        manager.Server_DeletePackage(index)
        raise RerollError("The game didn't open the item package.")


def vendor() -> Any:
    """The nearest loaded vending machine, or None."""
    pawn = _pc().Pawn
    here = pawn.K2_GetActorLocation() if pawn else None
    best, best_d = None, None
    for machine in unrealsdk.find_all("OakVendingMachine", exact=False):
        if machine.Name.startswith("Default__") or machine.ShopDef is None:
            continue
        where = machine.K2_GetActorLocation()
        d = 0.0 if here is None else (where.X - here.X) ** 2 + (where.Y - here.Y) ** 2 + (where.Z - here.Z) ** 2
        if best_d is None or d < best_d:
            best, best_d = machine, d
    return best


def _entry(handle: int) -> Any:
    for entry in _player_state().BackpackItems.items:
        if entry.InventoryItem.Handle.Handle == handle:
            return entry
    return None


def _sell(machine: Any, handle: int) -> None:
    pc = _pc()
    cash = _currency(CASH).type
    fn = pc.ServerSellItem
    args = WrappedStruct(fn.func)
    args.VendingMachineActor = machine
    args.ItemHandle.Handle = handle
    args.ItemContainerName = BACKPACK
    args.CurrencyID.Name = cash.Name
    args.CurrencyID.Hash = cash.Hash
    fn(args)


def _forget_sale(machine: Any, serial: str) -> None:
    """Takes a sold item off the machine's buyback list (so the old version can't be bought back)."""
    buyback = machine.BuybackItems
    for i in reversed(range(len(buyback))):
        if read_serial(buyback[i].InvIdent) == serial:
            buyback.pop(i)
            return


def _add(serial: str, present: set[int]) -> Any:
    """Adds an item with this serial; returns its backpack entry, or None if it didn't arrive."""
    _open_package_with(serial)
    for entry in _player_state().BackpackItems.items:
        inv = entry.InventoryItem
        if inv.Handle.Handle not in (-1, *present) and read_serial(inv.Item.data.Identity) == serial:
            return entry
    return None


def can_replace() -> str | None:
    """Why replace() couldn't work right now, or None."""
    if native.available() or vendor() is not None:
        return None
    return "Rerolling needs a vending machine nearby (any one the game has loaded around you)."


def replace(slot: Slot, new_serial: str) -> tuple[int, bool]:
    """
    Swaps the item in `slot` for one with `new_serial`. Returns the new item's handle and whether
    it's equipped.
    """
    try:
        return _replace(slot, new_serial)
    finally:
        # Whatever happened, the backpack cache can't trust that handle's serial any more (the
        # in-place swap keeps the handle and doesn't bump the backpack's replication key)
        invalidate(slot.handle)


def _replace(slot: Slot, new_serial: str) -> tuple[int, bool]:
    entry = _entry(slot.handle)
    if entry is None or read_serial(entry.InventoryItem.Item.data.Identity) != slot.item.serial:
        raise RerollError("That item changed; try again.")
    new = decode(new_serial)
    if new is None:
        raise RerollError("The new item didn't read back.")
    if native.available():
        try:
            return native.replace(_pc(), entry, slot.item, new_serial), slot.equipped
        except native.NotTaken as ex:
            raise RerollError(str(ex)) from ex
        except native.Unavailable as ex:
            logging.info(f"[Reroll] In-place swap not possible ({ex}); using the slow way.")
    return _replace_slow(slot, entry, new_serial), False


def _replace_slow(slot: Slot, entry: Any, new_serial: str) -> int:
    machine = vendor()
    if machine is None:
        raise RerollError("Rerolling needs a vending machine nearby (any one the game has loaded around you).")
    marks = entry.InventoryItem.Item.State.Flags
    entries = _player_state().BackpackItems.items
    # Items already there with either serial (say, an identical copy) aren't the one we add
    present_new = {e.InventoryItem.Handle.Handle for e in entries if read_serial(e.InventoryItem.Item.data.Identity) == new_serial}
    present_old = {e.InventoryItem.Handle.Handle for e in entries if read_serial(e.InventoryItem.Item.data.Identity) == slot.item.serial}
    cash = balance(CASH)
    try:
        _sell(machine, slot.handle)
        if _entry(slot.handle) is not None:
            raise RerollError("The game wouldn't take the old item; nothing was changed.")
        # The sold item stays on the buyback list until one of the two items has arrived, so
        # it can still be bought back if both fail
        landed = None
        try:
            landed = _add(new_serial, present_new)
        except Exception as ex:  # noqa: BLE001 - the old item is sold: it has to come back whatever happened
            logging.error(f"[Reroll] Adding the new item failed: {ex!r}")
        if landed is None:
            logging.error(f"[Reroll] The new item didn't arrive; giving back {slot.item.serial}")
            back = None
            try:
                back = _add(slot.item.serial, present_old - {slot.handle})
            except Exception as ex:  # noqa: BLE001
                logging.error(f"[Reroll] Giving the old item back failed: {ex!r}")
            if back is None:
                raise RerollError("The game didn't add the new item or give the old one back: buy it back at the vending machine.")
            _forget_sale(machine, slot.item.serial)
            raise RerollError("The game didn't add the new item; you have the old one back.")
        _forget_sale(machine, slot.item.serial)
        landed.InventoryItem.Item.State.Flags = marks
        landed.ReplicationKey += 1
        _player_state().BackpackItems.ArrayReplicationKey += 1
        return int(landed.InventoryItem.Handle.Handle)
    finally:
        set_balance(CASH, cash)


# endregion
# region Legendary roll values


class Values:
    """
    Game values a legendary roll's weights, level gates and part chances name (legendaries.json
    "values"): attributes, all constants or table cells (`attr_weight_weapon_grip_hyp` = 0.3,
    `Att_MinGameStage_*` = a LootSchedule cell), and data table cells. Asked from the game once
    per session (~0.02-0.07 ms each, a few dozen for an item's first roll), as Kairosdex does
    (bl4_weapondex/roll.py Game).
    """

    MISSING = -1.0e30
    PARAM_FLOAT = 8  # GbxParam: the value sits at +8
    constants: dict[str, float | None] = {}  # noqa: RUF012
    cells: dict[tuple[str, str, str], float] = {}  # noqa: RUF012

    @staticmethod
    def _pawn() -> Any:
        pawn = get_pc().Pawn if get_pc() else None
        if pawn is None:
            raise RerollError("Load into the game first.")
        return pawn

    @classmethod
    def constant(cls, name: str) -> float | None:
        if name not in cls.constants:
            lib = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject
            pawn = cls._pawn()
            try:
                value = float(lib.GetValueOfAttribute(name, pawn, cls.MISSING))
            except Exception:  # noqa: BLE001
                value = cls.MISSING
            cls.constants[name] = None if value == cls.MISSING or not math.isfinite(value) else value
        return cls.constants[name]

    @classmethod
    def cell(cls, table: str, row: str, column: str) -> float:
        key = (table, row, column)
        if key not in cls.cells:
            pawn = cls._pawn()
            value = 0.0
            try:
                tables = unrealsdk.find_object("ScriptStruct", "/Script/GbxEngine.GbxUEDataTableDef")
                handle = unrealsdk.make_struct("DataTableValueHandle", DataTable=FGbxDefPtr(table, tables), RowName=row, ColumnName=column)
                lib = unrealsdk.find_class("GbxDataTableFunctionLibrary").ClassDefaultObject
                param = lib.GetDataTableValueFromHandle(handle, pawn)[0]
                raw = _read(param._get_address() + cls.PARAM_FLOAT, 4) or bytes(4)
                bits = struct.unpack("<I", raw)[0]
                # Int columns come back as the int itself; as a float that would be a denormal,
                # which no table holds
                value = float(struct.unpack("<f", raw)[0]) if bits & 0x7F800000 else float(struct.unpack("<i", raw)[0])
            except Exception as ex:  # noqa: BLE001
                logging.dev_warning(f"[Reroll] table cell {key}: {ex!r}")
            cls.cells[key] = value
        return cls.cells[key]

    @classmethod
    def resolve(cls, spec: dict[str, Any]) -> float:
        """A weight / gate / chance (serialgen.Resolve)."""
        return serialgen.product(spec, cls.constant, cls.cell)


# endregion
# region Currency


def _currency(name: str) -> Any:
    pc = _pc()
    manager = pc.CurrencyManager if pc else None
    if manager is None:
        raise RerollError("Load into the game first.")
    for entry in manager.currencies:
        if entry.type.Name == name:
            return entry
    raise RerollError(f"No {name} on this character.")


def balance(name: str = ERIDIUM) -> int:
    return int(_currency(name).Amount)


def set_balance(name: str, amount: int) -> int:
    """Sets a currency (the server's copy, which the game saves and shows). Returns the new amount.

    Writing `Amount` alone changes the value but not the UI (the inventory kept showing the old
    Eridium count). `OnRep_Currencies(old)` is the replication callback clients run: it compares
    the old list to the current one and announces what changed, so the host's UI updates too.
    """
    entry = _currency(name)
    manager = _pc().CurrencyManager
    if _Acting.pc is not None:
        # A friend's, on the host: the value replicates to them and their game runs the callback;
        # running it here would announce their change on the host's screen
        entry.Amount = max(0, int(amount))
        return int(entry.Amount)
    try:
        old = [copy.copy(e) for e in manager.currencies]
    except Exception:  # noqa: BLE001 - the value still changes, only the UI may lag
        old = None
    entry.Amount = max(0, int(amount))
    if old is not None:
        try:
            manager.OnRep_Currencies(old)
        except Exception as ex:  # noqa: BLE001
            logging.dev_warning(f"[Reroll] Couldn't refresh the currency display: {ex!r}")
    return int(entry.Amount)


# endregion
