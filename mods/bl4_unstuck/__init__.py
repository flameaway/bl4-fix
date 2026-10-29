"""
Unstuck for Borderlands 4: frees a character stuck floating and puts them on the nearest respawn
station.

Sometimes the game holds a character after a teleport and never lets go: it hangs in the air
(gravity 0) and WASD does nothing (hold.py has the details). When that lasts a few seconds, this
lets the character go and lands them on the closest place the game respawns players (a fast
travel or respawn station, respawn.py). "Unstuck now" and the `unstuck` console command do the
same by hand, for any kind of stuck.

Co-op: a client's position belongs to the host, so a client asks the host's copy of this mod
through BL4 Direct's co-op channel; with a host that doesn't run it, the client's own release
still happens and the game's own trip to its respawn station does the move.

Everything but this file can be hot-reloaded (dev/reload_unstuck.py).
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, ButtonOption, SliderOption, build_mod, command, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import UObject, WeakPointer

from . import health, hold, respawn

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo / host only
    coop = None

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

FEATURE = KIND = "unstuck"
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame: act on the first call after a gap, at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
CHECK_EVERY_S = 0.5  # the stuck check
SLOW = 20.0  # uu/s: slower than this counts as not moving
COOLDOWN_S = 30.0  # after a move, another automatic one only after this long (else release only)
AFTER_S = 6.0  # after a release, keep releasing a hold that comes back (a teleport can start it)
AFTER_EVERY_S = 0.25
MENU_WAIT_S = 3.0  # "Unstuck now": longest wait for the menus to close
_last_call = _ticked_at = 0.0

_char_class = unrealsdk.find_class("OakCharacter")
_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject


def log(msg: str) -> None:
    logging.info(f"[Unstuck] {msg}")


class S:
    next_check = 0.0
    last_check = 0.0
    pawn: WeakPointer | None = None  # the character the timer below is about
    stuck_for = 0.0  # unpaused seconds the local character has looked stuck
    last_move = -1e9  # monotonic time of our last move
    after: list[tuple[WeakPointer, float]] = []  # noqa: RUF012 - (pawn, until): watched after a release
    next_after = 0.0
    manual_at = 0.0  # "Unstuck now" waits for the menus to close until this time; 0 = none
    errors: set[str] = set()  # noqa: RUF012


# region Options


auto_option = BoolOption(
    "Unstuck by itself",
    True,
    description=(
        "When your character hangs in the air and can't move for a while (a teleport the game never"
        " finished), it's freed and put on the nearest fast travel or respawn station."
    ),
)
delay_option = SliderOption(
    "Stuck for (seconds)",
    8,
    3,
    30,
    1,
    is_integer=True,
    description="How long your character has to be stuck before Unstuck steps in by itself.",
)


def _press(_: ButtonOption) -> None:
    S.manual_at = time.monotonic() + MENU_WAIT_S
    try:
        from bl4_mod_menu import web  # noqa: PLC0415

        web.request_close()  # never move the player from inside the menu
    except Exception:  # noqa: BLE001 - no Mod Menu: the tick waits a moment and goes
        pass


now_button = ButtonOption(
    "Unstuck now",
    description="Frees your character and puts them on the nearest fast travel or respawn station.",
    on_press=_press,
)


# endregion
# region Doing it


def _character(pc: UObject | None) -> UObject | None:
    pawn = pc.Pawn if pc is not None else None
    if pawn is None or not pawn.Class._inherits(_char_class):
        return None  # loading, dead, or driving
    return pawn


def _watch_after(pawn: UObject) -> None:
    S.after = [(w, t) for w, t in S.after if w() is not None and w() != pawn]
    S.after.append((WeakPointer(pawn), time.monotonic() + AFTER_S))


def _free_and_move(pc: UObject | None) -> dict[str, Any]:
    """Where positions belong (solo / host, for any player running this): release, then move."""
    pawn = _character(pc)
    if pawn is None:
        return {"ok": False, "error": "no character"}
    released = hold.release(pc, pawn)
    _watch_after(pawn)
    reply = respawn.place(pc)
    reply["released"] = released
    return reply


def _host_unstuck(req: Any) -> dict[str, Any]:
    return _free_and_move(req.controller())


def unstuck(why: str, move: bool = True) -> None:
    """Frees the local character and (move) puts them on the nearest respawn station."""
    pc = get_pc()
    pawn = _character(pc)
    if pawn is None:
        log("No character to free (loading, driving or down).")
        return
    released = hold.release(pc, pawn)  # our own copy, also as a client (the movement runs here too)
    _watch_after(pawn)
    if not move:
        log(f"{why}: released {released or 'nothing'}")
        return
    S.last_move = time.monotonic()

    def done(reply: dict[str, Any]) -> None:
        if reply.get("ok"):
            how = "the game's trip to" if reply.get("how") == "trip" else "landed at"
            log(f"{why}: {how} {reply.get('station')} (released {released or reply.get('released') or 'nothing'})")
        else:
            log(f"{why}: couldn't move you ({reply.get('error')})")

    if coop is not None:
        if coop.run(KIND, {}, done, feature_name=FEATURE):
            return
        # A client whose host doesn't run this: the game's own trip to our respawn station
        name = respawn.game_respawn_trip(pc)
        done({"ok": True, "station": name, "how": "trip"} if name else {"ok": False, "error": "no respawn station"})
        return
    done(_free_and_move(pc))


if coop is not None:
    coop.handle(KIND, _host_unstuck)
    coop.feature(FEATURE, lambda: mod.is_enabled)


# endregion
# region Watching


def _looks_stuck(pawn: UObject) -> bool:
    """Hanging still with no gravity, falling or under the movement hold."""
    move = pawn.CharacterMovement
    if move.GravityScale > 0:
        return False
    v = move.Velocity
    if v.X * v.X + v.Y * v.Y + v.Z * v.Z > SLOW * SLOW:
        return False
    return move.MovementMode == 3 or hold.held(pawn)  # 3 = MOVE_Falling


def _check(now: float) -> None:
    dt = min(now - S.last_check, 1.0)  # a loading hitch doesn't count as stuck time
    S.last_check = now
    pc = get_pc()
    pawn = _character(pc)
    if pawn is None or (S.pawn is not None and S.pawn() != pawn):
        S.pawn, S.stuck_for = (WeakPointer(pawn) if pawn is not None else None), 0.0
        return
    S.pawn = S.pawn or WeakPointer(pawn)
    hold.flag_offset(pc)  # finds the hold byte once, with a character
    if not _looks_stuck(pawn):
        S.stuck_for = 0.0
        return
    if _statics.IsGamePaused(ENGINE.GameViewport.World):
        return  # paused mid-jump isn't stuck
    S.stuck_for += dt
    if S.stuck_for < float(delay_option.value):
        return
    S.stuck_for = 0.0
    if now - S.last_move < COOLDOWN_S:
        unstuck("Stuck again", move=False)
    else:
        unstuck("Stuck")


def _check_after(now: float) -> None:
    keep = []
    for weak, until in S.after:
        pawn = weak()
        if pawn is None or now > until:
            continue
        pc = pawn.Controller
        if pc is not None and (hold.held(pawn) or pawn.CharacterMovement.GravityScale <= 0):
            hold.release(pc, pawn)
        keep.append((weak, until))
    S.after = keep


@hook(TICK_FUNC, Type.POST)
def _tick(*_: Any) -> None:
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    now = time.monotonic()
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        if S.manual_at and (now >= S.manual_at or not _menus_up()):
            S.manual_at = 0.0
            unstuck("Unstuck now")
        if S.after and now >= S.next_after:
            S.next_after = now + AFTER_EVERY_S
            _check_after(now)
        if auto_option.value and now >= S.next_check:
            S.next_check = now + CHECK_EVERY_S
            _check(now)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in S.errors:
            S.errors.add(repr(ex))
            logging.exception(ex)


def _menus_up() -> bool:
    """The Mod Menu's page still answers (it only runs while a game menu shows)."""
    try:
        from bl4_mod_menu import web  # noqa: PLC0415

        return web.connected()
    except Exception:  # noqa: BLE001 - no Mod Menu: give the console a moment to close
        return time.monotonic() < S.manual_at - MENU_WAIT_S + 0.3


# endregion


@command("unstuck", description="Frees your character and puts them on the nearest fast travel or respawn station.")
def unstuck_command(_: argparse.Namespace) -> None:
    S.manual_at = time.monotonic() + 0.3  # next tick, with the console closed


def _disable() -> None:
    S.after, S.manual_at, S.stuck_for = [], 0.0, 0.0


mod = build_mod(
    options=[auto_option, delay_option, now_button],
    hooks=[_tick],
    commands=[unstuck_command],
    on_disable=_disable,
)
