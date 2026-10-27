"""
The board in co-op, through BL4 Direct's co-op channel (bl4_direct/coop.py).

Damage is dealt on the host, so the host's copy of this mod counts everyone (tracker.py binds each
player's character there). A client's own copy counts nothing; while its Carnage Meter screen is
open it asks the host for the board once a second (`carnage.board`), and the host answers with
it, the asker marked as "you". No traffic at all while no client has the screen open.

A client whose host doesn't run Carnage Meter (or no BL4 Direct) gets `host_missing`: the screen
says the host needs it.
"""

from __future__ import annotations

import time
from typing import Any

from . import tracker

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct isn't installed: solo / host-only
    coop = None

FEATURE = "carnage"
KIND = "carnage.board"
REPORT = "carnage.report"
ASK_S = 1.0
STALE_S = 4.0


class S:
    enabled = False
    asked_at = 0.0
    waiting = False
    board: dict[str, Any] | None = None
    board_at = 0.0
    error = ""
    report: dict[str, Any] | None = None  # the host's last boss report (client)


def role() -> str:
    if coop is None:
        return "solo"
    try:
        return coop.role()
    except Exception:  # noqa: BLE001
        return "solo"


def _serve(req: Any) -> dict[str, Any]:
    board = tracker.board()
    asker = req.controller()
    ps = asker.PlayerState if asker is not None else None
    me = ps._get_address() & 0xFFFFFFFF if ps is not None else None
    for player in board["players"]:
        player["local"] = player["id"] == me
    return {"ok": True, "board": board}


def install(enabled: Any) -> None:
    S.enabled = True
    if coop is None:
        return
    coop.feature(FEATURE, enabled)
    coop.handle(KIND, _serve)
    coop.on_push(REPORT, _got_report)


def push_report(report: dict[str, Any]) -> None:
    """As host: every friend running Carnage Meter gets the boss report (their screen opens on it)."""
    if coop is not None:
        coop.push_all(REPORT, report, FEATURE)


def _got_report(report: dict[str, Any]) -> None:
    # The host marks its own player as "you": mark ours instead, by name (ids are host addresses)
    try:
        from mods_base import get_pc  # noqa: PLC0415

        pc = get_pc(possibly_loading=True)
        me = str(pc.PlayerState.GetPlayerName()) if pc is not None and pc.PlayerState is not None else None
    except Exception:  # noqa: BLE001
        me = None
    for player in report.get("players", []):
        player["local"] = me is not None and player.get("name") == me
    S.report = report


def uninstall() -> None:
    S.enabled = False
    S.board = None


def host_status() -> str:
    """As a client: "ok" (the host runs Carnage Meter), "missing" (a modded host without it) or
    "unknown" (no hello from the host: unmodded, or not yet)."""
    if coop is None:
        return "unknown"
    if coop.host_has(FEATURE):
        return "ok"
    return "missing" if coop.host_known() else "unknown"


def _got(reply: dict[str, Any]) -> None:
    S.waiting = False
    if reply.get("ok") and isinstance(reply.get("board"), dict):
        S.board, S.board_at, S.error = reply["board"], time.monotonic(), ""
    else:
        S.error = str(reply.get("error") or "no answer from the host")


def client_board(screen_open: bool) -> dict[str, Any] | None:
    """As a client: the host's board (asked for while our screen is open), None if we have none."""
    now = time.monotonic()
    if coop is not None and screen_open and not S.waiting and now - S.asked_at >= ASK_S and coop.host_has(FEATURE):
        S.asked_at, S.waiting = now, True
        coop.request(KIND, {}, _got, timeout=5.0)
    if S.board is not None and now - S.board_at > STALE_S * 4:
        S.board = None
    return S.board


def forget() -> None:
    """A new game session: the host's board and report were from the last one."""
    S.board, S.report, S.error = None, None, ""


def stale() -> bool:
    return S.board is None or time.monotonic() - S.board_at > STALE_S
