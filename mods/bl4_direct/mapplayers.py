"""
ECHO map players for a direct-IP client: the map's player icons and where it opens (GitHub issue #4).

The ECHO map draws players from a native per-world player list that a PlayerState joins in
`RegisterPlayerWithSession` (OakPlayerState's override of the engine virtual, vtable +0x6B0, adds
it once its PlayerId is set; the list ignores a player it already holds). The host calls that for
everyone through its GameSession. A client only calls it from `OnRep_UniqueId`, and in direct-IP
co-op that never fired on the client (2026-11-07, both copies offline: no UniqueID arrived), so the
client's list stayed empty. The map then had no player icons at all (not even the player's own)
and opened zoomed out over the whole world instead of on the player.

So as a client, this calls `OnRep_UniqueId` (a UFunction: the engine's own path, which calls a
virtual at vtable +0x6F8, not yet identified, then RegisterPlayerWithSession) once for each player
whose UniqueID shows empty and whose PlayerId has arrived. Verified in the two-instance lab
(2026-11-07): the client's list went from 0 players to 2, and the map opened on the player with
both players' icons, like the host's.

`ReplicationBytes` is only a cache filled when the id is sent: signed into SHiFT, a player has a
real id (its SHiFT account id) while it still reads empty (2026-11-08). Whether that id reaches a
direct-IP client hasn't been checked yet.

Cost: once a second, a pass over `PlayerArray` (at most 4 players) comparing object indexes with
the ones already handled.
"""

from __future__ import annotations

import ctypes
from typing import Any

from mods_base import ENGINE
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

OBJECT_INDEX = 0xC  # UObjectBase::InternalIndex (as buddies.py)


class S:
    stopped = False  # set after an error: no retry every second
    world: Any = None  # WeakPointer: the handled set below is per world
    done: set[int] = set()  # noqa: RUF012  # object indexes of PlayerStates handled in this world


def tick() -> None:
    """Called about once a second on the game thread."""
    if S.stopped:
        return
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    if world is None:
        return
    if S.world is None or S.world() != world:
        S.world = WeakPointer(world)
        S.done.clear()
    driver = world.NetDriver
    state = world.GameState
    if driver is None or driver.ServerConnection is None or state is None:
        return  # solo or hosting: the game registers everyone itself
    for ps in state.PlayerArray:
        if ps is None:
            continue
        index = ctypes.c_int32.from_address(ps._get_address() + OBJECT_INDEX).value
        if index in S.done:
            continue
        if len(ps.UniqueID.ReplicationBytes):
            S.done.add(index)  # a real id: its OnRep_UniqueId registered it
            continue
        if ps.PlayerId == -1:
            continue  # the registration needs the PlayerId: next second
        ps.OnRep_UniqueId()
        S.done.add(index)
        logging.info(f"[BL4 Direct] ECHO map: registered {ps.Name} (the game hadn't)")
