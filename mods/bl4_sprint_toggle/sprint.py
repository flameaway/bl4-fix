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
aiming, crouching or sliding, and for a moment after each (RESUME_S) so a shot or a peek down the
sights isn't cut short by the sprint starting again. The game still decides whether a sprint can
start (direction, stamina, gliding...); we only repeat the request.
"""

from __future__ import annotations

from typing import Any

import unrealsdk

WALKING, FALLING = 1, 3  # EMovementMode
OFF_HOLD_S = 0.3  # after turning off: keep the game's own press (same key) from restarting it
ASK_EVERY_S = 0.1  # a refused request is repeated at most this often
BUSY_WEAPON = frozenset({"Using"})  # firing; "Reloading", "Equipping", "PuttingDown" allow sprint

_char_class = unrealsdk.find_class("OakCharacter")


class State:
    on = False
    off_until = 0.0  # stop any sprint until then (just turned off)
    busy_until = 0.0  # the player was firing / aiming / crouching: don't ask before then
    asked_at = 0.0
    pawn = 0


def character(pc: Any) -> Any | None:
    """The local player's character on foot, None in a vehicle or with no pawn."""
    pawn = pc.Pawn
    if pawn is None or not pawn.Class._inherits(_char_class):
        return None
    return pawn


def set_on(on: bool, pawn: Any | None, now: float) -> None:
    """The toggle was pressed. Turning on asks through tick() (same frame), so it waits its turn too."""
    State.on = on
    State.busy_until = State.asked_at = 0.0
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


def _busy(pawn: Any, cmc: Any, input_busy: bool) -> bool:
    """Something the player is doing that sprint would cut short."""
    if input_busy or cmc.bWantsToCrouch or cmc.bWantsToSlide or pawn.bIsCrouched:
        return True
    zoom = pawn.ZoomState
    if zoom.State or zoom.bWantsToZoom:
        return True
    for slot in pawn.ActiveWeapons.Slots:
        weapon = slot.Weapon
        if weapon is not None and str(weapon.CurrentState) in BUSY_WEAPON:
            return True
    return False


def tick(pawn: Any, now: float, resume_s: float, *, always: bool, input_busy: bool) -> None:
    """
    Once a frame with the local character on foot. The toggle survives loads and respawns; with
    always, a new character starts with it on. input_busy: the fire or aim key is held (the
    weapon's state only follows a few frames later).
    """
    cmc = pawn.CharacterMovement
    if cmc is None:
        return
    address = pawn._get_address()
    if address != State.pawn:  # respawn, new map, out of a vehicle
        State.pawn, State.busy_until, State.asked_at = address, 0.0, 0.0
        if always:
            State.on = True
    if not State.on:
        if now < State.off_until and (cmc.bIsSprinting or cmc.bWantsToSprint):
            _stop(cmc)
        return
    if cmc.bIsSprinting:
        return
    mode = cmc.MovementMode
    if mode == FALLING:
        # The game resumes by itself on landing as long as the request stands
        if not cmc.bWantsToSprint:
            cmc.bWantsToSprint = True
        return
    if mode != WALKING:
        return  # swimming, ladders, custom moves (grapple...): the game's business
    if _busy(pawn, cmc, input_busy):
        State.busy_until = now + resume_s
        if State.asked_at and cmc.bWantsToStartSprinting:
            # Our request, still pending: the game would start the sprint the moment the shot ends,
            # skipping the resume delay
            cmc.bWantsToStartSprinting = False
        return
    if now < State.busy_until or now - State.asked_at < ASK_EVERY_S:
        return
    a = cmc.Acceleration
    if a.X == 0 and a.Y == 0:
        return  # standing still: asking would only be cleared again
    _ask(cmc, now)


def reset() -> None:
    State.on = False
    State.off_until = State.busy_until = State.asked_at = 0.0
    State.pawn = 0
