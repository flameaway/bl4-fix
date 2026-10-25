"""
Python's cyclic garbage collector, kept off the long-lived objects (gc.freeze).

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
- Off: unfreeze; the collector works as before.
"""

from __future__ import annotations

import gc
import time

FULL_EVERY = 10  # worlds between full collections


class _S:
    world: int | None = None
    worlds = 0
    active = False
    last: str = ""


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def step(world_address: int | None) -> str | None:
    """Called from the mod's tick on the game thread; returns what it did (for the log), if anything."""
    if world_address is None or world_address == _S.world:
        return None
    first = _S.world is None or not _S.active
    _S.world = world_address
    _S.worlds += 1
    start = time.perf_counter()
    full = first or _S.worlds % FULL_EVERY == 0
    if full:
        gc.unfreeze()
    found = gc.collect()
    gc.freeze()
    _S.active = True
    _S.last = f"{'full' if full else 'new objects'}: {found} collected in {_ms(start)} ms, {gc.get_freeze_count()} frozen"
    return _S.last


def restore() -> bool:
    """Gives every frozen object back to the collector. Any thread (gc is process-wide)."""
    if not _S.active:
        return False
    gc.unfreeze()
    _S.active = False
    _S.world = None
    return True
