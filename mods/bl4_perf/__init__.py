"""
Performance Fixes for Borderlands 4: removes hitches we traced with an external stack sampler
(dev/perf/, findings in docs/game-performance.md).

- Faster Python hooks: one persistent Python thread state on the game thread instead of a new one
  per hook call, and a 0.5 ms GIL switch interval (python_state.py). Helps every SDK mod.
- Frame generation stays on in menus (off): no frame generation rebuild (a freeze with the RTX 30
  DlssgSm86 enabler) every time a menu closes (render.py).
- Keep DLSS loaded (experimental, off): keeps the DLSS upscaler's NVIDIA feature alive across menus
  instead of rebuilding it (render.py). Does not stop frame generation rebuilds.
- Keep game memory in RAM: a soft minimum working set so Windows trims other programs first
  (memory.py). Off by default; for PCs that run short of RAM.
- Python garbage collection without hitches: long-lived Python objects frozen out of the cycle
  collector after each world load (gc_tune.py), so a full collection no longer walks every mod's
  data inside a hook (35-209 ms measured).

Everything is put back when the mod is turned off. Local only: nothing here touches co-op.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from mods_base import ENGINE, BoolOption, SliderOption, build_mod, hook
from unrealsdk import hooks, logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import gc_tune, memory, python_state, render

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
CLEANUP_ID = "bl4_perf.cleanup"
CHECK_INTERVAL = 5.0  # s between cvar checks


class _S:
    game_thread: int | None = None
    next_check = 0.0


def _log(msg: str) -> None:
    logging.info(f"[Performance Fixes] {msg}")


def _python_changed(_opt: BoolOption, value: bool) -> None:
    if not value:
        _cleanup(cvars=False)  # unpins on the game thread


python_option = BoolOption(
    "Faster Python hooks",
    True,
    description=(
        "Keeps one Python thread state on the game thread instead of building and tearing one"
        " down for every mod hook call (~1-2 ms per frame with many mods), and lets the game"
        " thread get Python's lock back sooner from background threads."
    ),
    on_change_while_enabled=_python_changed,
)


def _gc_changed(_opt: BoolOption, value: bool) -> None:
    _S.next_check = 0.0
    if not value and gc_tune.restore():
        _log("Python objects unfrozen")


gc_option = BoolOption(
    "Python garbage collection without hitches",
    True,
    description=(
        "Python's garbage collector now and then walks every mod's data in one go, inside a game"
        " frame (35-200 ms freezes). This sets the long-lived data aside after each world load so"
        " it only walks what's new. Changes nothing you can see."
    ),
    on_change_while_enabled=_gc_changed,
)


def _dlss_changed(_opt: BoolOption, value: bool) -> None:
    _S.next_check = 0.0
    if not value:
        _cleanup(python=False, cvars=list(render.DLSS))


def _framegen_changed(_opt: BoolOption, value: bool) -> None:
    _S.next_check = 0.0
    if not value:
        _cleanup(python=False, cvars=list(render.FRAMEGEN))


dlss_option = BoolOption(
    "Keep DLSS loaded (experimental)",
    False,
    description=(
        "Keeps the DLSS upscaler loaded when a menu hides the game instead of rebuilding it"
        " afterwards. Experimental: it did not stop the longer freezes from frame generation"
        " rebuilding itself. No effect without DLSS."
    ),
    on_change_while_enabled=_dlss_changed,
)


framegen_option = BoolOption(
    "Frame generation stays on in menus",
    False,
    description=(
        "The game switches frame generation off in menus and cinematics, and switching it back on"
        " rebuilds it: with an RTX 30 frame generation mod that freezes the game for up to half a"
        " second every time you close a menu. This keeps it on instead. Menus and cutscenes may"
        " show small frame generation artifacts. No effect with frame generation off."
    ),
    on_change_while_enabled=_framegen_changed,
)


def _memory_changed(_opt: SliderOption, value: float) -> None:
    try:
        _log(memory.apply(float(value)))
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Performance Fixes] {ex!r}")


memory_option = SliderOption(
    "Keep game memory in RAM (GB)",
    0,
    0,
    24,
    1,
    description=(
        "When the PC runs low on free memory, Windows moves parts of the game out of RAM and the"
        " game freezes briefly each time it needs them back. This asks Windows to keep at least"
        " this much of the game in RAM and take memory from other programs first. 0 = off."
        " Leave it at 0 unless you see stutter with little free RAM; never set it near your total"
        " RAM."
    ),
    on_change_while_enabled=_memory_changed,
)


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    # fires several times a frame: the work below is gated, the rest is two compares
    try:
        if _S.game_thread is None:
            _S.game_thread = threading.get_ident()
        if python_option.value and not python_state.is_pinned():
            if python_state.pin_here():
                _log("game-thread Python state pinned")
        now = time.monotonic()
        if now < _S.next_check:
            return
        _S.next_check = now + CHECK_INTERVAL
        if gc_option.value:
            viewport = ENGINE.GameViewport
            world = viewport.World if viewport is not None else None
            if done := gc_tune.step(world._get_address() if world is not None else None):  # noqa: SLF001
                _log(f"Python objects frozen ({done})")
        if dlss_option.value:
            render.enforce(render.DLSS)
        if framegen_option.value:
            render.enforce(render.FRAMEGEN)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Performance Fixes] {ex!r}")


def _cleanup(*, python: bool = True, cvars: bool | list[str] = True) -> None:
    """Undoes our changes (all cvars, or the listed ones) on the game thread: now if we're on it,
    else on its next tick."""

    def run() -> None:
        if cvars:
            render.restore(None if cvars is True else cvars)
        if python and python_state.unpin_here():
            _log("game-thread Python state released")

    if threading.get_ident() == _S.game_thread or _S.game_thread is None:
        run()
        return

    def once(*_a: Any) -> None:
        try:
            hooks.remove_hook(TICK_FUNC, Type.POST, CLEANUP_ID)
            run()
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Performance Fixes] {ex!r}")

    hooks.add_hook(TICK_FUNC, Type.POST, CLEANUP_ID, once)


def _enable() -> None:
    _S.next_check = 0.0
    if memory_option.value:
        _memory_changed(memory_option, memory_option.value)


def _disable() -> None:
    try:
        _log(memory.restore())
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Performance Fixes] {ex!r}")
    if gc_tune.restore():
        _log("Python objects unfrozen")
    _cleanup()


mod = build_mod(
    options=[python_option, gc_option, framegen_option, dlss_option, memory_option],
    hooks=[tick],
    on_enable=_enable,
    on_disable=_disable,
)
