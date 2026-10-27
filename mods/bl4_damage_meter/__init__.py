"""
Carnage Meter for Borderlands 4: who did the damage, and with what.

Open the Carnage Report (F9, the pause menu's Carnage Report entry, or `carnage`) for every
player's damage in this fight and this session: total, damage per second, kills, crit damage,
biggest hit, share of the team's damage, and each player's top damage sources, named the way the
game names them: the gun ("Hellwalker"), the grenade, the shield, an element's damage over time
("Burning", "Bleed"...), melee, ground slams, skills, companions. A boss kill shows a results pop-up
on the HUD (popup_page) and fills the report's Boss Report tab.

How: the host's damage pipeline tells each character about every hit it deals
(`DamageCauserData.OnCausedAnyDamage`); this mod listens on that (native.py) and counts
(tracker.py). In co-op the host counts everyone; a friend running the mod sees the host's board
(share.py, through BL4 Direct). The screen is HTML in the game's menu page through the BL4 Mod
Menu (page.py, ui/meter.js), which must be installed.
"""

from __future__ import annotations

import argparse
import threading
import time
from typing import Any

from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, KeybindOption, SliderOption, build_mod, command, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import naming, native, page, popup_page, share, tracker

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

GAME_DIR = SETTINGS_DIR.parent.parent
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame: act on the first call after a gap, at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012


def log(msg: str) -> None:
    logging.info(f"[Carnage Meter] {msg}")


# region Options


open_key = KeybindOption(
    "Open key",
    "F9",
    description="Opens and closes the Carnage Report. Works in game and in the game's menus.",
)


def _gap_changed(_: SliderOption, value: float) -> None:
    tracker.FIGHT_GAP_S = float(value)


fight_gap = SliderOption(
    "Fight ends after (seconds)",
    10,
    3,
    60,
    1,
    is_integer=True,
    description=(
        "A fight is over when nobody has hit an enemy for this long; the next hit starts a new one."
        " This Fight shows the current fight, or the last one until the next begins."
    ),
    on_change=_gap_changed,
)

def _popup_changed(_: BoolOption, value: bool) -> None:
    _sync_popup(bool(value))


report_option = BoolOption(
    "Boss fight pop-up",
    True,
    description=(
        "When a boss dies, a results panel shows on screen for a few seconds: each player's damage to"
        " the boss, their top damage source and the MVP. The full report is on the Carnage Report's"
        " Boss Report tab either way. The first time it's turned on it needs one game restart."
    ),
    on_change=_popup_changed,
)


_popup_lock = threading.Lock()
_popup = {"wanted": False, "queued": False, "done": None}


def _sync_popup(enabled: bool) -> None:
    """
    Installs or removes the pop-up's HUD page (pak lookups take about a second: a worker thread).
    One worker at a time, applying the latest wish, so a quick off-on can't finish as off; enable
    and the option's load (both at startup) share one sync.
    """
    with _popup_lock:
        _popup["wanted"] = enabled
        if _popup["queued"]:
            return
        _popup["queued"] = True

    def run() -> None:
        while True:
            with _popup_lock:
                wanted = _popup["wanted"]
                if wanted == _popup["done"]:
                    _popup["queued"] = False
                    return
            try:
                log(popup_page.sync(GAME_DIR, wanted))
            except Exception as ex:  # noqa: BLE001
                logging.error(f"[Carnage Meter] pop-up page: {ex!r}")
            _popup["done"] = wanted

    threading.Thread(target=run, name="bl4_damage_meter.popup", daemon=True).start()

open_button = ButtonOption(
    "Open the Carnage Report",
    description="Shows every player's damage, kills and top damage sources.",
    on_press=lambda _: page.request_open(),
)


def _reset(_: ButtonOption) -> None:
    if share.role() == "client":
        log("Only the host can reset the board.")
        return
    tracker.reset()
    page.say("Board reset.")


reset_button = ButtonOption("Reset the board", description="Starts the fight and session totals over.", on_press=_reset)

page.S.key_name = lambda: str(open_key.value or "")
page.S.report_popup = lambda: bool(report_option.value)
tracker.GAME_DIR = GAME_DIR


# endregion
# region Console


def _fake_report(name: str) -> None:
    """Dev: a boss report from the current fight, as if `name` just died (`carnage report <name>`)."""
    boss = tracker.Boss.__new__(tracker.Boss)
    boss.addr, boss.actor, boss.name, boss.picture = 0, None, name or "Test Boss", ""
    boss.first, boss.last = tracker.S.fight_start, tracker.S.fight_last
    boss.damage = {key: p.fight.total for key, p in tracker.S.players.items() if p.fight.total > 0}
    if not boss.damage:
        log("Nothing in this fight to report: damage something first (or run dev/carnage_fake_board.py).")
        return
    tracker._make_report(boss, time.monotonic())  # noqa: SLF001


@command("carnage", description="Opens the Carnage Report. `carnage reset` starts it over, `carnage stats` prints diagnostics.")
def carnage_command(args: argparse.Namespace) -> None:
    verb = (args.verb or "").lower()
    if verb == "reset":
        _reset(reset_button)
    elif verb == "stats":
        log(tracker.stats())
    elif verb == "report":
        _fake_report(" ".join(args.rest))
    elif verb:
        log("Usage: carnage | carnage reset | carnage stats | carnage report [boss name]")
    else:
        S.pending_open = time.monotonic() + 0.3  # the console is still open on this frame


carnage_command.add_argument("verb", nargs="?")
carnage_command.add_argument("rest", nargs="*")


# endregion
# region Hooks


class S:
    pending_open = 0.0
    key_was_down = True
    registered = False
    ticked_at = last_call = 0.0
    setup_failed = False
    session: tuple[int, int, int, int] | None = None  # the game session the board belongs to
    session_checked = 0.0


@hook(native.FUNC_PATH, Type.PRE)
def caused_damage(obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> Any:
    # Our binding calls this on the class's default object; anything else is the game's own (an
    # augment script with that event), which we leave alone
    if obj._get_address() != tracker.S.binder.cdo_addr:
        return None
    if tracker.S.ok:
        tracker.record(args)
    return Block


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    now = time.perf_counter()
    gap, S.last_call = now - S.last_call, now
    if gap < FRAME_GAP_S and now - S.ticked_at < MAX_WAIT_S:
        return
    S.ticked_at = now
    try:
        _tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Carnage Meter] {ex!r}")
        logging.exception(ex)


def _tick() -> None:
    if not S.registered:
        S.registered = page.register(GAME_DIR)  # the Mod Menu may load after us
    if not tracker.S.ok and not S.setup_failed and share.role() != "client":
        S.setup_failed = not tracker.setup()
    _check_session()
    if share.role() != "client":
        tracker.tick(time.monotonic())
    page.tick()
    if S.pending_open and time.monotonic() > S.pending_open:
        S.pending_open = 0.0
        page.request_open()
    keys = page._menu_module("keys")  # noqa: SLF001
    if keys is not None and get_pc(possibly_loading=True) is not None and open_key.value:
        down = keys.key_down(open_key.value)
        if down and not S.key_was_down:
            page.toggle()
        S.key_was_down = down


SESSION_CHECK_S = 1.0


def _check_session() -> None:
    """Back from the main menu (or on another character) is a new game session: a clean board."""
    now = time.monotonic()
    if now - S.session_checked < SESSION_CHECK_S:
        return
    S.session_checked = now
    session = tracker.game_session()
    if session is None:
        return
    if S.session is not None and session != S.session:
        tracker.new_session()
        share.forget()
        page.S.view = {"scope": "fight"}
        page.changed()
        log("New game session: the board starts over.")
    S.session = session


def _enable() -> None:
    S.key_was_down = True
    S.setup_failed = False
    tracker.FIGHT_GAP_S = float(fight_gap.value)
    naming.start_check(GAME_DIR)
    share.install(lambda: mod.is_enabled)
    _sync_popup(bool(report_option.value))
    S.registered = page.register(GAME_DIR)
    if not S.registered:
        log("Waiting for BL4 Mod Menu: the Carnage Report lives in its page.")


def _disable() -> None:
    _sync_popup(False)
    tracker.unbind_all()
    tracker.S.ok = False
    page.unregister()
    share.uninstall()
    S.registered = False


# endregion

mod = build_mod(
    options=[open_button, open_key, report_option, fight_gap, reset_button],
    keybinds=[],
    hooks=[caused_damage, tick],
    commands=[carnage_command],
    on_enable=_enable,
    on_disable=_disable,
)
