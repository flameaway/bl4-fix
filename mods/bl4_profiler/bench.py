"""
One benchmark run, from the request to the saved report (game thread, from the mod's tick).

Run from the page (or the option): it waits for the menus to close, lets the game settle for
SETTLE_S (closing a menu can rebuild frame generation, a hitch that isn't any mod's), measures for
the chosen length, then puts every hook back and keeps the results for the page, the console and
a text report in the settings folder. A map change during the run ends it early (a loading screen
isn't play); the command can also run it with the menus up (to measure a mod's menu page).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from mods_base import ENGINE, SETTINGS_DIR, get_pc
from unrealsdk import hooks, logging
from unrealsdk.unreal import WeakPointer

from . import profiler
from .notice import Notice

REPORT = SETTINGS_DIR / "bl4_profiler_report.txt"
SETTLE_S = 2.0
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
CLEANUP_ID = "bl4_profiler.notice_cleanup"
# Shorter than this after a map change: nothing worth showing
MIN_S = 3.0


class B:
    # idle | waiting (for the menus to close) | settling | running
    phase = "idle"
    length = 20.0
    menus_ok = False  # measure with the menus up (the command)
    phase_at = 0.0
    pc: WeakPointer | None = None
    result: dict[str, Any] | None = None
    message = ""
    skip = ""  # our own mod's name
    on_change: Any = None


def _changed() -> None:
    if B.on_change is not None:
        B.on_change()


def say(text: str) -> None:
    B.message = text
    logging.info(f"[Mod Profiler] {text}")
    _changed()


def request(length: float, *, with_menus: bool = False) -> None:
    """Starts a run as soon as the menus are down (now, with with_menus)."""
    if B.phase == "running":
        return
    B.length = float(length)
    B.menus_ok = with_menus
    B.phase = "settling" if with_menus else "waiting"
    B.phase_at = time.monotonic()
    say(f"Benchmark of {B.length:.0f} s starts when the menus close. Play as you normally would." if not with_menus else f"Measuring {B.length:.0f} s.")


def cancel() -> None:
    if B.phase == "running":
        finish("Stopped early.")
    elif B.phase != "idle":
        B.phase = "idle"
        say("Benchmark cancelled.")


def left() -> float:
    if B.phase == "running":
        return max(0.0, B.length - profiler.elapsed())
    return B.length


def _menus_up(pc: Any) -> bool:
    return pc is None or bool(pc.bShowMouseCursor) or pc.Pawn is None


def _world() -> str:
    try:
        return str(ENGINE.GameViewport.World.Name)
    except Exception:  # noqa: BLE001
        return ""


def tick() -> None:
    Notice.tick()  # hides a shown notice when its time is up (a clock compare otherwise)
    if B.phase == "idle":
        return
    now = time.monotonic()
    pc = get_pc(possibly_loading=True)
    if B.phase == "waiting":
        if not _menus_up(pc):
            B.phase = "settling"
            B.phase_at = now
            _changed()
            # Gone again before measuring starts, so it isn't part of what's measured
            Notice.show(pc, "Benchmark starting", [f"Measuring {B.length:.0f} s of play: carry on as normal.",
                                                   "A notice here says when it's done."], hold=SETTLE_S - 0.25)  # fmt: skip
    elif B.phase == "settling":
        if not B.menus_ok and _menus_up(pc):
            B.phase = "waiting"  # opened again: wait some more
            _changed()
        elif now - B.phase_at >= SETTLE_S:
            B.pc = WeakPointer(pc) if pc is not None else None
            B.phase = "running"
            say(profiler.start(B.length, B.skip, _world()))
    elif B.phase == "running":
        if B.pc is not None and B.pc() is not pc:
            finish("The map changed: the run ended early.")
        elif profiler.elapsed() >= B.length:
            finish("")


def finish(note: str) -> None:
    span = profiler.elapsed()
    res = profiler.stop()
    B.phase = "idle"
    B.pc = None
    if res is None or span < MIN_S:
        say(note or "No results.")
        _notice("Benchmark stopped", [note or "Too short to show anything."], error=True)
        return
    if note:
        res["notes"].insert(0, note)
    B.result = res
    text = profiler.report_text(res)
    threading.Thread(target=_write, args=(text,), name="bl4_profiler report", daemon=True).start()
    top = res["mods"][0] if res["mods"] else None
    say(
        f"Done: mods cost {res['mods_ms_frame']:.2f} ms per frame"
        + (f", most {top['name']} ({top['ms_frame']:.2f} ms)" if top and top["ms_frame"] >= 0.01 else "")
        + ". Results on the Mod Profiler page.",
    )
    details = [f"All mods: {res['mods_ms_frame']:.2f} ms per frame ({res['share'] * 100:.0f}% of a frame)"]
    if top is not None and top["ms_frame"] >= 0.01:
        details.append(f"Most: {top['name']}, {top['ms_frame']:.2f} ms")
    details.append("Results: MODS > BL4 Mod Profiler" + (f" ({note})" if note else ""))
    _notice("Benchmark done", details)


def _notice(title: str, details: list[str], *, error: bool = False) -> None:
    """Over the game, unless a menu is up (the page shows the results there)."""
    pc = get_pc(possibly_loading=True)
    if pc is not None and (B.menus_ok or not _menus_up(pc)):
        Notice.show(pc, title, details, error=error)


def _write(text: str) -> None:
    try:
        tmp = REPORT.with_name(REPORT.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(REPORT)
    except OSError as ex:
        logging.warning(f"[Mod Profiler] couldn't write {REPORT}: {ex}")


def open_report() -> bool:
    """Opens the text report in the player's text editor (on a worker: ShellExecute can be slow)."""
    if not Path(REPORT).is_file():
        return False

    def run() -> None:
        try:
            os.startfile(REPORT)  # noqa: S606 - our own text file
        except OSError as ex:
            logging.warning(f"[Mod Profiler] couldn't open the report: {ex}")

    threading.Thread(target=run, name="bl4_profiler open", daemon=True).start()
    return True


def shutdown() -> None:
    """The mod is going off (maybe not on the game thread): put every hook back now."""
    profiler.abort()
    B.phase = "idle"

    # The notice is UMG: removed on the game thread, at its next tick (if there is one)
    def once(*_: object) -> None:
        try:
            hooks.remove_hook(TICK_FUNC, hooks.Type.POST, CLEANUP_ID)
            Notice.remove()
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Mod Profiler] {ex!r}")

    hooks.add_hook(TICK_FUNC, hooks.Type.POST, CLEANUP_ID, once)
    B.pc = None
