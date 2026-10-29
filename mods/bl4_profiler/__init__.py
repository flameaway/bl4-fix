"""
Mod Profiler for Borderlands 4: a quick benchmark of what every loaded mod costs the game.

Run benchmark (on its Mod Menu page, as an option, or `modprofile` in the console) times every
hook of every enabled mod for a few seconds of normal play, other modders' mods included, plus the
CPU of each mod's worker threads, frame times and Python garbage collections. The results show on
the page (one row per mod, hover for its hooks) and go to sdk_mods/settings/bl4_profiler_report.txt.

Nothing is measured, and nothing changes, until a run starts: when idle the mod costs one clock
compare per camera hook call. A run swaps each hook for a timing wrapper and puts the original
back at the end.

Everything but this file can be hot-reloaded (dev/reload_profiler.py); the page's script and
styles reload themselves in the page.
"""

from __future__ import annotations

import argparse
import time
import traceback
from typing import Any

from mods_base import ButtonOption, SliderOption, build_mod, command, hook
from unrealsdk import logging
from unrealsdk.hooks import Type

from . import bench, page, profiler

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The run's phases and the page registration: a few times a second is plenty
CHECK_S = 0.25

length_option = SliderOption(
    "Benchmark length (s)",
    20,
    10,
    60,
    step=5,
    description="How long a benchmark measures. Longer runs average out more, and catch rarer hitches.",
)


def _run_pressed(_: ButtonOption) -> None:
    bench.request(length_option.value)


run_option = ButtonOption(
    "Run benchmark",
    description=(
        "Measures every enabled mod for a few seconds once you close the menus. Play normally (a fight"
        " or the open world shows the most). Results on this page and in sdk_mods/settings/bl4_profiler_report.txt."
    ),
    on_press=_run_pressed,
)

page.S.length = lambda: float(length_option.value)


class S:
    checked_at = 0.0
    errors: set[str] = set()  # noqa: RUF012


@hook(TICK_FUNC, Type.POST)
def _tick(*_: Any) -> None:
    try:
        now = time.monotonic()
        if now - S.checked_at < CHECK_S:
            return
        S.checked_at = now
        page.tick()
        bench.tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in S.errors:
            S.errors.add(repr(ex))
            logging.error("".join(traceback.format_exception(ex)))


@command("modprofile", description="Benchmarks every mod: modprofile [seconds] | menus [seconds] (with the menus up) | stop | report")
def modprofile_command(args: argparse.Namespace) -> None:
    words = [w.lower() for w in args.words]
    verb = words[0] if words else ""
    if verb == "stop":
        bench.cancel()
    elif verb == "report":
        if bench.B.result is None:
            logging.info("[Mod Profiler] No results yet.")
        else:
            logging.info(profiler.report_text(bench.B.result))
    else:
        with_menus = verb == "menus"
        number = words[1] if with_menus and len(words) > 1 else verb
        seconds = float(number) if number.replace(".", "", 1).isdigit() else length_option.value
        bench.request(max(3.0, min(seconds, 300.0)), with_menus=with_menus)


modprofile_command.add_argument("words", nargs="*")


def _enable() -> None:
    bench.B.skip = mod.name  # here, not at import: a hot reload of bench would forget it
    bench.B.on_change = page.changed
    page.S.registered = False
    page.S.register_tried = 0.0


def _disable() -> None:
    bench.shutdown()
    page.unregister()


mod = build_mod(
    options=[run_option, length_option],
    hooks=[_tick],
    commands=[modprofile_command],
    on_enable=_enable,
    on_disable=_disable,
)
