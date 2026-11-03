"""
Pictures made at the title screen (option "Pictures at the title screen", on by default: the user's
choice, 2026-11-02): every boss, legendary and pearl in the dex without a picture yet, beaten /
obtained or not. Bosses: each is rebuilt from its booth recipe (booth.jsonl: plain meshes on our
own actor, no boss spawned) high above the title scene's camera and photographed (bake.py's booth
mode). Items: each is rolled and drawn by the game's own inventory thumbnail renderer (thumbs.py;
nothing spawned). Both at once. The pictures' Python work (compose, PNGs, small copies) runs on
interpreters of their own (offload.py, ~0.22 s a boss), so bosses are bound by their booths'
settle time (BOOTH_AHEAD booths settling at once: ~0.3 s a boss) and items by the renderer
(PRERENDER passes, one a frame: ~0.2 s each with the bosses beside them): items set the pace.
Everything missing (118 bosses, 318 items: a first launch) took 65 s, bosses done at 35 s (85 s
before offload.py, when compose shared the game's GIL; 2026-11-03). A fresh launch is about as
fast: the renderer's passes are counted in frames, and cold booth loads add ~2 s to the bosses.
Faster item passes were measured and rejected: on a fresh launch they catch textures half loaded
(see thumbs.py). Nothing shows on screen but a notice: while it runs, how many of each are made, the
time left (from the pace so far) and a bar, updated in place (PROGRESS_S); then a short one at the
end. The dex keeps hiding unbeaten bosses and items not had yet as before.

Why the title screen: a boss picture costs ~0.5-0.9 s of game thread over ~2 s (the booth's
blocking asset loads, then 5 GPU readbacks of 65-110 ms each; measured in gameplay, 2026-11-02;
the read backs are ~20 ms since readback.py), a visible stutter in play. At the title screen
nobody is playing.

Nothing ships pictures (2026-11-03, the user's choice: no file made from the game's ships with the
mod, and nothing is downloaded): every install makes them here, all at its first visit to the
title screen, then only those a dex update adds (or a wiped folder). It waits for the small-copy
thread (screen.SMALL_THREAD) so the two never write the same folder at once.

"At the title screen" = a player controller, no character, the cursor showing, all held for
TITLE_HOLD_S in the same world (the title scene is World_P, the same map as play, and neither the
game mode nor the game instance says which: a death or a loading screen shows no cursor, and leaving
the title screen changes the world, which stops both bakes). Once a session: after a full run (or
nothing to do) it's done until the next launch; a run cut short by Continue picks up at the next
visit to the title screen. A boss a run stopped on twice (the game crashed or was closed) is
skipped from then on (bake.recover / crashed.json). A damaged picture file counts as missing
(picture.whole_pngs), so it's made again.

Dev options (__init__.py): the counts of pictures there against the dex's (`S.counts`, counted on
a worker by `recount` and by every run's read), and `rerun`: the run again (now at the title
screen, else at the next visit), with the option off too, retrying bosses skipped after a crash.

Game thread: tick() (from the mod's tick, every 0.25 s while nothing runs: a few attribute reads).
The folder scans and the recipe read run on a worker thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

from . import bake, booth, data, screen, serialgen, thumbs
from .notice import Notice

TITLE_HOLD_S = 3.0  # the title screen held this long before it starts (the scene settles first)
THREAD = "weapondex-titlebake"
COUNT_THREAD = "weapondex-count"
PROGRESS_S = 0.5  # the progress notice is updated this often
MEASURED_AFTER = 3  # pictures of a kind made before its own pace is used for the time left
RECOUNT_S = 5.0  # after a run: the last boss pictures are still being composed when it ends
BOSS_S, ITEM_S = 0.8, 0.2  # per picture, for the notice's estimate (both bakes at once; see above)


class S:
    phase = "idle"  # idle | reading | ready | running | done
    running: set[str] = set()  # "bosses" / "items" still going
    cut = ""  # what was cut short (Continue, a map change), if anything
    title_since = 0.0  # when the title screen was first seen (0: not now)
    world = WeakPointer()  # the world it was seen in
    read: dict[str, Any] | None = None  # the worker's result
    made = [0, 0]  # bosses, items made this run
    failed = 0
    forced = False  # the dev re-run: runs with the option off, once more this session, crashed bosses again
    # {"bosses": (have, of), "items": (have, of), "skipped": bosses skipped after a crash}; None: not counted yet
    counts: dict[str, Any] | None = None
    counts_version = 0  # bumped whenever counts change (the options poll it)
    recount_at = 0.0
    totals = [0, 0]  # bosses, items asked for this run
    started_at = 0.0
    progress_at = 0.0  # the progress notice's next update


def log(msg: str) -> None:
    logging.info(f"[Kairosdex] {msg}")


def _at_title() -> bool:
    """At the title screen, held for TITLE_HOLD_S in the same world."""
    pc = get_pc(possibly_loading=False)
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    if pc is None or world is None or pc.Pawn is not None or not pc.bShowMouseCursor:
        S.title_since = 0.0
        return False
    now = time.monotonic()
    if not S.title_since or S.world() != world:
        S.world = WeakPointer(world)
        S.title_since = now
    return now - S.title_since >= TITLE_HOLD_S


def tick(game_dir: Path, enabled: bool) -> None:
    """From the mod's tick (game thread)."""
    if Notice.hide_at:
        Notice.tick()
    if S.recount_at and time.monotonic() >= S.recount_at:
        S.recount_at = 0.0
        recount(game_dir)
    enabled = enabled or S.forced
    if not enabled:
        # Ours only: the dev bakes never set these phases
        if S.phase == "running":
            if "bosses" in S.running and bake.running():
                bake.stop("stopped (option off)")
            if "items" in S.running and thumbs.running():
                thumbs.stop("stopped (option off)")
        S.phase = "idle"
        S.title_since = 0.0
        return
    if S.phase == "running" and time.monotonic() >= S.progress_at:
        _show_progress()
    if S.phase == "idle":
        if not _at_title():
            return
        if bake.running() or thumbs.running() or any(t.name in (screen.SMALL_THREAD, THREAD) for t in threading.enumerate()):
            return  # a dev bake, or the small copies: after them
        S.phase = "reading"
        S.read = None
        dex = data.dex()
        bosses = [b["id"] for b in dex["bosses"]]
        threading.Thread(target=_read, args=(game_dir, bosses, list(dex["items"])), name=THREAD, daemon=True).start()
    elif S.phase == "reading":
        if S.read is not None:
            S.phase = "ready"
    elif S.phase == "ready":
        _start(game_dir)


def _read(game_dir: Path, bosses: list[str], items: list[str]) -> None:
    """Worker: the bosses and items without a picture, and the bosses' recipes (and the counts)."""
    try:
        bake.recover(game_dir)  # the last run stopped on a boss: tried once more, or skipped from now on
        if S.forced:
            bake.forgive(game_dir)  # the dev re-run tries the skipped ones too
        pictured = set(bake.baked(game_dir))
        skipped = set(bake.crashed(game_dir))
        missing = [i for i in bosses if i not in pictured | skipped]
        recipes, why = booth.read_recipes(missing) if missing else ({}, "")
        have_items = set(thumbs.baked(game_dir))
        _tally(bosses, items, pictured, skipped, have_items)
        S.read = {"recipes": recipes, "why": why, "items": [k for k in items if k not in have_items and serialgen.has(k)]}
    except Exception as ex:  # noqa: BLE001 - no pictures is fine, a broken tick isn't
        S.read = {"recipes": {}, "why": f"couldn't look for missing pictures: {ex!r}", "items": []}


def _start(game_dir: Path) -> None:
    read = S.read or {}
    recipes: dict[str, Any] = read.get("recipes") or {}
    items: list[str] = read.get("items") or []
    if read.get("why"):
        log(f"No boss pictures made at the title screen: {read['why']}")
    if not recipes and not items:
        S.phase = "done"
        if S.forced:
            S.forced = False
            counts = S.counts or {}
            lacking = sum(of - have for have, of in (counts.get("bosses", (0, 0)), counts.get("items", (0, 0))))
            _show("Kairosdex: every picture is there" if not lacking else f"Kairosdex: {lacking} picture{'s' if lacking != 1 else ''} can't be made",
                  [read["why"]] if lacking and read.get("why") else None)  # fmt: skip
        return
    if not _at_title():
        S.phase = "idle"  # left meanwhile: at the next visit
        return
    S.made, S.failed, S.cut, S.running = [0, 0], 0, "", set()
    bosses = [b["id"] for b in data.dex()["bosses"] if b["id"] in recipes]
    parts = []
    if bosses:
        parts.append(f"{len(bosses)} boss{'es' if len(bosses) != 1 else ''}")
    if items:
        parts.append(f"{len(items)} item{'s' if len(items) != 1 else ''}")
    if bosses:
        msg = bake.start(game_dir, bosses, recipes=recipes, on_finish=lambda summary: _finished("bosses", summary, len(bosses)))
        if bake.running():
            S.running.add("bosses")
        else:
            log(f"Boss pictures at the title screen didn't start: {msg}")
    if items:
        msg = thumbs.start(game_dir, items, on_finish=lambda summary: _finished("items", summary, len(items)))
        if thumbs.running():
            S.running.add("items")
        else:
            log(f"Item pictures at the title screen didn't start: {msg}")
    if not S.running:
        S.phase = "done"
        return
    S.phase = "running"
    log(f"Making pictures at the title screen: {' and '.join(parts)}")
    S.totals = [len(bosses) if "bosses" in S.running else 0, len(items) if "items" in S.running else 0]
    S.started_at = time.monotonic()
    _show_progress()


def _show_progress() -> None:
    """The held notice: how many of each are made, the time left and a bar (game thread)."""
    now = time.monotonic()
    S.progress_at = now + PROGRESS_S
    elapsed = now - S.started_at
    left = 0.0
    counts = []
    bakes = (("bosses", bake.S.done, bake.S.failed, BOSS_S), ("items", thumbs.S.done, thumbs.S.failed, ITEM_S))
    for i, (kind, done, failed, per) in enumerate(bakes):
        total = S.totals[i]
        if not total:
            continue
        if kind in S.running:
            made, handled = len(done), len(done) + len(failed)
        else:
            made, handled = S.made[i], total  # that bake is over
        counts.append(f"{kind.capitalize() if not counts else kind} {min(made, total)} of {total}")
        # Both bakes run at once: the run lasts as long as the slower one
        pace = elapsed / handled if handled >= MEASURED_AFTER else per
        left = max(left, max(0, total - handled) * pace)
    progress = elapsed / (elapsed + left) if elapsed + left > 0 else 0.0
    if "bosses" in S.running and bake.waiting():
        eta = "waiting for the game to build its shaders"  # bake.DRAW_RETRY_S: booths not drawn yet
    else:
        eta = "almost done" if left < 5 else f"about {_eta(round(left / 5) * 5)} left"  # in 5 s steps: no flicker
    _show("Kairosdex: making pictures", [", ".join(counts), eta], hold=True, progress=progress)


def _finished(kind: str, summary: str, total: int) -> None:
    """A bake's on_finish: when both are done, the run is."""
    made = len(bake.S.done) if kind == "bosses" else len(thumbs.S.done)
    S.made[0 if kind == "bosses" else 1] += made
    S.failed += len(bake.S.failed) if kind == "bosses" else len(thumbs.S.failed)
    if made < total and "stopped" in summary:
        S.cut = S.cut or f"{made} of {total} {'boss' if kind == 'bosses' else 'item'} pictures"
    S.running.discard(kind)
    if S.running:
        return
    if S.cut:
        # Continue, a map change, the option: the rest at the next visit to the title screen (no
        # notice: the player is loading in)
        Notice.hide()
        S.phase = "idle"
        S.title_since = 0.0
        S.recount_at = time.monotonic() + RECOUNT_S
        log(f"Pictures at the title screen cut short ({S.cut}); the rest at the next title screen")
        return
    _done()


def _done() -> None:
    S.phase = "done"
    S.forced = False
    S.recount_at = time.monotonic() + RECOUNT_S
    bosses, items = S.made
    parts = []
    if bosses:
        parts.append(f"{bosses} boss")
    if items:
        parts.append(f"{items} item")
    title = f"Kairosdex: {' and '.join(parts)} picture{'s' if bosses + items != 1 else ''} made" if parts else "Kairosdex"
    if parts or S.failed:
        _show(title, [f"{S.failed} couldn't be made"] if S.failed else None)


def _eta(seconds: float) -> str:
    seconds = max(5, round(seconds))
    return f"{seconds} seconds" if seconds < 90 else f"{round(seconds / 60)} minutes"


def _show(title: str, details: list[str] | None = None, **how: Any) -> None:
    pc = get_pc(possibly_loading=False)
    if pc is None:
        return
    try:
        Notice.show(pc, title, details, **how)
    except Exception as ex:  # noqa: BLE001 - a notice is a nicety
        logging.error(f"[Kairosdex] notice: {ex!r}")


def rerun() -> str:
    """
    The dev button: the title-screen run again, with the option on or off, retrying bosses
    skipped after a crash. Starts within a few seconds at the title screen, else at the next visit.
    """
    if S.phase in ("reading", "ready", "running"):
        return "Kairosdex is already making pictures."
    S.forced = True
    S.phase, S.title_since = "idle", 0.0
    pc = get_pc(possibly_loading=True)
    if pc is not None and pc.Pawn is None:
        return "Making the missing pictures in a few seconds."
    return "The missing pictures will be made the next time you're at the title screen."


def status() -> str:
    """The run's state in a few words, for the dev button."""
    if S.phase in ("reading", "ready", "running"):
        return "making them"
    if not S.forced:
        return ""
    pc = get_pc(possibly_loading=True)
    return "starting" if pc is not None and pc.Pawn is None else "next title screen"


def recount(game_dir: Path) -> None:
    """Counts the pictures again on a worker thread (S.counts: the dev options show them)."""
    if any(t.name == COUNT_THREAD for t in threading.enumerate()):
        return
    dex = data.dex()
    bosses = [b["id"] for b in dex["bosses"]]
    items = list(dex["items"])

    def work() -> None:
        try:
            _tally(bosses, items, set(bake.baked(game_dir)), set(bake.crashed(game_dir)), set(thumbs.baked(game_dir)))
        except Exception as ex:  # noqa: BLE001 - only the counts
            logging.error(f"[Kairosdex] counting pictures: {ex!r}")

    threading.Thread(target=work, name=COUNT_THREAD, daemon=True).start()


def _tally(bosses: list[str], items: list[str], pictured: set[str], skipped: set[str], have_items: set[str]) -> None:
    """Only the dex's own bosses and items count (the folder may hold others)."""
    counts = {
        "bosses": (sum(1 for i in bosses if i in pictured), len(bosses)),
        "items": (sum(1 for k in items if k in have_items), len(items)),
        "skipped": sum(1 for i in bosses if i in skipped and i not in pictured),
    }
    if counts != S.counts:
        S.counts = counts
        S.counts_version += 1


def forget() -> None:
    """The mod is disabled (its tick stops; the mod's _disable stops a running bake first)."""
    S.phase = "idle"
    S.forced = False
    S.recount_at = 0.0
    S.running = set()
    S.title_since = 0.0
    S.read = None
    Notice.remove()
