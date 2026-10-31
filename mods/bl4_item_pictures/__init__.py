"""
Instant Item Pictures for Borderlands 4: your backpack's item pictures are ready before you open it.

The game draws each item's picture (the inventory and card thumbnails) on demand, slowly (one
every 1/15 s after 5 warm-up frames, plus loading the item), and throws them all away whenever it
holds more than 100 or every 5 minutes. So the backpack and bank kept drawing their pictures again.
This mod keeps the game's own pictures (same renderer, same size) for every item you carry, and
your bank's too if you want, and draws missing ones in the background, one at a time, while the
game has nothing of its own to draw. See pictures.py. And the menu page holds every picture
loaded (page.py, ui/pictures.js), so the inventory's reused cells switch pictures without going
blank while you scroll.

Local only: pictures are drawn on each player's own machine, host or client.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from mods_base import SETTINGS_DIR, BoolOption, build_mod, command, get_pc
from unrealsdk import logging

from . import frametick, health, page, pictures

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

TICK_KEY = "bl4_item_pictures"
TICK_S = 0.25
LINK_KEY = "bl4_item_pictures.link"
GAME_DIR = SETTINGS_DIR.parent.parent
PATHS_PER_TICK = 32  # pictures' paths resolved per tick for the page (~40 us each)
PLAY_GAP_S = 0.25  # between our requests in play (each one is a scene capture over a few frames)
RETRY_S = 30.0  # a picture asked for that never came: ask again after this long
MAX_TRIES = 3


class S:
    backpack = pictures.Wanted()
    bank = pictures.Wanted()
    wanted: list[str] = []  # noqa: RUF012 - every serial we keep, equipped first
    wanted_set: frozenset[str] = frozenset()
    present: set[str] | None = None  # serials with a finished picture, read when the count changes
    present_count = -1
    missing: list[str] = []  # noqa: RUF012 - wanted without a picture, in order (asked ones included)
    asked: dict[str, tuple[float, int]] = {}  # noqa: RUF012 - serial -> (when, how many times)
    last_request = 0.0
    sweeping_since = 0.0  # our quiet sweep: when it started (0 = none)
    sweep_from = 0
    reported = False
    stats = {"requests": 0, "sweeps": 0}  # noqa: RUF012
    counts: pictures.Counts | None = None
    paths_due = True  # the map changed: the page's list of pictures needs a look


def _reset() -> None:
    S.backpack, S.bank = pictures.Wanted(), pictures.Wanted()
    S.wanted, S.wanted_set = [], frozenset()
    S.present, S.present_count = None, -1
    S.missing = []
    S.asked = {}
    S.sweeping_since = 0.0
    S.paths_due = True


def _option_changed(_option: Any, _value: Any) -> None:
    frametick.soon(TICK_KEY)


bank_option = BoolOption(
    "Bank pictures too",
    False,
    description=(
        "Also keeps the pictures of everything in your bank ready. Each picture takes about 0.5 MB"
        " of video memory, so a bank of 300 items is about 150 MB more. Off: only what you carry"
        " (equipped and backpack)."
    ),
    on_change=_option_changed,
)

fast_option = BoolOption(
    "Draw faster in menus",
    True,
    description=(
        "While a menu is open, the game draws a missing picture every frame instead of 15 a second,"
        " so new loot and the bank fill in quicker. Off: the game's own pace."
    ),
    on_change=_option_changed,
)


def _menu_up(pc: Any) -> bool:
    return bool(pc.bShowMouseCursor)


def _size() -> tuple[int, int]:
    return int(pictures.cvar("TextureSizeX")), int(pictures.cvar("TextureSizeY"))


@frametick.tick(TICK_KEY, every=TICK_S)
def tick() -> None:
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health)
        _tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Instant Item Pictures] {ex!r}")


@frametick.tick(LINK_KEY)
def link_tick() -> None:
    """Once a frame: the BL4 UI Link answers the page (cheap; one owner tick a frame for all our mods)."""
    try:
        page.uilink.tick()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Instant Item Pictures] {ex!r}")


def _tick() -> None:
    if not page.S.registered:
        page.register(GAME_DIR)
    else:
        page.publish()
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None or pc.PlayerState is None:
        return  # title screen or loading
    menu = _menu_up(pc)
    pictures.set_cvar("MaxSweepInterval", pictures.NEVER_S)
    pictures.set_cvar("RenderInterval", 0.0 if menu and fast_option.value else pictures.original("RenderInterval"))

    ps = pc.PlayerState
    changed = pictures.walk(S.backpack, ps, ps.BackpackItems)
    if bank_option.value:
        changed |= pictures.walk(S.bank, ps, ps.BankItems)
    elif S.bank.serials:
        S.bank = pictures.Wanted()
        changed = True
    if changed:
        S.wanted = list(dict.fromkeys(S.backpack.serials + S.bank.serials))
        S.wanted_set = frozenset(S.wanted)
    if not S.sweeping_since:
        pictures.set_cvar("MaxThumbnails", max(pictures.GAME_MAX, len(S.wanted) + pictures.ROOM))

    native = pictures.native_problem()
    if native and not S.reported:
        S.reported = True
        health.report(mod, "warm", f"Pictures aren't drawn ahead of time ({native}); the game keeps them longer still.")
    manager = pictures.manager()
    if manager is None:
        return
    address = manager._get_address()  # noqa: SLF001
    counts = S.counts = pictures.counts(address)
    if counts is None:
        return
    if counts.pictures != S.present_count or changed or S.present is None:
        if counts.pictures < S.present_count:
            pictures.forget_names()  # entries went: their texts' memory may hold other serials now
        S.present = pictures.rendered(address, _size())
        S.present_count = counts.pictures
        if S.present is None:
            return
        S.missing = [serial for serial in S.wanted if serial not in S.present]
        S.paths_due = True
    if S.paths_due:
        paths, left = pictures.paths(PATHS_PER_TICK)
        S.paths_due = left > 0
        page.set_paths(paths)
    now = time.monotonic()
    if _sweep(counts, menu, now):
        return
    # One at a time, only when the game has nothing of its own loading or queued. Keeping several
    # of ours in its lists drew no faster (3.5 a second with 1, 3 or 8: the item's asset load is
    # the limit, measured 2026-10-29), it only delayed the cards' own requests.
    if native or counts.loading or counts.queued:
        return
    if now - S.last_request < (0.0 if menu else PLAY_GAP_S):
        return
    serial = _next_missing(now)
    if serial is not None:
        _ask(address, serial, now)


def _ask(address: int, serial: str, now: float) -> None:
    pictures.request(address, serial)
    S.asked[serial] = (now, S.asked.get(serial, (0.0, 0))[1] + 1)
    S.last_request = now
    S.stats["requests"] += 1


def _next_missing(now: float) -> str | None:
    present = S.present or set()
    for serial in S.missing:
        if serial in present:
            continue
        when, tries = S.asked.get(serial, (0.0, 0))
        if tries and (tries >= MAX_TRIES or now - when < RETRY_S):
            continue
        return serial
    return None


def _sweep(counts: pictures.Counts, menu: bool, now: float) -> bool:
    """
    Our quiet sweep: once other pictures (loot, vendors) pile up, let the game sweep now, with no
    menu up, instead of in the middle of one when the map goes over MaxThumbnails. Its sweep keeps
    nothing finished, so ours are drawn again afterwards (equipped first). True while one runs.
    """
    if S.sweeping_since:
        if counts.pictures < S.sweep_from or now - S.sweeping_since > pictures.SWEEP_WAIT_S:
            S.sweeping_since = 0.0
            S.asked = {}
            pictures.forget_names()
            S.stats["sweeps"] += 1
            pictures.set_cvar("MaxThumbnails", max(pictures.GAME_MAX, len(S.wanted) + pictures.ROOM))
            return False
        return True
    ours = len(S.wanted_set & (S.present or set()))
    if menu or counts.pictures - ours < pictures.SWEEP_AT:
        return False
    # MaxThumbnails just under the count: the game's tick marks a sweep due (a flag that stays set
    # until the sweep) and sweeps once its timer is past MinSweepInterval. Not 0: the first picture
    # drawn after the sweep, before our next tick puts the limit back, set the flag again and a
    # second wipe followed 10 s later (seen 2026-10-29)
    S.sweeping_since, S.sweep_from = now, counts.pictures
    pictures.set_cvar("MaxThumbnails", counts.pictures - 1)
    return True


def status() -> str:
    counts = S.counts
    present = S.present or set()
    ours = len(S.wanted_set & present)
    text = (
        f"{ours}/{len(S.wanted)} of your items have a picture ready"
        f" (backpack {len(S.backpack.serials)}, bank {len(S.bank.serials) if bank_option.value else 'off'})."
    )
    if counts is not None:
        mb = counts.pictures * pictures.TARGET_BYTES / (1 << 20)
        text += (
            f" The game holds {counts.pictures} pictures (~{mb:.0f} MB of video memory), {counts.loading} loading,"
            f" {counts.queued} queued. Asked {S.stats['requests']}, quiet sweeps {S.stats['sweeps']}."
        )
    if pictures.Native.why:
        text += f" Not drawing ahead: {pictures.Native.why}."
    return text


@command("pictures", description="Instant Item Pictures: how many of your items' pictures are ready.")
def pictures_command(_args: argparse.Namespace) -> None:
    pictures.log(status())


def _enable() -> None:
    _reset()


def _disable() -> None:
    try:
        page.unregister()  # the page script goes and lets every picture go
        page.set_paths([])
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Instant Item Pictures] {ex!r}")
    try:
        pictures.restore_cvars()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Instant Item Pictures] {ex!r}")
    _reset()


mod = build_mod(
    options=[bank_option, fast_option],
    hooks=[tick, link_tick],
    commands=[pictures_command],
    on_enable=_enable,
    on_disable=_disable,
)
