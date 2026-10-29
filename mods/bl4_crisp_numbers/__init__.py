"""
Crisp Damage Numbers for Borderlands 4: sharper damage numbers under DLSS/FSR.

Damage numbers are Niagara sprites (NS_Damage_Numbers, materials M_Display_Damage_Numbers and
M_Display_Damage_Icons) in the "after motion blur" translucency pass. Two things soften them:
- Gearbox's FXAA over that layer (Gbx.FxaaAfterMotionBlur.Enabled 1) smears the dark outline.
  "Light" turns it off: clearly crisper, no measurable GPU cost (bench 2026-10-28).
- The pass is drawn at the render resolution (r.ScreenPercentage, e.g. 50 with DLSS Performance)
  and stretched. "Full" sets r.Translucency.ScreenPercentage.Basis 1: drawn at the output
  resolution and composited after the upscale. The engine has ONE size for all separate
  translucency (the sizing function at 0x30b1c07: clamp(percent/100, 0, 1) [/ primary fraction
  with Basis 1] x SceneTexturesExtent), so every effect is drawn at that size too: +1.3-2 ms GPU in
  a busy scene. (>100 percent with Basis 0 is clamped to 100 there, and is composited before DLSS.)

The catch with Full, measured 2026-10-28: Basis 1 sizes the translucency buffers as the scene buffers'
size / the render fraction, and the ECHO menu drops r.ScreenPercentage to 10 (native code, the
same frame it pauses) to draw the world behind it cheaply. One frame of Basis 1 at 10% asks for
buffers ~10x the screen each way: a D3D12 CreateHeap of seconds (2-6 s freezes on ECHO opening,
~5 fps inside it). Switching Basis back on pause lost the race now and then.

So the order of changes is ours alone:
- Playing: r.ScreenPercentage is written from the console (set-by level 0x0E), which makes the
  game's lower-level writes (the ECHO menu's 10) do nothing. Then Basis 1, never before.
- ECHO menu: Basis 0 first, then we set the 10% ourselves (the menu's GPU saving stays).
- Pause menu / Options / anything else paused: Basis 0 first, then the cvar's set-by level is put
  back (cvars.py), so a DLSS mode change in Options applies as usual.
- Off: the game's values and the set-by level back. Nothing stays behind.
ECHO is told from the pause menu by ECHO Anywhere's menu state check (2 = ECHO). Without that mod,
ECHO keeps the gameplay resolution behind it (safe, a bit more GPU while it's open).

Purely visual and local.
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any

from mods_base import ENGINE, DropdownOption, SliderOption, build_mod, get_pc, hook
import unrealsdk
from unrealsdk import hooks, logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import cvars

__version__ = "0.3.0"
__version_info__ = (0, 3, 0)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
CLEANUP_ID = "bl4_crisp_numbers.cleanup"
CHECK_INTERVAL = 1.0  # s between re-checks of the values while nothing changes
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates): act on the first
# call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012

BASIS = "r.Translucency.ScreenPercentage.Basis"  # 0 render resolution (game), 1 screen resolution
PERCENT = "r.SeparateTranslucencyScreenPercentage"  # of the basis (game: 100)
RENDER = "r.ScreenPercentage"  # the render resolution, the upscaler's input
FXAA = "Gbx.FxaaAfterMotionBlur.Enabled"  # Gearbox's FXAA over the after-motion-blur layer (game: 1)
LIGHT, FULL = "Light (no cost)", "Full (costs GPU)"
MENU_RENDER = 10.0  # what the ECHO menu uses
GAMEPLAY_MIN = 25.0  # below this a value is a menu's, never the gameplay resolution
ECHO_STATE = 2  # ECHO Anywhere's menu state while the ECHO menu is up

PLAY, PAUSED, ECHO = 0, 1, 2


class _S:
    ksl: Any = None
    gps: Any = None
    console: cvars.Console | None = None
    render_cvar: cvars.Cvar | None = None
    broken = False  # the cvar lookup failed: the mod stays out of the way
    menu: Any = None  # ECHO Anywhere's StatusMenu, False when unavailable
    game_thread: int | None = None
    last_call = 0.0
    ticked_at = 0.0
    next_check = 0.0
    mode: int | None = None
    sharp = False  # Basis 1 is on
    pinned_level: int | None = None  # the render cvar's set-by level before we pinned it
    echo_low = False  # we set the ECHO menu's 10%
    render_pct = 100.0  # the gameplay r.ScreenPercentage
    fxaa_cvar: cvars.Cvar | None = None
    fxaa_level: int | None = None  # the FXAA cvar's set-by level before we wrote it
    original: dict[str, float] = {}  # cvar -> the game's value before we changed it


def _ksl() -> Any:
    if _S.ksl is None:
        _S.ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    return _S.ksl


def _world() -> Any:
    viewport = ENGINE.GameViewport
    return viewport.World if viewport is not None else None


def _get(name: str) -> float:
    return float(_ksl().GetConsoleVariableFloatValue(name))


def _set(name: str, value: float) -> None:
    text = str(int(value)) if value == int(value) else f"{value:g}"
    _ksl().ExecuteConsoleCommand(_world(), f"{name} {text}", None)


def _render_cvar() -> cvars.Cvar | None:
    if _S.render_cvar is None and not _S.broken:
        try:
            _S.console = cvars.Console()
            _S.render_cvar = _S.console.cvar(RENDER)
        except cvars.NotFound as ex:
            _S.broken = True
            logging.warning(f"[Crisp Damage Numbers] off: {ex}")
    return _S.render_cvar


def _menu_state(pc: Any) -> int | None:
    """ECHO Anywhere's status menu state (2 = ECHO menu), or None without it."""
    if _S.menu is None:
        _S.menu = False
        try:
            echo = importlib.import_module("bl4_echo_anywhere")
            _S.menu = getattr(echo.State, "menu", None) or echo.native.find(pc)
        except Exception as ex:  # noqa: BLE001 - not installed, or its check failed
            logging.dev_warning(f"[Crisp Damage Numbers] no ECHO menu state: {ex!r}")
    if not _S.menu:
        return None
    return _S.menu.state(pc)


def _mode(world: Any) -> int:
    pc = get_pc(possibly_loading=True)
    if world is None or pc is None:
        return PAUSED
    state = _menu_state(pc)
    if state == ECHO_STATE:
        return ECHO
    if _S.gps is None:
        _S.gps = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    if state or _S.gps.IsGamePaused(world):
        return PAUSED
    return PLAY


def _remember(name: str) -> None:
    if name not in _S.original:
        _S.original[name] = _get(name)


def _sharp_off() -> None:
    """Basis 0 (the game's), whoever set it. Always before the render resolution may drop."""
    if _get(BASIS) != 0:
        _set(BASIS, 0)
    _S.original.pop(BASIS, None)
    if PERCENT in _S.original:
        _set(PERCENT, _S.original.pop(PERCENT))
    _S.sharp = False


def _unpin() -> None:
    cvar = _S.render_cvar
    if _S.echo_low:
        _set(RENDER, _S.render_pct)  # the game puts its own value back only as it closes ECHO
        _S.echo_low = False
    if cvar is not None and _S.pinned_level is not None:
        cvar.set_by = _S.pinned_level
        _S.pinned_level = None


def _play() -> None:
    """Pin the gameplay resolution (if needed fix it first), then Basis 1."""
    cvar = _S.render_cvar
    if cvar is None:
        return
    current = _get(RENDER)
    if _S.pinned_level is None:
        _S.pinned_level = cvar.set_by
    if _S.echo_low or current < GAMEPLAY_MIN or cvar.set_by != cvars.SET_BY_CONSOLE:
        value = current if current >= GAMEPLAY_MIN else _S.render_pct
        _set(RENDER, value)  # a console write: the game's own writes can't change it now
        _S.echo_low = False
        current = value
        if cvar.set_by != cvars.SET_BY_CONSOLE:
            raise cvars.NotFound(f"{RENDER} didn't take the console level")
    _S.render_pct = current
    want = float(percent_option.value)
    if abs(_get(PERCENT) - want) >= 0.01:
        _remember(PERCENT)
        _set(PERCENT, want)
    if _get(BASIS) != 1:
        _set(BASIS, 1)
    _S.sharp = True


def _echo() -> None:
    """Basis 0 first, then the menu's 10% (ours to set while the cvar is pinned)."""
    _sharp_off()
    if _S.pinned_level is not None and not _S.echo_low:
        _set(RENDER, MENU_RENDER)
        _S.echo_low = True


def _paused() -> None:
    """Basis 0 first, then the game may write the resolution again (Options, other menus)."""
    _sharp_off()
    _unpin()


def _fxaa_off() -> None:
    """FXAA off on the after-motion-blur layer: it smears the digits' outline (any resolution)."""
    if _S.fxaa_cvar is None and _S.console is not None:
        _S.fxaa_cvar = _S.console.cvar(FXAA)
    if _get(FXAA) == 0:
        return
    if FXAA not in _S.original:
        _remember(FXAA)
        if _S.fxaa_cvar is not None:
            _S.fxaa_level = _S.fxaa_cvar.set_by
    _set(FXAA, 0)


def _fxaa_restore() -> None:
    if FXAA in _S.original:
        _set(FXAA, _S.original.pop(FXAA))
    if _S.fxaa_cvar is not None and _S.fxaa_level is not None:
        _S.fxaa_cvar.set_by = _S.fxaa_level
        _S.fxaa_level = None


def _full_off() -> None:
    if _S.sharp or _S.pinned_level is not None or _S.echo_low or PERCENT in _S.original:
        _sharp_off()
        _unpin()


def _restore() -> None:
    _sharp_off()
    _unpin()
    _fxaa_restore()
    _S.mode = None


def _option_changed(_opt: Any, _value: Any) -> None:
    _S.next_check = 0.0  # the tick applies it on the game thread
    _S.mode = None


sharpness_option = DropdownOption(
    "Sharpness",
    LIGHT,
    [LIGHT, FULL],
    description=(
        "Light: turns off the anti-aliasing the game runs over damage numbers, which smears their"
        " outline. Clearly crisper, no measurable cost. Full: also draws damage numbers at your"
        " screen's resolution instead of DLSS/FSR's lower one, the sharpest. But the game draws all"
        " its effects (fire, smoke, sparks) at that same resolution, which cost 1.3-2 ms per frame"
        " in a busy scene (measured, DLSS Performance): pick it only if you have GPU to spare."
    ),
    on_change_while_enabled=_option_changed,
)


percent_option = SliderOption(
    "Resolution (% of screen)",
    100,
    50,
    100,
    5,
    description=(
        "Full sharpness only: the resolution damage numbers are drawn at. 100 = pixel for pixel."
        " Every effect is drawn at this resolution too: lower it if your frame rate drops. With"
        " DLSS or FSR off (100% render resolution) it changes nothing."
    ),
    on_change_while_enabled=_option_changed,
)


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    started = time.perf_counter()
    gap, _S.last_call = started - _S.last_call, started
    if gap < FRAME_GAP_S and started - _S.ticked_at < MAX_WAIT_S:
        return
    _S.ticked_at = started
    if _S.broken:
        return
    try:
        _S.game_thread = threading.get_ident()
        if sharpness_option.value != FULL:
            if started < _S.next_check:
                return
            _S.next_check = started + CHECK_INTERVAL
            if _world() is None or _render_cvar() is None:
                return
            _full_off()
            _fxaa_off()
            return
        world = _world()
        if world is None or _render_cvar() is None:
            return
        mode = _mode(world)
        if mode == _S.mode and started < _S.next_check:
            return
        _S.mode = mode
        _S.next_check = started + CHECK_INTERVAL
        _fxaa_off()
        if mode == PLAY:
            _play()
        elif mode == ECHO:
            _echo()
        else:
            _paused()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Crisp Damage Numbers] {ex!r}")
        try:
            _restore()
        finally:
            _S.broken = True


def _enable() -> None:
    _S.next_check = 0.0
    _S.mode = None
    _S.broken = False


def _disable() -> None:
    """Puts the game's values back on the game thread: now if we're on it, else next tick."""
    if not (_S.sharp or _S.original or _S.pinned_level is not None or _S.echo_low or _S.fxaa_level is not None):
        return
    if threading.get_ident() == _S.game_thread:
        _restore()
        return

    def once(*_a: Any) -> None:
        try:
            hooks.remove_hook(TICK_FUNC, Type.POST, CLEANUP_ID)
            _restore()
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Crisp Damage Numbers] {ex!r}")

    hooks.add_hook(TICK_FUNC, Type.POST, CLEANUP_ID, once)


mod = build_mod(
    options=[sharpness_option, percent_option],
    hooks=[tick],
    on_enable=_enable,
    on_disable=_disable,
)
