"""
Sprint Toggle for Borderlands 4: press once to sprint, press again to walk.

The game's own "toggle" sprint is really press-to-start: stopping, aiming or sliding ends it and it
doesn't come back until you press sprint again. With this on, a press latches sprint until the
next press: after a stop, a shot, a look down the sights, a slide or a jump your character picks
the sprint back up as soon as it moves (sprint.py).

Nothing is bound until you pick it: any key, or your sprint key read from the game's own bindings
(a key you rebound in the game's Options counts); on a controller, your sprint button or another
button (binds.py). "Always sprint" (off by default) latches it without any key.

Co-op: sprint is the game's own movement request on your own character, which your game already
sends to the host as you play, so it works as host or client; nobody else needs the mod.

Everything but this file can be hot-reloaded (dev/reload_sprint_toggle.py).
"""

from __future__ import annotations

import time
from typing import Any

from mods_base import BoolOption, DropdownOption, KeybindOption, SliderOption, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import binds, health, sprint

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

# Fires several times a frame, in menus too
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0
# With the toggle off and nothing pending, a frame only looks at the toggle keys; the full pass
# (character, bindings, the movement flags) runs on a press, while on, and this often otherwise
IDLE_FULL_S = 1.0
# The foreground check is 3 Win32 calls (each lets other threads take Python's lock): at most this often
FOCUS_CHECK_S = 0.25

GAME_KEY = "Your sprint key"
CUSTOM_KEY = "Custom key"
GAME_BUTTON = "Your sprint button"
NO_BUTTON = "Off"


def log(msg: str) -> None:
    logging.info(f"[Sprint Toggle] {msg}")


def _always_changed(_option: BoolOption, _value: bool) -> None:
    Keys.always_changed = True  # applied by the next tick (options may change off the game thread)


always_option = BoolOption(
    "Always sprint",
    False,
    description=(
        "Sprint without pressing anything: the toggle starts on and comes back on after every load"
        " and respawn. Your toggle key still switches it off until then."
    ),
    on_change=_always_changed,
)
key_source_option = DropdownOption(
    "Toggle key",
    CUSTOM_KEY,
    [CUSTOM_KEY, GAME_KEY],
    description=(
        "Custom key: the key below (none until you pick one), and your sprint key works as in the"
        " normal game. Your sprint key: the key sprint is bound to in the game's Options."
    ),
)
custom_key_option = KeybindOption(
    "Custom key",
    None,
    description="The toggle key when Toggle key is set to Custom key. Click it, then press a key.",
)
pad_option = DropdownOption(
    "Controller button",
    NO_BUTTON,
    [NO_BUTTON, GAME_BUTTON, *binds.PAD_CHOICES],
    description=(
        "The controller button that toggles sprint. Your sprint button: the one sprint is bound to in"
        " the game's Options (left stick click unless you changed it)."
    ),
)
resume_option = SliderOption(
    "Resume after shooting (seconds)",
    0.4,
    0.0,
    1.5,
    0.1,
    description=(
        "How long after you stop firing, aiming or crouching before the sprint picks up again. Longer"
        " keeps the gun up between bursts."
    ),
)


class Keys:
    was_down = True  # a key held when we start doesn't count
    errors: set[str] = set()  # noqa: RUF012
    always_changed = False
    full_at = 0.0  # the last full pass
    focused = False
    focus_at = -1e9
    # _toggle_keys' answer and what it was worked out from
    names_from: tuple[Any, ...] = ()
    names: tuple[str, ...] = ()


def _focused(now: float) -> bool:
    if now - Keys.focus_at >= FOCUS_CHECK_S:
        Keys.focus_at, Keys.focused = now, binds.game_focused()
    return Keys.focused


def _toggle_keys() -> tuple[str, ...]:
    """The keys that toggle, rebuilt only when the options or the game's sprint binding change."""
    source = (key_source_option.value, custom_key_option.value, pad_option.value, binds.GameBinding.keys)
    if source != Keys.names_from:
        Keys.names_from, Keys.names = source, _work_out_keys()
    return Keys.names


def _work_out_keys() -> tuple[str, ...]:
    game = binds.GameBinding.keys
    if key_source_option.value == GAME_KEY:
        names = tuple(n for n in game if not n.startswith("Gamepad_"))
    else:
        names = (custom_key_option.value,) if custom_key_option.value else ()
    if pad_option.value == GAME_BUTTON:
        names += tuple(n for n in game if n.startswith("Gamepad_"))
    elif pad_option.value in binds.PAD_CHOICES:
        names += (binds.PAD_CHOICES[pad_option.value],)
    return names


def _air_dash(pawn: UObject, names: tuple[str, ...]) -> bool:
    """In the air the game's sprint key is the dash (logged 2026-10-29): that press isn't a toggle."""
    return pawn.CharacterMovement.MovementMode == sprint.FALLING and any(
        n in binds.GameBinding.keys and binds.any_down((n,)) for n in names
    )


@hook(TICK_FUNC, Type.POST)
def _tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        now = time.monotonic()
        focused = _focused(now)
        names = _toggle_keys()
        # A key held when the game comes back to the front doesn't count
        down = binds.any_down(names) if focused else True
        was, Keys.was_down = Keys.was_down, down
        pressed = down and not was
        state = sprint.State
        # Idle (2026-10-29, user ask: "much lighter passively"): toggle off, no press, nothing
        # pending. Only the key check above, plus a full pass now and then for new bindings and
        # characters
        if not (pressed or state.on or Keys.always_changed or always_option.value or now < state.off_until
                or now - Keys.full_at >= IDLE_FULL_S):  # fmt: skip
            return
        Keys.full_at = now
        pc = get_pc(possibly_loading=True)
        if pc is None:
            return
        pawn = sprint.character(pc)
        if pawn is None and pc.Pawn is None:
            return
        if pawn is not None:
            binds.refresh(pc, pawn, now)
        if Keys.always_changed and pawn is not None:
            Keys.always_changed = False
            sprint.set_on(always_option.value, pawn, now)
        if pressed and pawn is not None and not pc.bShowMouseCursor and not _air_dash(pawn, names):
            sprint.set_on(not sprint.State.on, pawn, now)
            log("on" if sprint.State.on else "off")
        if pawn is not None:
            busy = sprint.State.on and focused and binds.any_down(binds.GameBinding.busy)
            sprint.tick(pawn, now, resume_option.value, always=always_option.value, input_busy=busy)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in Keys.errors:
            Keys.errors.add(repr(ex))
            logging.exception(ex)


def _enable() -> None:
    Keys.was_down = True
    sprint.reset()
    Keys.always_changed = always_option.value


def _disable() -> None:
    sprint.reset()  # the game's own sprint state is left as it is: it ends at the next stop


mod = build_mod(
    options=[always_option, key_source_option, custom_key_option, pad_option, resume_option],
    hooks=[_tick],
    on_enable=_enable,
    on_disable=_disable,
)
