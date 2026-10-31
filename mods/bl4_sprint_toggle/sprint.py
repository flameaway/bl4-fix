"""
Keeps the local character sprinting while the toggle is on.

How the game's sprint works (logged live, 2026-10-29, OakCharacterMovementComponent):
- `bWantsToSprint` is the request, `bIsSprinting` the state. Pressing sprint sets both
  `bWantsToSprint` and `bWantsToStartSprinting`; the latter is consumed when the sprint starts, or
  held while the game won't allow it (firing), until then.
- The game's own "toggle" setting is really press-to-start: stopping (no move input) clears
  `bWantsToSprint`, and so does a slide. Aiming stops the sprint but leaves `bWantsToSprint` set,
  and it doesn't come back until sprint is pressed again. A jump stops it and landing resumes it
  by itself. Reloading and melee don't stop it; firing does.
- Pressing sprint while sprinting does nothing (it never turns the game's sprint off).

So while the toggle is on we ask again (both flags, like a press) whenever the character moves
and isn't sprinting, except while something the player is doing wins over sprint: firing,
aiming, crouching or sliding; the sprint picks up again as soon as they end. The game still
decides whether a sprint can start (direction, stamina, gliding...); we only repeat the request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import unrealsdk
from unrealsdk.unreal import WeakPointer

WALKING, FALLING = 1, 3  # EMovementMode
OFF_HOLD_S = 0.3  # after turning off: keep the game's own press (same key) from restarting it
ASK_EVERY_S = 0.1  # a refused request is repeated at most this often
BUSY_WEAPON = frozenset({"Using"})  # firing; "Reloading", "Equipping", "PuttingDown" allow sprint
# The weapons' states (every slot's weapon and its state name: the costly part of the busy checks)
# are read at most this often. The fire key itself counts as busy at once, and the state lags the
# press by a few frames anyway. Crouch, slide and zoom are still read every frame.
WEAPON_CHECK_S = 0.05
PAWN_CHECK_S = 0.25  # character(): pc.Pawn read again this often while the kept pawn lives
# Standing still with nothing of ours pending there's nothing to do: the movement flags (4 property
# reads, ~30 us a frame inside the tick) are looked at again after this long. Sprint can pick up at
# most this much later after you start moving (2026-10-29, user: "should be a free mod")
STILL_RECHECK_S = 0.05
# With the toggle on, tick() says when it next needs to run (State.next_at) and the frame tick skips
# the whole pass until then (controller, character, bindings: ~25 us a frame inside the tick, 2026-10-29):
# while sprinting there's nothing to do, so it looks again after SPRINTING_RECHECK_S (a stopped sprint
# is picked up at most this much later; aiming, shots and stops last longer than that); with a request
# of ours pending, or busy checks to make, every PASS_S
SPRINTING_RECHECK_S = 0.1
PASS_S = 0.025

_char_class = unrealsdk.find_class("OakCharacter")


class State:
    on = False
    off_until = 0.0  # stop any sprint until then (just turned off)
    asked_at = 0.0
    pawn = 0
    # character()'s last class check: the pawn's address and whether it's an OakCharacter
    checked = (0, False)
    cmc = WeakPointer()  # the pawn's movement component (read once per pawn)
    weapon_at = -1e9  # the last weapon-state read, and what it said
    weapon_busy = False
    pawn_ref = WeakPointer()  # character()'s kept pawn, its address, and when pc.Pawn was read
    pawn_addr = 0
    pawn_at = -1e9
    still_until = 0.0  # standing still: nothing looked at until then (STILL_RECHECK_S)
    next_at = 0.0  # toggle on: the frame tick's next pass (see PASS_S)


def character(pc: Any, now: float | None = None) -> Any | None:
    """
    The local player's character on foot, None in a vehicle or with no pawn. With `now`, the pawn is
    kept (a WeakPointer, its address with it) and `pc.Pawn` read again every PAWN_CHECK_S or once it's
    gone (a death or a new map: at once). Getting into a vehicle shows up within PAWN_CHECK_S.
    """
    pawn = State.pawn_ref()
    if pawn is None or now is None or now - State.pawn_at >= PAWN_CHECK_S:
        pawn = pc.Pawn
        State.pawn_at = now if now is not None else -1e9
        State.pawn_ref = WeakPointer(pawn) if pawn is not None else WeakPointer()
        State.pawn_addr = pawn._get_address() if pawn is not None else 0
        if pawn is None:
            return None
    # The class check once per pawn, not every frame (a pawn's class never changes)
    address = State.pawn_addr
    if State.checked[0] != address:
        State.checked = (address, bool(pawn.Class._inherits(_char_class)))
    return pawn if State.checked[1] else None


def set_on(on: bool, pawn: Any | None, now: float) -> None:
    """The toggle was pressed. Turning on asks through tick() (same frame), so it waits its turn too."""
    State.on = on
    State.asked_at = State.still_until = State.next_at = 0.0
    if on:
        State.off_until = 0.0
    else:
        State.off_until = now + OFF_HOLD_S
        if pawn is not None:
            _stop(pawn.CharacterMovement)


def _stop(cmc: Any) -> None:
    if cmc.bWantsToStartSprinting:
        cmc.bWantsToStartSprinting = False
    if cmc.bWantsToSprint:
        cmc.bWantsToSprint = False


def _ask(cmc: Any, now: float) -> None:
    State.asked_at = now
    if not cmc.bWantsToSprint:
        cmc.bWantsToSprint = True
    if not cmc.bWantsToStartSprinting:
        cmc.bWantsToStartSprinting = True


def _busy(pawn: Any, cmc: Any, input_busy: bool, now: float) -> bool:
    """Something the player is doing that sprint would cut short."""
    if input_busy or cmc.bWantsToCrouch or cmc.bWantsToSlide or pawn.bIsCrouched:
        return True
    zoom = pawn.ZoomState
    if zoom.State or zoom.bWantsToZoom:
        return True
    if now - State.weapon_at >= WEAPON_CHECK_S:
        State.weapon_at, State.weapon_busy = now, _weapon_busy(pawn)
    return State.weapon_busy


def _weapon_busy(pawn: Any) -> bool:
    slots = pawn.ActiveWeapons.Slots
    for i in range(len(slots)):  # by index: iterating a game array builds a list (~18-55 us in the tick)
        weapon = slots[i].Weapon
        if weapon is not None and str(weapon.CurrentState) in BUSY_WEAPON:
            return True
    return False


def tick(pawn: Any, now: float, *, always: bool, input_busy: bool | Callable[[], bool]) -> None:
    """
    Once a frame with the local character on foot. The toggle survives loads and respawns; with
    always, a new character starts with it on. input_busy: the fire or aim key is held (the
    weapon's state only follows a few frames later); a function is only asked when it matters
    (not while sprinting: the keys were read every frame of a sprint for nothing).
    """
    address = State.pawn_addr if pawn is State.pawn_ref() else pawn._get_address()
    if address != State.pawn:  # respawn, new map, out of a vehicle
        State.pawn, State.asked_at, State.still_until = address, 0.0, 0.0
        State.cmc = WeakPointer(pawn.CharacterMovement)
        if always:
            State.on = True
    cmc = State.cmc()  # kept per pawn: a property read every frame costs a few us in the tick
    if cmc is None:
        cmc = pawn.CharacterMovement
        if cmc is None:
            return
        State.cmc = WeakPointer(cmc)
    if not State.on:
        if now < State.off_until and (cmc.bIsSprinting or cmc.bWantsToSprint):
            _stop(cmc)
        return
    if now < State.still_until:
        State.next_at = State.still_until
        return
    if cmc.bIsSprinting:
        State.next_at = now + SPRINTING_RECHECK_S
        return
    State.next_at = now + PASS_S
    mode = cmc.MovementMode
    if mode == FALLING:
        # The game resumes by itself on landing as long as the request stands
        if not cmc.bWantsToSprint:
            cmc.bWantsToSprint = True
        return
    if mode != WALKING:
        return  # swimming, ladders, custom moves (grapple...): the game's business
    # Our request, still pending: dropped while something is busy, so the game can't start the
    # sprint in the middle of it (we ask again once it's over)
    pending = bool(State.asked_at) and cmc.bWantsToStartSprinting
    time_due = now - State.asked_at >= ASK_EVERY_S
    if not (pending or time_due):
        # Just asked, nothing of ours waiting: the acceleration (a struct read) isn't needed, and
        # nothing changes before the next ask is due
        State.next_at = max(State.next_at, State.asked_at + ASK_EVERY_S)
        return
    a = cmc.Acceleration
    moving = a.X != 0 or a.Y != 0
    if not moving and not pending:
        State.still_until = State.next_at = now + STILL_RECHECK_S
        return
    due = moving and time_due
    # Nothing to ask and nothing of ours to drop this frame (just asked, or standing still): the busy
    # checks wouldn't change anything, so they're skipped (same outcome as before, 2026-10-29)
    if not (pending or due):
        return
    keys_busy = input_busy() if callable(input_busy) else input_busy
    # Standing still there's nothing to ask for (it would only be cleared again), so the weapon
    # checks in _busy (every slot's state: the costly part, 0.17 ms a frame with the toggle on while
    # standing, 2026-10-29) wait until you move. The fire / aim keys still count.
    if (_busy(pawn, cmc, keys_busy, now) if moving else keys_busy):
        if pending:
            cmc.bWantsToStartSprinting = False
        return
    if due:
        _ask(cmc, now)


def reset() -> None:
    State.on = False
    State.off_until = State.asked_at = State.still_until = State.next_at = 0.0
    State.pawn = 0
    State.checked = (0, False)
    State.cmc = WeakPointer()
    State.weapon_at, State.weapon_busy = -1e9, False
    State.pawn_ref, State.pawn_addr, State.pawn_at = WeakPointer(), 0, -1e9
