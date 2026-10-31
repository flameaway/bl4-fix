"""
Performance: a quick benchmark of what every loaded mod costs the game, built into the Mod Menu
(MODS > Performance) so any player can see which mod slows their game. Until 2026-10-29 this was a
separate mod, BL4 Mod Profiler (bl4_profiler).

Run benchmark (on the page, or `modprofile` in the console) times every hook and shared frame tick
callback of every enabled mod for a few seconds of normal play, other modders' mods included, plus
the CPU of each mod's worker threads, frame times and Python garbage collections. Results show on
the page and go to sdk_mods/settings/bl4_performance_report.txt.

Nothing is measured, and nothing changes, until a run starts: when idle it costs one shared frame
tick callback every TICK_S. A run swaps each hook for a timing wrapper and puts the original back
at the end.

    profiler.py  the measurement     bench.py  the run's phases, notices, the report file
    page.py      the page's state    notice.py the on-screen notices    ui/perf.js|css  the page
"""

from __future__ import annotations

import argparse
import traceback

from mods_base import command
from unrealsdk import logging

from .. import frametick
from . import bench, page, profiler

TICK_KEY = "bl4_mod_menu.perf"
TICK_S = 0.25  # the run's phases and the page: a few times a second is plenty
_errors: set[str] = set()


def _tick() -> None:
    try:
        page.tick()
        bench.tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in _errors and len(_errors) < 20:  # noqa: PLR2004
            _errors.add(repr(ex))
            logging.error("".join(traceback.format_exception(ex)))


def enable() -> None:
    bench.B.skip = TICK_KEY  # our own tick isn't what's measured
    bench.B.on_change = page.changed
    frametick.add(TICK_KEY, _tick, every=TICK_S)
    page.register()


def disable() -> None:
    frametick.remove(TICK_KEY)
    bench.shutdown()
    page.unregister()


@command("modprofile", description="Benchmarks every mod: modprofile [seconds] | menus [seconds] (with the menus up) | stop | report")
def modprofile_command(args: argparse.Namespace) -> None:
    words = [w.lower() for w in args.words]
    verb = words[0] if words else ""
    if verb == "stop":
        bench.cancel()
    elif verb == "report":
        if bench.B.result is None:
            logging.info("[Performance] No results yet.")
        else:
            logging.info(profiler.report_text(bench.B.result))
    else:
        with_menus = verb == "menus"
        number = words[1] if with_menus and len(words) > 1 else verb
        seconds = float(number) if number.replace(".", "", 1).isdigit() else page.S.length
        bench.request(max(3.0, min(seconds, 300.0)), with_menus=with_menus)


modprofile_command.add_argument("words", nargs="*")
