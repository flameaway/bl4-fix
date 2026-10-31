"""
Sprint Toggle for Borderlands 4: press once to sprint, press again to walk.

The game's own "toggle" sprint is really press-to-start: stopping, aiming or sliding ends it and it
doesn't come back until you press sprint again. With this on, a press latches sprint until the
next press: after a stop, a shot, a look down the sights, a slide or a jump your character picks
the sprint back up as soon as it moves (sprint.py).

Nothing is bound until you pick a toggle key. No controller button and no "use your sprint key"
mode (removed 2026-10-29, the user's call): on a controller, "Always sprint" (off by default)
latches it without any key. The game's own bindings are still read for the fire / aim keys
(binds.py), which pause the request while you shoot or aim, pads included.

Co-op: sprint is the game's own movement request on your own character, which your game already
sends to the host as you play, so it works as host or client; nobody else needs the mod.

Everything but this file can be hot-reloaded (dev/reload_sprint_toggle.py).
"""

from __future__ import annotations

import time
import traceback

from mods_base import BoolOption, KeybindOption, build_mod, get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

from . import binds, frametick, health, sprint

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

# With the toggle off and nothing pending, a frame only looks at the toggle keys; the full pass
# (character, bindings, the movement flags) runs on a press, while on, and this often otherwise
IDLE_FULL_S = 1.0
# Always sprint with the toggle off (switched off by the key): a new character turns it back on
# within this long
ALWAYS_CHECK_S = 0.25
# The foreground check is 3 Win32 calls (each lets other threads take Python's lock): at most this often
FOCUS_CHECK_S = 0.25
# With the toggle on the full pass runs every frame: the player controller is kept (a WeakPointer)
# and looked up again this often (get_pc is 10-13 us inside the frame tick), and the "needs an
# update" check runs this often too (2026-10-29, user: "should be a free mod")
PC_CHECK_S = 1.0
HEALTH_S = 0.25

def log(msg: str) -> None:
    logging.info(f"[Sprint Toggle] {msg}")


def _always_changed(_option: BoolOption, _value: bool) -> None:
    Keys.always_changed = True  # applied by the next tick (options may change off the game thread)


always_option = BoolOption(
    "Always sprint",
    False,
    description=(
        "Sprint without pressing anything: the toggle starts on and comes back on after every load"
        " and respawn. Your toggle key still switches it off until then. On a controller, use this:"
        " there's no toggle button."
    ),
    on_change=_always_changed,
)
# The identifier stays "Custom key" so a key players already bound is kept
custom_key_option = KeybindOption(
    "Custom key",
    None,
    display_name="Toggle key",
    description=(
        "The key that switches sprint on and off (none until you pick one). Click it, then press a"
        " key. Your sprint key works as in the normal game."
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
    names_from: str | None = None
    names: tuple[str, ...] = ()
    pc = WeakPointer()
    pc_at = -1e9
    health_at = -1e9


def _pc(now: float) -> UObject | None:
    pc = Keys.pc()
    if pc is None or now - Keys.pc_at >= PC_CHECK_S:
        Keys.pc_at = now
        pc = get_pc(possibly_loading=True)
        Keys.pc = WeakPointer(pc) if pc is not None else WeakPointer()
    return pc


def _focused(now: float) -> bool:
    if now - Keys.focus_at >= FOCUS_CHECK_S:
        Keys.focus_at, Keys.focused = now, binds.game_focused()
    return Keys.focused


def _toggle_keys() -> tuple[str, ...]:
    """The toggle key as a 1-tuple (empty while unbound), rebuilt only when the binding changes."""
    key = custom_key_option.value
    if key != Keys.names_from:
        Keys.names_from, Keys.names = key, ((key,) if key else ())
    return Keys.names


def _air_dash(pawn: UObject, names: tuple[str, ...]) -> bool:
    """In the air the game's sprint key is the dash (logged 2026-10-29): that press isn't a toggle."""
    return pawn.CharacterMovement.MovementMode == sprint.FALLING and any(
        n in binds.GameBinding.keys and binds.any_down((n,)) for n in names
    )


@frametick.tick("bl4_sprint_toggle")
def _tick() -> None:
    """Once per engine frame (the shared frame tick), in menus too."""
    try:
        now = time.monotonic()
        if now - Keys.health_at >= HEALTH_S:
            Keys.health_at = now
            health.tick()  # "needs an update" statuses for all our mods (shared/health)
        names = _toggle_keys()
        focused = _focused(now)
        # A key held when the game comes back to the front doesn't count
        down = (binds.any_down(names) if focused else True) if names else False
        was, Keys.was_down = Keys.was_down, down
        pressed = down and not was
        state = sprint.State
        # Only the key check above on most frames (2026-10-29, user: "should be a free mod"). The
        # full pass (controller, character, bindings, the movement flags) runs on a press, an
        # option change and through the off-hold, and otherwise: with the toggle on when
        # sprint.tick asked for it (State.next_at: ~10 times a second while sprinting, 40 while it
        # has something to do); off, every IDLE_FULL_S for new bindings and characters, or
        # ALWAYS_CHECK_S with Always sprint (a new character turns it on)
        if not (pressed or Keys.always_changed or now < state.off_until):
            if state.on:
                if now < state.next_at:
                    return
            elif now - Keys.full_at < (ALWAYS_CHECK_S if always_option.value else IDLE_FULL_S):
                return
        Keys.full_at = now
        state.next_at = now + sprint.PAWN_CHECK_S  # no character on foot (a load, a vehicle); tick() sets its own
        pc = _pc(now)
        if pc is None:
            return
        pawn = sprint.character(pc, now)
        if pawn is None and pc.Pawn is None:
            return
        if pawn is not None:
            binds.refresh(pc, pawn, now, sprint.State.pawn_addr)
        if Keys.always_changed and pawn is not None:
            Keys.always_changed = False
            sprint.set_on(always_option.value, pawn, now)
        if pressed and pawn is not None and not pc.bShowMouseCursor and not _air_dash(pawn, names):
            sprint.set_on(not sprint.State.on, pawn, now)
            log("on" if sprint.State.on else "off")
        if pawn is not None:
            # The fire / aim keys are read only when sprint.tick needs them (not while sprinting)
            def busy() -> bool:
                return sprint.State.on and focused and binds.any_down(binds.GameBinding.busy)

            sprint.tick(pawn, now, always=always_option.value, input_busy=busy)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in Keys.errors:
            Keys.errors.add(repr(ex))
            logging.error("".join(traceback.format_exception(ex)))


def _enable() -> None:
    Keys.was_down = True
    Keys.pc, Keys.pc_at = WeakPointer(), -1e9
    sprint.reset()
    Keys.always_changed = always_option.value


def _disable() -> None:
    sprint.reset()  # the game's own sprint state is left as it is: it ends at the next stop


mod = build_mod(
    options=[always_option, custom_key_option],
    hooks=[_tick],
    on_enable=_enable,
    on_disable=_disable,
)
