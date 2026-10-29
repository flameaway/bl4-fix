"""
Instant Menu Tabs for Borderlands 4: switching pages in the ECHO menu (inventory, skills, map,
missions, ...) happens right away instead of after a short pause and a transition.

What slows opening and switching down, and what we do about it:

- The game waits 0.4 s after each tab change before it shows the new page (so you can flick past
  pages quickly). We patch that wait out of the game's code (patch.DELAY).
- It then plays a hexagon wipe over the screen that keeps the page hidden for 0.35 s. We patch the
  tab bar so it skips that effect (patch.EFFECT).
- Opening the menu plays a triangle animation that hides the page for 0.49 s (patch.OPEN), and a
  warp particle in front of the camera (patch.WARP). We skip both.
- Each page then fades in from invisible over 0.25 s. That's the menu page's JavaScript, so a
  script of ours ends the fade as soon as it starts (ui/fade.js, loaded by ui/loader.js). The BL4
  UI Link (uilink/, shared by our mods; no game page replaced) runs it in the menu page. Until
  0.1.x our copies of the inventory and map pages in a pak did; pages/ removes that pak after the
  game exits and checks the game's pages are still the ones the script was made for.

Two extras, off by default: hide the 3D character model behind the pages, and a see-through
menu that drops the background so you can watch the game behind it (the game keeps running).

The code patches are made while the mod is enabled and undone when it's disabled. The Options
menu's tabs use the same code and switch instantly too.
"""

from __future__ import annotations

import json
import time
from importlib import resources
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR, BoolOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import health, pages, patch, uilink

__version__ = "0.2.1"
__version_info__ = (0, 2, 1)

GAME_DIR = SETTINGS_DIR.parent.parent
UI_DIR = GAME_DIR / "OakGame" / "Content" / "uiresources" / "bl4instanttabs"
UI_FILES = ("loader.js", "fade.js")
# The fade skip's loader, run by the BL4 UI Link in the menu page
LINK_KEY = "instanttabs"
LINK_SCRIPT = "coui://uiresources/bl4instanttabs/loader.js"


def log(msg: str) -> None:
    logging.info(f"[Instant Tabs] {msg}")


def _write(target: Path, data: bytes) -> None:
    """Writes a loose UI file, touching it only if it changed."""
    try:
        if target.read_bytes() == data:
            return
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def _publish(skip_fade: bool) -> None:
    """Copies our page scripts next to the game's UI and tells them whether to skip the fade."""
    assets = resources.files(__package__) / "ui"
    try:
        for name in UI_FILES:
            _write(UI_DIR / name, (assets / name).read_bytes())
        _write(UI_DIR / "config.json", json.dumps({"skipFade": skip_fade}).encode())
    except OSError as ex:
        logging.error(f"[Instant Tabs] couldn't write the page scripts: {ex!r}")


_errors: set[str] = set()

# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0


@hook("/Script/Engine.CameraModifier:BlueprintModifyCamera", Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    """Only drives the shared link and health checks (other mods' copies may do it instead)."""
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    try:
        uilink.tick()  # runs the fade skip in the menu page (shared/uilink; at most once a frame overall)
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
    except Exception as ex:  # noqa: BLE001 - never let this break the game's tick
        message = repr(ex)
        if message not in _errors:
            _errors.add(message)
            logging.error(f"[Instant Tabs] {message}")


def _set_patch(site: patch.Site, on: bool) -> None:
    found = patch.get(site)
    if found is None:
        return
    try:
        if on:
            found.apply()
        else:
            found.restore()
    except Exception as ex:  # noqa: BLE001 - the game keeps that part as it was
        logging.error(f"[Instant Tabs] {ex!r}")


def _set_transitions(skip: bool) -> None:
    for site in patch.TRANSITIONS:
        _set_patch(site, skip)
    _publish(skip)


def _transitions_changed(_option: BoolOption, value: bool) -> None:
    _set_transitions(value)


transitions_option = BoolOption(
    "Skip page transitions",
    True,
    description=(
        "Open the ECHO menu and show each page at once: no triangle animation or warp when it"
        " opens, no hexagon wipe or fade-in when you switch pages."
    ),
    on_change_while_enabled=_transitions_changed,
)


character_option = BoolOption(
    "Hide character model",
    False,
    description=(
        "Don't show your 3D character behind the ECHO menu pages. The menu opens a little"
        " lighter, and it's what you want with a see-through menu."
    ),
    on_change_while_enabled=lambda _option, value: _set_patch(patch.CHARACTER, value),
)
see_through_option = BoolOption(
    "See-through ECHO menu",
    False,
    description=(
        "Drop the ECHO menu's background so you can see the game behind it. The game keeps"
        " running while the menu is open, even in single player."
    ),
    on_change_while_enabled=lambda _option, value: _set_patch(patch.SEE_THROUGH, value),
)


def _enable() -> None:
    _set_patch(patch.DELAY, True)
    _set_transitions(transitions_option.value)
    _set_patch(patch.CHARACTER, character_option.value)
    _set_patch(patch.SEE_THROUGH, see_through_option.value)
    try:
        log(pages.sync(GAME_DIR))
    except Exception as ex:  # noqa: BLE001 - the pages just keep their fade
        logging.error(f"[Instant Tabs] page fade skip: {ex!r}")
    if pages.page_current:
        uilink.register(LINK_KEY, LINK_SCRIPT, dict, lambda _action: None, views=(uilink.MENUS,), mod=__name__)
    else:
        uilink.unregister(LINK_KEY)


def _disable() -> None:
    _set_patch(patch.DELAY, False)
    # The page tears the loader down (fade.js unwrapped); a loader the old pak started (until the
    # next game start) keeps reading config.json, so that turns the skip off too
    uilink.unregister(LINK_KEY)
    _set_transitions(False)
    _set_patch(patch.CHARACTER, False)
    _set_patch(patch.SEE_THROUGH, False)


mod = build_mod(
    options=[transitions_option, character_option, see_through_option],
    hooks=[tick],
    on_enable=_enable,
    on_disable=_disable,
)
