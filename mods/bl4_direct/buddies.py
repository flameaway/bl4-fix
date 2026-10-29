"""
Co-op HUD teammate panel: makes the game's panel list every other player (GitHub issue #3).

The panel (name, class icon, shield/health bars over the player's own HUD, the game's
`buddywidget` page) is filled by the native OakUIDataCollector_BuddyInfo (one per local player).
It shows a player only once that player is in its queue, and it gets there two ways: the
UFunction `OnPlayerConnected(PlayerState)`, or a one-off scan of `GameState.PlayerArray`. Once a
player is queued, the collector's own tick retries every frame (while it has more queued than
shown) until the player's character is ready, then `AddBuddy`s them and keeps the entry updated.

In direct-IP co-op a player sometimes never gets queued: the scan had already run and nothing
called `OnPlayerConnected` (seen both ways in the two-instance lab: the host missing the client on
2026-10-25, a player's client missing the host in the issue). What normally calls it wasn't found
(no reflected delegate, no string in the exe); a SHiFT party event is suspected, unproven.

So when the players change (someone joins or leaves, or a new map), this compares `PlayerArray`
with the collector's queue and list once a second until every other player is accounted for
(queued or shown), then goes idle until the players change again. A player missing from both for
SETTLE_CHECKS checks in a row (giving the game's own path first go) gets `OnPlayerConnected`; the
game does the rest. It also undoes a player queued twice (the collector would retry every frame
for good), and calls `OnPlayerDisconnected` for a player who left while still on the panel.

The queue and list aren't reflected, so this reads them at raw offsets (build of 2026-10-22):
shown list TArray at +0x58 (entries 0x88 bytes, PlayerId at +0x10), queue TArray<FWeakObjectPtr>
at +0x78 (ObjectIndex, SerialNumber; the index is the UObject's InternalIndex at +0xC). Before
any read, `_verify` checks the game's own code uses exactly those offsets (the two UFunctions'
thunks call the vtable slots we expect, and those functions' bytes address +0x58/+0x60/+0x78/
+0x80 with a 0x88 stride); on any mismatch the fix steps aside for the session. After each
`OnPlayerConnected` the queue must have grown by that player, else it steps aside too.

Cost: idle (solo, or everyone accounted for), one pass over `PlayerArray` a second comparing its
object indexes with the last ones (at most 4 players). The collector's lists are only read for
the few seconds after the players change. Finding the collector is one `find_all` (9-16 ms measured) once per game session, the first
time another player is in the game.
"""

from __future__ import annotations

import ctypes
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

COLLECTOR_CLASS = "OakUIDataCollector_BuddyInfo"
SETTLE_CHECKS = 3  # a player missing this many checks in a row (~1 s apart) gets queued
MAX_ENTRIES = 64  # sanity bound on the native arrays' counts

# Offsets inside the collector (checked against the game's code by _verify)
SHOWN_DATA, SHOWN_COUNT, SHOWN_STRIDE, SHOWN_PLAYER_ID = 0x58, 0x60, 0x88, 0x10
QUEUE_DATA, QUEUE_COUNT = 0x78, 0x80
OBJECT_INDEX = 0xC  # UObjectBase::InternalIndex (verified live against a queued PlayerState)
UFUNCTION_FUNC = 0xD8  # UFunction::Func, the native thunk

# What the game's code must contain for the offsets above to be right
CONNECTED_THUNK = bytes.fromhex("ff90d0020000")  # call [rax+0x2d0]: OnPlayerConnected impl
ADD_THUNK = bytes.fromhex("ff90c0020000")  # call [rax+0x2c0]: AddBuddy impl
CONNECTED_IMPL = (
    bytes.fromhex("488d7178"),  # lea rsi, [rcx+0x78]          queue array
    bytes.fromhex("4863bb80000000"),  # movsxd rdi, [rbx+0x80]  queue count
)
ADD_IMPL = (
    bytes.fromhex("4c8d6358"),  # lea r12, [rbx+0x58]           shown array
    bytes.fromhex("48634360"),  # movsxd rax, [rbx+0x60]        shown count
    bytes.fromhex("48c1e1074c8d04c1"),  # shl rcx,7; lea r8,[rcx+rax*8]: count * 0x88
    bytes.fromhex("4439740a10"),  # cmp [rdx+rcx+0x10], r14d    PlayerId in an entry
)


class S:
    collector: Any = None  # WeakPointer
    search_failed = False  # find_all found nothing: don't walk every object again
    verified: bool | None = None  # None until checked; False = stepped aside for the session
    world: Any = None  # WeakPointer: tracking below resets on a new world
    signature: tuple[int, ...] | None = None  # PlayerArray's object indexes at the last full check
    settled = False  # every player was queued or shown at that check: idle until signature changes
    missing: dict[int, int] = {}  # PlayerState object index -> checks in a row it was missing
    known: dict[int, tuple[Any, int]] = {}  # object index -> (WeakPointer to the PS, PlayerId)
    logged: set[str] = set()


def _log_once(key: str, msg: str) -> None:
    if key not in S.logged:
        S.logged.add(key)
        logging.warning(f"[BL4 Direct] teammate panel: {msg}")


def _u64(address: int) -> int:
    return ctypes.c_uint64.from_address(address).value


def _i32(address: int) -> int:
    return ctypes.c_int32.from_address(address).value


def _code(address: int, size: int) -> bytes:
    data = ctypes.string_at(address, size)
    if data[:1] == b"\xe9":  # an incremental-link style jump stub: follow it once
        rel = int.from_bytes(data[1:5], "little", signed=True)
        data = ctypes.string_at(address + 5 + rel, size)
    return data


def _image_range() -> tuple[int, int]:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    base = kernel32.GetModuleHandleW(None) or 0
    # SizeOfImage from the PE optional header
    pe = base + _i32(base + 0x3C)
    return base, base + _i32(pe + 0x18 + 0x38)


def _verify(collector: Any) -> bool:
    """True if the game's code uses the offsets this module reads (checked once per session)."""
    lo, hi = _image_range()

    def in_image(address: int) -> bool:
        return lo <= address < hi

    cls = collector.Class
    connected = cls._find("OnPlayerConnected")
    add = cls._find("AddBuddy")
    thunks = {name: _u64(fn._get_address() + UFUNCTION_FUNC) for name, fn in (("c", connected), ("a", add))}
    if not all(in_image(t) for t in thunks.values()):
        return False
    if CONNECTED_THUNK not in _code(thunks["c"], 0x80) or ADD_THUNK not in _code(thunks["a"], 0x100):
        return False
    vtable = _u64(collector._get_address())
    if not in_image(vtable):
        return False
    connected_impl = _u64(vtable + 0x2D0)
    add_impl = _u64(vtable + 0x2C0)
    if not (in_image(connected_impl) and in_image(add_impl)):
        return False
    connected_code = _code(connected_impl, 0x60)
    add_code = _code(add_impl, 0xA0)
    return all(p in connected_code for p in CONNECTED_IMPL) and all(p in add_code for p in ADD_IMPL)


def _collector() -> Any:
    if S.collector is not None:
        found = S.collector()
        if found is not None:
            return found
        S.collector = None
    if S.search_failed:
        return None
    # Outer is the local player, which lives for the whole session: found once
    for obj in unrealsdk.find_all(COLLECTOR_CLASS, exact=True):
        if not obj.Name.startswith("Default__"):
            S.collector = WeakPointer(obj)
            return obj
    S.search_failed = True
    return None


def _count(address: int) -> int:
    n = _i32(address)
    return n if 0 <= n <= MAX_ENTRIES else -1


def _shown_ids(base: int) -> list[int] | None:
    n = _count(base + SHOWN_COUNT)
    if n < 0:
        return None
    data = _u64(base + SHOWN_DATA)
    if n and not data:
        return None
    return [_i32(data + i * SHOWN_STRIDE + SHOWN_PLAYER_ID) for i in range(n)]


def _queued_indexes(base: int) -> list[int] | None:
    n = _count(base + QUEUE_COUNT)
    if n < 0:
        return None
    data = _u64(base + QUEUE_DATA)
    if n and not data:
        return None
    return [_i32(data + i * 8) for i in range(n)]


def _step_aside(msg: str) -> None:
    S.verified = False
    _log_once("aside", f"{msg}; not touching the panel this session.")


def tick() -> None:
    """Called about once a second on the game thread."""
    if S.verified is False:
        return
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    state = world.GameState if world is not None else None
    if state is None:
        return
    if S.world is None or S.world() != world:
        S.world = WeakPointer(world)
        S.missing.clear()
        S.known.clear()
        S.signature = None
        S.settled = False
        return
    players = state.PlayerArray
    # Idle while nobody joined or left since every player was last accounted for
    signature = tuple(_i32(ps._get_address() + OBJECT_INDEX) for ps in players if ps is not None)
    if S.settled and signature == S.signature:
        return
    S.signature = signature
    S.settled = False
    if len(signature) <= 1 and not S.known:
        S.settled = True  # solo (or the last friend's leave was already handled)
        return
    pc = get_pc()
    own = pc.PlayerState if pc is not None else None
    if own is None or pc.Pawn is None:
        return

    collector = _collector()
    if collector is None:
        _log_once("missing", "the game's teammate panel wasn't found")
        return
    if S.verified is None:
        S.verified = _verify(collector)
        if not S.verified:
            _step_aside("the game's code changed (a game patch?)")
            return
    base = collector._get_address()
    shown = _shown_ids(base)
    queued = _queued_indexes(base)
    if shown is None or queued is None:
        _step_aside("its lists read wrong")
        return

    current: dict[int, Any] = {}
    for ps in players:
        if ps is None or ps == own:
            continue
        index = _i32(ps._get_address() + OBJECT_INDEX)
        current[index] = ps
        S.known[index] = (WeakPointer(ps), ps.PlayerId)

    for index, ps in current.items():
        player_id = ps.PlayerId
        times = queued.count(index)
        if times > 1:
            # Queued twice (the game and us): the collector would retry every frame for good
            collector.OnPlayerDisconnected(ps)
            collector.OnPlayerConnected(ps)
            logging.info(f"[BL4 Direct] teammate panel: {ps.PlayerNamePrivate} was queued {times} times, fixed")
            S.missing.pop(index, None)
            continue
        if times or player_id in shown:
            S.missing.pop(index, None)
            continue
        S.missing[index] = S.missing.get(index, 0) + 1
        if S.missing[index] < SETTLE_CHECKS:
            continue
        before = _count(base + QUEUE_COUNT)
        collector.OnPlayerConnected(ps)
        after = _queued_indexes(base)
        if after is None or len(after) != before + 1 or after[-1] != index:
            _step_aside("adding a player didn't queue them as expected")
            return
        S.missing.pop(index, None)
        logging.info(f"[BL4 Direct] teammate panel: added {ps.PlayerNamePrivate} (the game hadn't)")

    # Players who left: take them off the panel if the game didn't
    for index in [i for i in S.known if i not in current]:
        weak, player_id = S.known.pop(index)
        S.missing.pop(index, None)
        if player_id not in shown:
            continue
        ps = weak()
        if ps is not None:
            collector.OnPlayerDisconnected(ps)
            logging.info(f"[BL4 Direct] teammate panel: removed {ps.PlayerNamePrivate}, who left")
        else:
            _log_once(f"stale{player_id}", f"player {player_id} left but is still on the panel")

    # Everyone is queued or shown (the game takes it from there): idle until the players change
    S.settled = not S.missing
