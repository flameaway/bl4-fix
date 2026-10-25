"""
Reading a build off the character and putting one back. Call everything here on the game thread.

A build is:

- gear: for each equip slot (4 weapons, shield, grenade/ordnance, repkit, enhancement, class
  mod), the item's serial. A serial is the whole item (type, level, every part), so "the same
  item" means "an item with the same serial".
- skills: for each of the character's progress graphs (action skill, augments and capstone, the
  three skill trees, specializations), the points spent on each node, or for graphs where you
  pick nodes instead of spending points (action skill, augments, capstone, specialization perks)
  which nodes are picked. SDU upgrades and vault powers aren't part of a build.

Loading equips each saved item that's still in the backpack into its slot (native.py) and leaves
the slot alone if it isn't: we never equip a look-alike. Skills are put back through the
graphs' own server calls (the same ones the skill screen uses), only where they differ.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import ctypes
from dataclasses import dataclass, field
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk.unreal import UObject, WeakPointer

from . import native

# FInventoryIdentity: the serial's text pointer and length (see bl4_reroll/game.py)
SERIAL_PTR = 0xA0
SERIAL_LEN = 0xB0
MAX_SERIAL = 4096
EQUIPPED_FLAG = 1
# EGbxProgressGraphType
POINTS, ACTIVATION = 0, 1
# Graphs in the character's group that aren't part of a build
NOT_BUILD = ("vaultpower",)
# Spending can need a few rounds: a tier unlocks once the one before it has its points
SPEND_ROUNDS = 12

_k32 = ctypes.windll.kernel32
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
_k32.ReadProcessMemory.restype = ctypes.c_int
_process = _k32.GetCurrentProcess()


class BuildError(Exception):
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
    head = _read(identity._get_address(), SERIAL_LEN + 8)
    if head is None or len(head) < SERIAL_LEN + 8:
        return None
    pointer = int.from_bytes(head[SERIAL_PTR : SERIAL_PTR + 8], "little")
    length = int.from_bytes(head[SERIAL_LEN : SERIAL_LEN + 8], "little")
    if not 2 < length <= MAX_SERIAL:  # noqa: PLR2004
        return None
    text = _read(pointer, length)
    if text is None:
        return None
    serial = text.split(b"\0", 1)[0].decode("ascii", "replace")
    return serial if serial.startswith("@U") else None


# region Player


class _Acting:
    """The player a host-side request is for (None: the local player)."""

    pc: Any = None


@contextlib.contextmanager
def acting_for(pc: Any) -> Iterator[None]:
    """On the host: everything in game.py reads and changes this player instead of ours."""
    old, _Acting.pc = _Acting.pc, pc
    try:
        yield
    finally:
        _Acting.pc = old


def _player() -> tuple[Any, Any, Any]:
    pc = _Acting.pc or get_pc()
    pawn = pc.Pawn if pc else None
    state = pc.PlayerState if pc else None
    if pawn is None or state is None or not hasattr(pawn, "GbxProgressionManager"):
        raise BuildError("Load into the game first.")
    return pc, pawn, state


class _Host:
    """is_host() per world: a world never changes between host and client."""

    world = WeakPointer()
    value = False


def is_host() -> bool:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return False
    if _Host.world() is not world:
        _Host.value = bool(unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject.IsServer(world))
        _Host.world = WeakPointer(world)
    return _Host.value


def character() -> str | None:
    """The id of the character being played (PlayerState.ActiveCharGuid), or None."""
    pc = get_pc(possibly_loading=True)
    state = getattr(pc, "PlayerState", None) if pc is not None else None
    if state is None:
        return None
    guid = state.ActiveCharGuid
    parts = [guid.A, guid.B, guid.C, guid.D]
    if not any(parts):
        return None
    return "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts)


# endregion
# region Gear


@dataclass
class Item:
    handle: int
    serial: str
    slot: int  # equip slot, or -1 in the backpack


def read_entry(entry: Any) -> Item | None:
    """One BackpackItems entry as an Item, or None for an empty slot (or an unreadable serial)."""
    inv = entry.InventoryItem
    handle = int(inv.Handle.Handle)
    if handle == -1:
        return None
    serial = read_serial(inv.Item.data.Identity)
    if serial is None:
        return None
    slot = int(inv.EquipSlot)
    equipped = bool(int(inv.Flags) & EQUIPPED_FLAG) and slot >= 0
    return Item(handle, serial, slot if equipped else -1)


def backpack() -> list[Item]:
    """Every item in the backpack, read now (~10 reads and two memory reads per item: not cheap)."""
    _, _, state = _player()
    return [item for entry in state.BackpackItems.items if (item := read_entry(entry)) is not None]


def backpack_signal() -> tuple[Any, ...]:
    """
    A cheap value that changes whenever the backpack or what's equipped changes (~20 reads, no
    serials): the backpack's fast-array replication key (bumped on the host by every add, remove
    or item change, since clients must hear of it) and the address of each equipped actor (a new
    one is spawned for every equip). Used only to decide when to re-read; never to act on.
    """
    _, pawn, state = _player()
    key = getattr(state.BackpackItems, "ArrayReplicationKey", None)
    try:
        slots = pawn.EquippedInventorySlots.items
        actors = tuple(
            actor._get_address() if (actor := slots[i].InstancedInventory) is not None else 0 for i in range(len(slots))
        )  # fmt: skip
    except AttributeError:
        actors = ()
    return key, actors


def _is_in(handle: int, slot: int) -> bool:
    """Whether the item with this handle is equipped in this slot (cheaper than backpack())."""
    _, _, state = _player()
    for entry in state.BackpackItems.items:
        inv = entry.InventoryItem
        if inv.Handle.Handle == handle:
            return bool(int(inv.Flags) & EQUIPPED_FLAG) and int(inv.EquipSlot) == slot
    return False


def equipped() -> dict[int, str]:
    """Equip slot -> serial of what's in it."""
    return {item.slot: item.serial for item in backpack() if item.slot >= 0}


_equipper: native.Equipper | None = None
_equipper_error: native.NotFound | None = None  # this game build doesn't have it: don't search again


def equipper() -> native.Equipper:
    global _equipper, _equipper_error
    if _equipper_error is not None:
        raise _equipper_error
    if _equipper is None:
        pc, _, _ = _player()
        try:
            _equipper = native.find(pc)
        except native.NotFound as ex:
            _equipper_error = ex
            raise
    return _equipper


@dataclass
class GearResult:
    equipped: list[int] = field(default_factory=list)  # slots changed
    kept: list[int] = field(default_factory=list)  # already right
    missing: list[int] = field(default_factory=list)  # item not in the backpack
    failed: list[int] = field(default_factory=list)  # the game refused (or a swap we couldn't make)


def apply_gear(wanted: dict[int, str]) -> GearResult:
    """
    Equips the item with each serial into its slot. Items still in the backpack go straight in
    (the item already in the slot goes back to the backpack). An item that's equipped in another
    slot gets freed first: equipping the right item over it sends it back to the backpack, and
    for a straight swap (two guns trading places) a spare item of the same kind stands in.
    """
    pc, pawn, _ = _player()
    eq = equipper()
    result = GearResult()
    items = backpack()
    have = {item.serial for item in items}
    todo = {}
    for slot, serial in wanted.items():
        if serial not in have:
            result.missing.append(slot)
        elif any(item.slot == slot and item.serial == serial for item in items):
            result.kept.append(slot)
        else:
            todo[slot] = serial

    def free_copy(serial: str, items: list[Item]) -> Item | None:
        return next((item for item in items if item.serial == serial and item.slot < 0), None)

    def equip(handle: int, slot: int) -> bool:
        eq.equip(pc, pawn, handle, slot)
        return _is_in(handle, slot)

    # Rounds: each equip of a backpack item can free an item another slot wants
    while todo:
        items = backpack()
        progress = False
        for slot, serial in list(todo.items()):
            item = free_copy(serial, items)
            if item is None:
                continue
            del todo[slot]
            if equip(item.handle, slot):
                result.equipped.append(slot)
            else:
                result.failed.append(slot)
            progress = True
            items = backpack()
        if progress:
            continue
        # Only swaps left: every wanted item sits in another slot we also want changed. Put a
        # spare item into one of those slots to free its item, then carry on.
        if not _stand_in(eq, pc, pawn, todo, wanted, items):
            result.failed += list(todo)
            break
    return result


def _stand_in(eq: native.Equipper, pc: Any, pawn: Any, todo: dict[int, str], wanted: dict[int, str],
              items: list[Item]) -> bool:  # fmt: skip
    busy = set(wanted.values())
    spares = [item for item in items if item.slot < 0 and item.serial not in busy]
    for serial in todo.values():
        holder = next((item for item in items if item.serial == serial and item.slot >= 0), None)
        if holder is None:
            continue
        # Any kind of item is tried (the serial doesn't say which slots it fits); the game
        # refuses the ones that don't fit, and each try only checks the one entry
        for spare in spares:
            eq.equip(pc, pawn, spare.handle, holder.slot)
            if _is_in(spare.handle, holder.slot):
                return True
    return False


# endregion
# region Skills


def _graph_name(graph: UObject) -> str:
    return str(graph.ProgressGraphDef._name)


def build_graphs() -> list[UObject]:
    """The character's own graphs (same group as its action skills), minus vault powers."""
    _, pawn, _ = _player()
    graphs = list(pawn.GbxProgressionManager.ProgressGraphs)
    groups = {}
    for graph in graphs:
        name = _graph_name(graph)
        if name.endswith("_ActionSkills"):
            groups[str(graph.GraphGroupDef._name)] = True
    return [
        g for g in graphs
        if str(g.GraphGroupDef._name) in groups and not _graph_name(g).lower().startswith(NOT_BUILD)
    ]  # fmt: skip


def _graph_type(graph: UObject) -> int:
    return int(graph.ProgressGraphDef.GraphType)


def _pool(graph: UObject) -> str:
    try:
        return str(graph.ProgressGraphDef.PointPool._name)
    except Exception:  # noqa: BLE001
        return ""


def read_graph(graph: UObject) -> dict[str, Any]:
    """One graph as read_skills() keeps it."""
    if _graph_type(graph) == ACTIVATION:
        return {"type": ACTIVATION, "active": _active(graph)}
    return {"type": POINTS, "points": _points(graph)}


def read_skills() -> dict[str, dict[str, Any]]:
    """{graph: {"type": 0, "points": {node: n}} or {"type": 1, "active": {node: level}}}"""
    return {_graph_name(graph): read_graph(graph) for graph in build_graphs()}


@dataclass
class SkillResult:
    changed: list[str] = field(default_factory=list)  # graphs we changed
    short: dict[str, int] = field(default_factory=dict)  # graph -> points we couldn't put back
    unpicked: list[str] = field(default_factory=list)  # "graph node" picks the game refused


def _points(graph: UObject) -> dict[str, int]:
    return {str(i): int(n.ProgressPointsSpent) for i, n in enumerate(graph.nodes) if n.ProgressPointsSpent > 0}


def _active(graph: UObject) -> dict[str, int]:
    return {str(i): int(n.ActivationLevel) for i, n in enumerate(graph.nodes) if n.bIsActivated}


def _wanted(graphs: dict[str, UObject], saved: Any, specializations: bool) -> dict[str, Any]:
    if not isinstance(saved, dict):
        return {}
    wanted = {name: data for name, data in saved.items() if name in graphs}
    if not specializations:
        wanted = {name: data for name, data in wanted.items() if "specialization" not in name.lower()}
    return wanted


def _fits(graph: UObject, data: Any) -> bool:
    """Whether a saved graph can be put back on this one: same kind, every node index there."""
    if not isinstance(data, dict) or int(data.get("type", _graph_type(graph))) != _graph_type(graph):
        return False
    entries = data.get("active" if _graph_type(graph) == ACTIVATION else "points", {})
    if not isinstance(entries, dict):
        return False
    count = len(graph.nodes)
    try:
        return all(0 <= int(index) < count and int(value) >= 0 for index, value in entries.items())
    except (TypeError, ValueError):
        return False


def check_skills(saved: dict[str, Any], *, specializations: bool = True) -> list[str]:
    """The saved graphs that don't fit the character's (a game patch changed them, or a bad file)."""
    graphs = {_graph_name(g): g for g in build_graphs()}
    return [name for name, data in _wanted(graphs, saved, specializations).items() if not _fits(graphs[name], data)]


def apply_skills(saved: dict[str, dict[str, Any]], *, specializations: bool = True) -> SkillResult:
    """
    Puts the saved skill points and picks back. Points are refunded and spent again per point
    pool (the skill trees share one; specializations have their own), and only if something in
    that pool differs. Picks are reset group by group and picked again, only in graphs that
    differ (a graph's nodes outside its groups, like the class trait, are never touched).

    Every saved graph is checked first (check_skills): nothing is refunded unless all of it fits.
    """
    result = SkillResult()
    graphs = {_graph_name(g): g for g in build_graphs()}
    wanted = _wanted(graphs, saved, specializations)
    if bad := [name for name, data in wanted.items() if not _fits(graphs[name], data)]:
        raise BuildError(f"The saved skills don't fit this character any more ({', '.join(bad)}); skills left as they are.")

    # Points, per pool
    pools: dict[str, list[str]] = {}
    for name, graph in graphs.items():
        if name in wanted and _graph_type(graph) == POINTS:
            pools.setdefault(_pool(graph), []).append(name)
    for names in pools.values():
        target = {name: {k: int(v) for k, v in wanted[name].get("points", {}).items()} for name in names}
        if all(_points(graphs[name]) == target[name] for name in names):
            continue
        for name in names:
            graphs[name].Server_ResetSpentPoints()
        for _ in range(SPEND_ROUNDS):
            progress = False
            for name in names:
                graph = graphs[name]
                for index, points in target[name].items():
                    have = int(graph.nodes[int(index)].ProgressPointsSpent)
                    if have < points:
                        graph.Server_SpendProgressPoints(int(index), points - have)
                        progress |= int(graph.nodes[int(index)].ProgressPointsSpent) != have
            if not progress:
                break
        for name in names:
            missing = sum(max(0, p - int(graphs[name].nodes[int(i)].ProgressPointsSpent)) for i, p in target[name].items())
            if missing:
                result.short[name] = missing
            result.changed.append(name)

    # Picks (after points: augments and capstones unlock from tree points)
    for name, graph in graphs.items():
        if name not in wanted or _graph_type(graph) != ACTIVATION:
            continue
        target = {k: int(v) for k, v in wanted[name].get("active", {}).items()}
        if _active(graph) == target:
            continue
        for group in range(len(graph.Groups)):
            graph.Server_ResetActivationsInGroup(group)
        for index, level in target.items():
            node = graph.nodes[int(index)]
            if node.bIsActivated:
                continue
            # A node knows nothing of its group; only the right one accepts it
            for group in range(len(graph.Groups)):
                graph.Server_ActivateNodeInGroup(group, int(index), True, level)
                if node.bIsActivated:
                    break
            if not node.bIsActivated:
                result.unpicked.append(f"{name} {index}")
        result.changed.append(name)
    return result


# endregion
