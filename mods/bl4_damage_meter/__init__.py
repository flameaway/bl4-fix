"""
Carnage Meter for Borderlands 4: who did the damage, and with what.

Open the Carnage Report (F9, the pause menu's Carnage Report entry, or `carnage`) for every
player's damage in this fight and this session: total, damage per second, kills, crit damage,
biggest hit, share of the team's damage, and each player's top damage sources, named the way the
game names them: the gun ("Hellwalker"), the grenade, the shield, an element's damage over time
("Burning", "Bleed"...), melee, ground slams, skills, companions. A boss kill shows a results pop-up
on the HUD (popup_page, run in the HUD page by the BL4 UI Link) and fills the report's Boss
Report tab.

How: the host's damage pipeline tells each character about every hit it deals
(`DamageCauserData.OnCausedAnyDamage`); this mod listens on that (native.py) and counts
(tracker.py). In co-op the host counts everyone; a friend running the mod sees the host's board
(share.py, through BL4 Direct). The screen is HTML in the game's menu page, run there by the BL4
UI Link (page.py, ui/meter.js, uilink/; no Mod Menu needed).
"""

from __future__ import annotations

import argparse
import threading
import time
import traceback
from typing import Any

from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, KeybindOption, SliderOption, build_mod, command, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import frametick, naming, native, page, popup_page, share, tracker, uilink

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

GAME_DIR = SETTINGS_DIR.parent.parent


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
        " Boss Report tab either way."
    ),
    on_change=_popup_changed,
)


_popup_lock = threading.Lock()
_popup = {"wanted": False, "queued": False, "done": None}


# The pop-up's loader, run in the HUD page by the BL4 UI Link while the option is on and the
# game's loot feed page is the one it was written for (popup_page.page_current)
POPUP_KEY = "carnagepopup"
POPUP_SCRIPT = "coui://uiresources/bl4carnage/popup.js"


def _link_popup(on: bool) -> None:
    if on:
        uilink.register(POPUP_KEY, POPUP_SCRIPT, dict, lambda _action: None, views=(uilink.HUD,), mod="Carnage Meter")
    else:
        uilink.unregister(POPUP_KEY)
        uilink.stop()  # the link's server goes once no script of any mod is left


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
            _link_popup(wanted and popup_page.page_current is True)
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
    setup_failed = False
    session: tuple[int, int, int, int] | None = None  # the game session the board belongs to
    session_checked = 0.0
    slow_at = 0.0  # the next _slow()
    role = "solo"  # share.role() as of the last _slow()


@hook(native.FUNC_PATH, Type.PRE)
def caused_damage(obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> Any:
    # Our binding calls this on the class's default object; anything else is the game's own (an
    # augment script with that event), which we leave alone
    if obj._get_address() != tracker.S.binder.cdo_addr:
        return None
    if tracker.S.ok:
        tracker.record(args)
    return Block


@frametick.tick("bl4_damage_meter")  # once per engine frame (the shared Frame Tick, frametick/)
def tick() -> None:
    try:
        _tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Carnage Meter] {ex!r}")
        logging.error("".join(traceback.format_exception(ex)))


# Everything but the link, queued hits and the open key is looked at this often (2026-10-29: it all
# ran every frame, ~0.1 ms a frame in play for a few clock compares and calls)
SLOW_S = 0.25


def _tick() -> None:
    uilink.tick()  # every frame: the page's polls wait for it (works once a frame across our mods)
    now = time.monotonic()
    if now >= S.slow_at:
        S.slow_at = now + SLOW_S
        _slow(now)
    t = tracker.S
    # Hits queued by the hook, or the next player / boss look due (tracker.tick sets next_at)
    if t.ok and (t.pending or now >= t.next_at) and S.role != "client":
        tracker.tick(now)
    # The key snapshot first (a dict read); the controller only on a press (get_pc is a chain of
    # property reads: not worth doing every frame to learn that nothing was pressed)
    key = open_key.value
    down = bool(key) and uilink.key_down(key)
    if down and not S.key_was_down and get_pc(possibly_loading=True) is not None:
        page.toggle()
    S.key_was_down = down


def _slow(now: float) -> None:
    """Every SLOW_S: the co-op role, setup, the session, the page's housekeeping."""
    role = S.role = share.role()  # BL4 Direct caches it 0.25 s anyway
    if not tracker.S.ok and not S.setup_failed and role != "client":
        S.setup_failed = not tracker.setup()
    _check_session()
    page.tick(role)
    if S.pending_open and now > S.pending_open:
        S.pending_open = 0.0
        page.request_open()


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
    S.slow_at = 0.0
    S.setup_failed = False
    tracker.FIGHT_GAP_S = float(fight_gap.value)
    naming.start_check(GAME_DIR)
    share.install(lambda: mod.is_enabled)
    _sync_popup(bool(report_option.value))
    page.register(GAME_DIR)


def _disable() -> None:
    _link_popup(False)  # at once: the worker below only removes files
    _sync_popup(False)
    tracker.unbind_all()
    tracker.S.ok = False
    page.unregister()
    share.uninstall()


# endregion

mod = build_mod(
    options=[open_button, open_key, report_option, fight_gap, reset_button],
    keybinds=[],
    hooks=[caused_damage, tick],
    commands=[carnage_command],
    on_enable=_enable,
    on_disable=_disable,
)
