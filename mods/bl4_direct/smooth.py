"""
Smoother enemies for friends: host-side engine settings (console variables) that make enemies
move more smoothly on friends' screens. Always on since 0.5.3 (0.5.2 had it as an option, off).

What they are (game build 26007962):
- gbx.nav.net.TransformPeriod / gbx.nav.net.PathPeriod (game: 0.25 s). Gearbox doesn't stream an
  enemy's position every frame: the host sends its position and planned path, and each client
  moves the enemy along that path by itself. The exe's help text: "Once a new X is sent to
  clients, it will not send another X for this long (in seconds)". When the client's guess was
  wrong (a dodge, a leap), the next update snaps the enemy into place. We set 0: no throttle, an
  enemy's update goes out whenever replication runs (still capped by the actor's own update rate
  and the budget below), so smaller and fewer snaps.
- gbx.net.ReplicationBudgetSeconds (game: 0.0025). How much host CPU per frame goes into choosing
  and packing what to send; whatever doesn't fit waits a frame, players first, so enemies are what
  gets skipped in big fights. No help text ships; meaning from the name + a test (starving it to
  0.1 ms cut 25% of traffic). Costs nothing until a fight is big enough to hit the cap.

Measured (docs/networking-performance.md 4.9-4.10, two copies on one PC, simulated 40 +- 15 ms /
1% loss link, ~11 enemies, alternating 2-minute halves): hidden position snaps per enemy-minute
0.25 s 3.2 -> 0.1 s 1.7 -> 0 1.0 (lower at each step in both rounds), visible teleports 0.8 ->
0.56 -> 0.43 (noisy), host fps unchanged, traffic to the client 8.9 -> 9.1 -> 16.5 KB/s.

Only the host's values matter, so they're held only while this game hosts with at least one
friend connected (direct IP or SHiFT); solo and as a client nothing changes. Each value is read
before our first change and written back when the mod is disabled or the last friend leaves. The game might set them again (map load): the tick re-checks them once a second.
"""

from __future__ import annotations

import threading
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging

from . import frametick

WANTED: dict[str, float] = {
    "gbx.nav.net.TransformPeriod": 0.0,
    "gbx.nav.net.PathPeriod": 0.0,
    "gbx.net.ReplicationBudgetSeconds": 0.005,
}
CLEANUP_KEY = "bl4_direct.smooth_cleanup"  # a one-shot frametick callback
HOLD = True  # lab A/B runs only (dev/netlab_suite_run.py sets it False through the bridge): no player switch


class S:
    ksl: Any = None
    original: dict[str, float] = {}  # cvar -> the game's value before we changed it
    game_thread: int | None = None


def _ksl() -> Any:
    if S.ksl is None:
        S.ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    return S.ksl


def _same(a: float, b: float) -> bool:
    # Float cvars come back as float32 (0.0025 reads 0.0024999999)
    return abs(a - b) <= 1e-6 + 1e-4 * abs(b)


def _set(name: str, value: float) -> bool:
    pc = get_pc()
    if pc is None:
        return False
    _ksl().ExecuteConsoleCommand(pc, f"{name} {value:g}", pc)
    return True


def hosting_friends() -> bool:
    """This game is a server with at least one friend connected."""
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    driver = world.NetDriver if world is not None else None
    return driver is not None and driver.ServerConnection is None and len(driver.ClientConnections) > 0


def tick() -> None:
    """Called once a second on the game thread: hold our values while hosting friends, else put the game's back."""
    S.game_thread = threading.get_ident()
    if not (HOLD and hosting_friends()):
        if S.original:
            restore()
        return
    ksl = _ksl()
    for name, value in WANTED.items():
        cur = ksl.GetConsoleVariableFloatValue(name)
        if _same(cur, value):
            continue
        S.original.setdefault(name, cur)
        if _set(name, value):
            logging.dev_warning(f"[BL4 Direct] smoother enemies: {name} {cur:g} -> {value:g}")


def restore() -> None:
    """Puts back the game's values (game thread only)."""
    for name in list(S.original):
        if _set(name, S.original[name]):
            logging.dev_warning(f"[BL4 Direct] smoother enemies: {name} back to {S.original[name]:g}")
            del S.original[name]


def cleanup() -> None:
    """Restore on the game thread: now if we're on it, else on its next tick (mod disabled from elsewhere)."""
    if not S.original:
        return
    if S.game_thread is None or threading.get_ident() == S.game_thread:
        restore()
        return

    def once() -> None:
        try:
            frametick.remove(CLEANUP_KEY)
            restore()
        except Exception as ex:  # noqa: BLE001 - never break the game's tick
            logging.error(f"[BL4 Direct] smoother enemies restore: {ex!r}")

    frametick.add(CLEANUP_KEY, once)
