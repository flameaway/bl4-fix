"""
Less Fog for Borderlands 4: remove the fog and the blue haze over the world ("No fog", the default
since 2026-10-29), or thin them out.

The game's fog is driven by each map's time-of-day cycle. Every frame we check the numbers it
picked and scale them (see fog.py): lower density, and fog that starts further away; "No fog"
takes the density to 0. Changes show at once, no restart. Turning the mod off puts the game's fog
back. Once a frame through the shared frame tick (frametick/): no hook of its own, and on Vanilla
nothing at all.

Purely visual and local: each player picks their own.
"""

from __future__ import annotations

from mods_base import SliderOption, SpinnerOption, build_mod
from unrealsdk import logging

from . import fog, frametick

__version__ = "0.1.2"
__version_info__ = (0, 1, 2)


CUSTOM = "Custom"
# name: (fog density %, start distance x, distance haze %)
PRESETS = {
    "Vanilla": (100, 1.0, 100),
    "Light": (75, 1.5, 100),
    "Balanced": (50, 2.0, 100),
    "Clear": (25, 3.0, 100),
    "Very clear": (10, 4.0, 100),
    "No fog": (0, 1.0, 0),
}
DEFAULT_PRESET = "No fog"  # the user's pick (2026-10-29): what people use it for

_syncing = False


def _match_preset(density: float, start: float, haze: float) -> str:
    for name, (d, s, h) in PRESETS.items():
        if d == round(density) and s == start and h == round(haze):
            return name
    return CUSTOM


def _preset_changed(_option: SpinnerOption, value: str) -> None:
    global _syncing
    if value not in PRESETS or _syncing:
        return
    _syncing = True
    try:
        density_option.value, start_option.value, haze_option.value = PRESETS[value]
    finally:
        _syncing = False


def _density_changed(_option: SliderOption, value: float) -> None:
    if not _syncing:
        preset_option.value = _match_preset(value, start_option.value, haze_option.value)


def _start_changed(_option: SliderOption, value: float) -> None:
    if not _syncing:
        preset_option.value = _match_preset(density_option.value, value, haze_option.value)


def _haze_changed(_option: SliderOption, value: float) -> None:
    if not _syncing:
        preset_option.value = _match_preset(density_option.value, start_option.value, value)


preset_option = SpinnerOption(
    "Preset",
    DEFAULT_PRESET,
    [*PRESETS, CUSTOM],
    wrap_enabled=True,
    description=(
        "How much fog to keep. Vanilla is the game's own fog; each step down is thinner and starts"
        " further away. No fog removes it completely: the fog, local fog patches and the blue"
        " distance haze. Some places (Dominion, coasts that end at the edge of the map) lean on the"
        " fog for their look or to hide the world's edge; pick a lighter preset if that bothers you."
    ),
    on_change_anytime=_preset_changed,
)
density_option = SliderOption(
    "Fog density",
    PRESETS[DEFAULT_PRESET][0],
    0,
    100,
    5,
    description="How thick the fog is (and local fog patches), as a percentage of the game's. 0 = no fog.",
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
haze_option = SliderOption(
    "Distance haze",
    PRESETS[DEFAULT_PRESET][2],
    0,
    100,
    5,
    description=(
        "The sky's blue haze over far-away ground, which stays even with the fog at 0, as a"
        " percentage of the game's. 0 = none: distant hills look as clear as near ones."
    ),
    on_change_anytime=_haze_changed,
)


class _S:
    restored = False  # on Vanilla: the game's fog is back, nothing to do until the settings change


# Once a frame: the time of day writes its fog numbers before the frame's first camera update, never
# between the two (World_P 2026-10-29), so one look a frame keeps the fog ours
@frametick.tick("bl4_less_fog")
def tick() -> None:
    try:
        density, start, haze = density_option.value / 100, start_option.value, haze_option.value / 100
        if density == 1 and start == 1 and haze == 1:
            if not _S.restored:
                fog.restore()
                _S.restored = True
            return
        _S.restored = False
        fog.apply(density, start, haze)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        # Each distinct error once: a game patch renaming something this reads would fail every frame
        text = repr(ex)[:300]
        if text not in _logged_errors and len(_logged_errors) < 20:  # noqa: PLR2004
            _logged_errors.add(text)
            logging.error(f"[Less Fog] {text} (logged once)")


_logged_errors: set[str] = set()


def _disable() -> None:
    fog.restore()
    _S.restored = False


mod = build_mod(
    options=[preset_option, density_option, start_option, haze_option],
    hooks=[tick],
    on_disable=_disable,
)
