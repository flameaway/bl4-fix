"""
Python's cyclic garbage collector, kept off the long-lived objects (gc.freeze) and out of gameplay.

Every mod's data, code and caches live in one interpreter, and a full collection walks all of it on
whatever thread allocated last, usually the game thread inside a hook: 35 ms for the ~85k live
objects of a normal session, 209 ms with the garbage of many hot reloads, 108 ms seen in the middle
of a roll burst (2026-10-24). gc.freeze() moves every object alive at that moment out of the
collector's view; later collections only walk what was made since (0.0 ms measured right after).

- First tick (normally the title screen): collect, then freeze.
- Each new world (seen at the 5 s check after a load): collect what's new since the last freeze,
  then freeze that too. Every FULL_EVERY worlds: unfreeze, collect everything, freeze again, so
  cyclic garbage among frozen objects (they're still freed by reference counting, just never by
  the cycle collector while frozen) can't pile up for a whole session.
- Full collections never start by themselves during play (2026-10-29): with a freeze only at world
  loads, the unfrozen objects of a long open-world session grew until an automatic full pass took
  9.9 ms mid-fight (Mod Profiler). Now the third threshold is set out of reach (young passes stay
  as they were: 0.04-0.5 ms) and the pass runs at a quiet moment instead (a menu that has been up a
  while, or loading), at most every QUIET_MIN_S, then freezes what survived, so each one stays
  small. At least every FULL_EVERY_S, on a loading screen (2026-10-30: in a menu the 16-28 ms pass
  landed as the pause menu opened), everything is unfrozen and collected; a settled menu only once
  FULL_OVERDUE_S passed without a load. A new world's pass also waits for the loading screen (at
  most WORLD_WAIT_S). With no quiet moment for PLAY_MAX_S, it collects during play anyway (small:
  everything older is frozen).
- Off: the old thresholds, unfreeze; the collector works as before.
"""

from __future__ import annotations

import gc
import time

FULL_EVERY = 10  # worlds between full collections
QUIET_MIN_S = 20.0
FULL_EVERY_S = 1800.0
PLAY_MAX_S = 600.0
# A full pass waits for a loading screen; with none for this long, a settled menu will do
FULL_OVERDUE_S = 7200.0
# A new world's pass waits this long at most for a loading screen / settled menu
WORLD_WAIT_S = 60.0
# The third threshold while we schedule full collections ourselves
GEN2_OFF = 1_000_000


class _S:
    world: int | None = None
    worlds = 0
    active = False
    last: str = ""
    thresholds: tuple[int, int, int] | None = None  # before ours
    collected_at = 0.0  # the last collection of ours, any kind
    full_at = 0.0  # the last one with everything unfrozen
    world_seen_for: int | None = None  # a new world waiting for its pass, and since when
    world_seen_at = 0.0


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _collect(*, full: bool, why: str) -> str:
    start = time.perf_counter()
    if full:
        gc.unfreeze()
    found = gc.collect()
    gc.freeze()
    now = time.monotonic()
    _S.collected_at = now
    if full:
        _S.full_at = now
    _S.last = f"{why}, {'full' if full else 'new objects'}: {found} collected in {_ms(start)} ms, {gc.get_freeze_count()} frozen"
    return _S.last


def step(world_address: int | None, *, quiet: bool = False, loading: bool = False) -> str | None:
    """
    Called from the mod's tick on the game thread every few seconds; returns what it did (for the
    log), if anything. quiet: a frame stall wouldn't be noticed now (a menu that has been up a
    while, or loading). loading: no character (a loading screen): the only time for full passes.
    """
    if world_address is None:
        return None
    if _S.thresholds is None:
        _S.thresholds = gc.get_threshold()
        t0, t1, _t2 = _S.thresholds
        gc.set_threshold(t0, t1, GEN2_OFF)
    now = time.monotonic()
    quiet = quiet or loading
    if world_address != _S.world:
        first = _S.world is None or not _S.active
        if first:
            _S.world_seen_at = now
        elif _S.world_seen_for != world_address:
            _S.world_seen_for, _S.world_seen_at = world_address, now
        # A new world's pass waits for the loading screen (or a settled menu), at most WORLD_WAIT_S:
        # a full one (every FULL_EVERY worlds) took 15-28 ms, a stall right as play starts otherwise
        full = first or (_S.worlds + 1) % FULL_EVERY == 0
        if not first and not (loading or (quiet and not full)) and now - _S.world_seen_at < WORLD_WAIT_S:
            return None
        _S.world = world_address
        _S.worlds += 1
        _S.active = True
        return _collect(full=full, why="new world")
    # Something reached the oldest generation since our last pass (count[2] = middle passes since)
    pending = gc.get_count()[2] > 0
    # The full pass (16-28 ms for ~70-80k objects, seen 2026-10-30) only while loading: in a menu it
    # landed as the pause menu opened, in its opening animation. A settled menu only once long overdue
    if now - _S.full_at >= FULL_EVERY_S and (loading or (quiet and now - _S.full_at >= FULL_OVERDUE_S)):
        return _collect(full=True, why="loading screen" if loading else "quiet moment, overdue")
    if quiet and pending and now - _S.collected_at >= QUIET_MIN_S:
        return _collect(full=False, why="quiet moment")
    if pending and now - _S.collected_at >= PLAY_MAX_S:
        return _collect(full=False, why=f"no quiet moment for {PLAY_MAX_S / 60:.0f} min")
    return None


def restore() -> bool:
    """Gives every frozen object back to the collector, and its thresholds. Any thread (gc is process-wide)."""
    if _S.thresholds is not None:
        gc.set_threshold(*_S.thresholds)
        _S.thresholds = None
    if not _S.active:
        return False
    gc.unfreeze()
    _S.active = False
    _S.world = None
    _S.world_seen_for = None
    return True
