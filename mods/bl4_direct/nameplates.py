"""
Teammate names over their heads for a direct-IP client (GitHub issue #3, follow-up).

On a client the game leaves another player's overhead nameplate without a name: the HUD entry is
there and shown (`isnametagvisible`), but `name` and `platform` are empty, so nothing is drawn.
The game fills them once, when the player's character first shows up, from its online user list
(PlayerState -> user lookup at 0x42AF24, name at 0x11790AC); a directly connected player isn't
in that list yet at that moment, and the game never fills them again (found 2026-11-07 in the
two-instance lab: called later, the same code does return the name).

The fix stays out of native code. While we're a client with other players in the game, this
publishes their names and PlayerIds keyed by their character's object index (the HUD entry's
`uniqueid`), and `ui/nameplate.js` (run in the HUD page through the BL4 UI Link) shows that name
in an entry the game left nameless, with the platform icon the game's own teammate panel shows for
that PlayerId (the panel reads the PlayerState, which works on a client). An entry the game named
itself is left alone.

Cost: called once a second; idle unless we're a client. As a client, one pass over PlayerArray
(at most 4 players) comparing object indexes with the last pass; names are rebuilt and the page
told only when someone joins, leaves or respawns. Solo and host register nothing.
"""

from __future__ import annotations

import sys
from typing import Any

from mods_base import ENGINE, get_pc

from . import coop, page

KEY = "bl4dcnames"
SCRIPT_URL = "coui://uiresources/bl4directconnect/nameplate.js"


class S:
    registered = False
    signature: tuple[int, ...] | None = None
    names: dict[str, dict[str, Any]] = {}  # str(character InternalIndex) -> {n: name, id: PlayerId}


def _link() -> Any:
    # The Mod Menu's copy of the BL4 UI Link (always installed; it ticks the link every frame)
    return sys.modules.get("bl4_mod_menu.uilink")


def _state() -> dict[str, Any]:
    return {"names": S.names}


def _stop(link: Any) -> None:
    if S.registered:
        link.unregister(KEY)
        S.registered = False
    S.signature = None
    S.names = {}


def tick() -> None:
    """About once a second, on the game thread."""
    link = _link()
    if link is None:
        return
    if not coop.is_client():
        if S.registered:
            _stop(link)
        return
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    state = world.GameState if world is not None else None
    if state is None:
        return
    pc = get_pc()
    own = pc.PlayerState if pc is not None else None
    players = state.PlayerArray
    signature: list[int] = []
    others: list[Any] = []
    for i in range(len(players)):
        ps = players[i]
        if ps is None or ps == own:
            continue
        pawn = ps.PawnPrivate
        signature += (ps.InternalIndex, pawn.InternalIndex if pawn is not None else 0)
        others.append((ps, pawn))
    if tuple(signature) == S.signature:
        return
    S.signature = tuple(signature)
    names = {
        str(pawn.InternalIndex): {"n": str(ps.PlayerNamePrivate), "id": ps.PlayerId}
        for ps, pawn in others
        if pawn is not None
    }
    if not S.registered:
        page.publish(force=True)
        link.register(KEY, SCRIPT_URL, _state, lambda _action: None, views=(link.HUD,), mod="BL4 Direct Connect")
        S.registered = True
    if names != S.names:
        S.names = names
        link.changed(KEY)


def stop() -> None:
    if (link := _link()) is not None:
        _stop(link)
