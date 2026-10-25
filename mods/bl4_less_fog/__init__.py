"""
Less Fog for Borderlands 4: thin out the blue haze over the world without switching fog off.

The game's fog is driven by each map's time-of-day cycle. Every frame we take the numbers it
picked and scale them (see fog.py): lower density, and fog that starts further away. Fog stays on,
so there's no black void where the world ends (as with r.Fog=0), just less of it. Changes show at
once, no restart. Turning the mod off puts the game's fog back.

Purely visual and local: each player picks their own.
"""

from __future__ import annotations

import time
from typing import Any

from mods_base import SliderOption, SpinnerOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import fog

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

# Runs every frame after the time-of-day actor has ticked, before the frame is drawn
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates): act once a frame
SAME_FRAME_S = 0.003
_ticked_at = 0.0

CUSTOM = "Custom"
# name: (density %, start distance x)
PRESETS = {
    "Vanilla": (100, 1.0),
    "Light": (75, 1.5),
    "Balanced": (50, 2.0),
    "Clear": (25, 3.0),
    "Very clear": (10, 4.0),
}
DEFAULT_PRESET = "Balanced"

_syncing = False


def _match_preset(density: float, start: float) -> str:
    for name, (d, s) in PRESETS.items():
        if d == round(density) and s == start:
            return name
    return CUSTOM


def _preset_changed(_option: SpinnerOption, value: str) -> None:
    global _syncing
    if value not in PRESETS or _syncing:
        return
    _syncing = True
    try:
        density_option.value, start_option.value = PRESETS[value]
    finally:
        _syncing = False


def _density_changed(_option: SliderOption, value: float) -> None:
    if not _syncing:
        preset_option.value = _match_preset(value, start_option.value)


def _start_changed(_option: SliderOption, value: float) -> None:
    if not _syncing:
        preset_option.value = _match_preset(density_option.value, value)


preset_option = SpinnerOption(
    "Preset",
    DEFAULT_PRESET,
    [*PRESETS, CUSTOM],
    wrap_enabled=True,
    description=(
        "How much fog to keep. Vanilla is the game's own fog; each step down is thinner and starts"
        " further away. Some places (Dominion, coasts that end at the edge of the map) lean on the"
        " fog for their look or to hide the world's edge; pick a lighter preset if that bothers"
        " you."
    ),
    on_change_anytime=_preset_changed,
)
density_option = SliderOption(
    "Fog density",
    PRESETS[DEFAULT_PRESET][0],
    0,
    100,
    5,
    description="How thick the fog is, as a percentage of the game's.",
    on_change_anytime=_density_changed,
)
start_option = SliderOption(
    "Fog start distance",
    PRESETS[DEFAULT_PRESET][1],
    1.0,
    5.0,
    0.25,
    is_integer=False,
    description=(
        "How far away the fog begins, as a multiple of the game's distance. Places where the game"
        " starts fog right at the camera aren't affected."
    ),
    on_change_anytime=_start_changed,
)


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _ticked_at
    started = time.perf_counter()
    if started - _ticked_at < SAME_FRAME_S:
        return
    _ticked_at = started
    try:
        fog.apply(density_option.value / 100, start_option.value)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Less Fog] {ex!r}")


mod = build_mod(
    options=[preset_option, density_option, start_option],
    hooks=[tick],
    on_disable=fog.restore,
)
