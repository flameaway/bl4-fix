"""
The link between the Carnage Report screen (ui/meter.js, in the game's menu page) and Python.

Same arrangement as the Kairosdex (bl4_weapondex/screen.py): the screen is a BL4 Mod Menu
extension. The Mod Menu loads ui/meter.js into the page and serves state() to it; the page's
clicks come back through act(). The open key posts Esc (the page only runs while a game menu
shows), and the screen covers the pause menu; Close shuts the pause menu again.

state() is only built when the page polls (about 4 times a second with the screen open, once a
second otherwise), never per frame.

When a boss dies, the report goes to the HUD pop-up (popup_page/, drawn in the HUD page, nothing
to click) and the Carnage Report switches to its Boss Report tab for the next time it's opened.
"""

from __future__ import annotations

import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging

from . import popup_page, share, tracker

KEY = "carnage"
UI_DIR = "OakGame/Content/uiresources/bl4carnage"
SCRIPT_URL = "coui://uiresources/bl4carnage/meter.js"
UI_FILES = ("meter.js", "meter.css")
PUBLISH_S = 1.0
OPEN_MAX_S = 10.0
OPEN_WAIT_S = 4.0
OPEN_SETTLE_S = 1.5
MENU_POLL_S = 1.5
OPEN_POLL_S = 0.6
# After a boss dies: let the death play out a moment, then show the pop-up
REPORT_DELAY_S = 1.5
POPUP_FILES = ("popup.js", "popup_main.js", "popup.css")
POPUP_SOURCES = 3  # each player's top damage sources on the pop-up


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012
    open_request: dict[str, Any] = {"id": 0, "own": False}  # noqa: RUF012
    open_requested_at = -1e9
    open_pending = False
    open_menus_closed = 0
    waiting_until = 0.0
    close_request = 0
    toast: dict[str, Any] = {"id": 0, "text": ""}  # noqa: RUF012
    view: dict[str, Any] = {"scope": "fight"}  # noqa: RUF012
    key_name = lambda: "F9"  # noqa: E731 - set by __init__ (the open key's current binding)
    report_popup = lambda: True  # noqa: E731 - set by __init__ (the "Boss fight pop-up" option)
    report_seen = 0  # the last report id shown or skipped
    report_due = 0.0


def _menu_module(name: str) -> Any:
    return sys.modules.get(f"bl4_mod_menu.{name}")


def available() -> bool:
    web = _menu_module("web")
    return _menu_module("extensions") is not None and web is not None and web.Link.server is not None


def say(text: str) -> None:
    S.toast = {"id": S.toast["id"] + 1, "text": text}
    logging.info(f"[Carnage Meter] {text}")
    changed()


def changed() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.changed(KEY)


# region Page link


def state() -> dict[str, Any]:
    web = _menu_module("web")
    role = share.role()
    if role == "client":
        board = share.client_board(screen_open())
        host = share.host_status()
    else:
        board = tracker.board()
        host = "self"
    return {
        "report": _report(),
        "open": {**S.open_request, "fresh": _open_pending()},
        "close": S.close_request,
        "menus_closed": getattr(getattr(web, "Link", None), "menus_closed", 0),
        "toast": S.toast,
        "role": role,
        "host": host,
        "stale": role == "client" and share.stale(),
        "board": board,
        "view": S.view,
        "key": S.key_name(),
    }


def act(action: dict[str, Any]) -> None:
    match action.get("do"):
        case "opened":
            if action.get("id") == S.open_request["id"]:
                S.open_pending = False
        case "reset":
            if share.role() == "client":
                say("Only the host can reset the board.")
            else:
                tracker.reset()
                say("Board reset. Go make some carnage.")
        case "view":
            scope = action.get("scope")
            if scope in ("boss", "fight", "session"):
                S.view = {"scope": scope}
        case _:
            pass


def register(game_dir: Path) -> bool:
    S.game_dir = game_dir
    exts = _menu_module("extensions")
    if exts is None:
        return False
    publish(force=True)
    exts.register(KEY, SCRIPT_URL, state, act)
    return True


def unregister() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.unregister(KEY)


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4carnage/, touching only files that changed."""
    now = time.monotonic()
    if S.game_dir is None or (not force and now - S.published_at < PUBLISH_S):
        return
    S.published_at = now
    root = resources.files(__package__)
    from_source = isinstance(root, Path)
    if not force and not from_source:
        return  # a packed .sdkmod can't change while the game runs: copied once, at register
    for name in UI_FILES:
        source = root / "ui" / name
        try:
            if from_source:
                st = source.stat()
                stamp = (st.st_size, st.st_mtime_ns)
                if not force and S.published.get(name) == stamp:
                    continue
                S.published[name] = stamp
            content = source.read_bytes()
            target = S.game_dir / UI_DIR / name
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(target)
        except OSError as ex:
            S.published.pop(name, None)
            logging.dev_warning(f"[Carnage Meter] couldn't publish {name}: {ex}")
    if from_source and not force and S.report_popup():
        # Dev: the pop-up's files (popup_page republishes them with a new stamp, the HUD reloads them)
        stamps = []
        for name in POPUP_FILES:
            try:
                st = (root / "popup_page" / name).stat()
                stamps.append((st.st_size, st.st_mtime_ns))
            except OSError:
                return
        if S.published.get("popup") != tuple(stamps):
            S.published["popup"] = tuple(stamps)
            try:
                popup_page.write_loose_files(S.game_dir)
            except OSError as ex:
                logging.dev_warning(f"[Carnage Meter] couldn't publish the pop-up: {ex}")


# endregion
# region Opening


def screen_open() -> bool:
    exts = _menu_module("extensions")
    return exts is not None and exts.page_open(KEY, OPEN_POLL_S)


def _menus_closed() -> int:
    return getattr(getattr(_menu_module("web"), "Link", None), "menus_closed", 0)


def _open_pending() -> bool:
    if not S.open_pending:
        return False
    if time.monotonic() - S.open_requested_at > OPEN_MAX_S or _menus_closed() != S.open_menus_closed:
        S.open_pending = False
    return S.open_pending


def _menu_showing(pc: Any) -> bool:
    exts = _menu_module("extensions")
    return bool(pc.bShowMouseCursor) and exts is not None and exts.polling(KEY, MENU_POLL_S)


def _report() -> dict[str, Any] | None:
    """The last boss report: our own (solo / host) or the one the host sent us (client)."""
    return share.S.report if share.role() == "client" else tracker.S.report


def request_open(scope: str | None = None) -> None:
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        logging.info("[Carnage Meter] The Carnage Report opens in game, not at the title screen.")
        return
    if not available():
        logging.error("[Carnage Meter] The Carnage Report needs the BL4 Mod Menu mod (it shows in the same page).")
        return
    own = not _menu_showing(pc)
    if own and (menu := _menu_module("menu")) is not None:
        menu.press_escape()
    S.open_request = {"id": S.open_request["id"] + 1, "own": own, "scope": scope}
    S.open_requested_at = time.monotonic()
    S.open_pending = True
    S.open_menus_closed = _menus_closed()
    S.waiting_until = S.open_requested_at + OPEN_WAIT_S
    changed()


def request_close() -> None:
    S.close_request += 1
    changed()


def toggle() -> None:
    pc = get_pc(possibly_loading=True)
    menu = pc is not None and _menu_showing(pc)
    showing = screen_open()
    if (not showing and S.open_request.get("own") and S.open_pending
            and time.monotonic() - S.open_requested_at < OPEN_SETTLE_S):  # fmt: skip
        return  # our Esc is still opening the pause menu: another Esc would close it again
    if pc is not None and menu and showing:
        request_close()
    else:
        request_open()


def popup_event(report: dict[str, Any]) -> dict[str, Any]:
    """What the HUD pop-up shows for a boss report: players by damage to the boss, their top sources."""
    players = []
    for p in report.get("players", []):
        r = p.get("report") or {}
        total = max(1, int(r.get("total", 0)))
        tops = [
            {
                "name": s.get("name", ""),
                # Colour: an item's rarity, a damage-over-time element, else the kind (skill...)
                "look": s.get("rarity") or (f"elem_{s['element']}" if s.get("element") else s.get("kind", "")),
                "share": round(100 * int(s.get("dmg", 0)) / total),
            }
            for s in (r.get("top") or [])[:POPUP_SOURCES]
        ]
        players.append({
            "name": p.get("name", ""),
            "local": bool(p.get("local")),
            "portrait": p.get("portrait", ""),
            "dmg": int(r.get("boss", 0)),
            "tops": tops,
        })  # fmt: skip
    players.sort(key=lambda x: -x["dmg"])
    return {
        "kind": "boss",
        "id": str(report["id"]),
        "boss": report.get("boss", ""),
        "picture": report.get("picture", ""),
        "seconds": report.get("seconds", 0),
        "team": sum(x["dmg"] for x in players),
        "players": [x for x in players if x["dmg"] > 0] or players,
        "key": S.key_name(),
    }


def _fight_no() -> int | None:
    """The fight under way (the host's, as a client: from its last board)."""
    if share.role() != "client":
        return tracker.S.fight_no
    board = share.S.board
    return board.get("fight", {}).get("no") if board else None


def _back_to_fight(report: dict[str, Any] | None) -> None:
    """Once a newer fight than the boss report's has begun, the screen opens on This Fight again."""
    if S.view.get("scope") != "boss" or report is None or report.get("fight_no") is None:
        return
    current = _fight_no()
    if current is not None and current != report["fight_no"]:
        S.view = {"scope": "fight"}
        changed()


def _tick_report() -> None:
    """A boss died: tell our friends, then (REPORT_DELAY_S later) the HUD pop-up and the Boss Report tab."""
    report = _report()
    if report is None or report["id"] == S.report_seen:
        _back_to_fight(report)
        return
    if not S.report_due:
        S.report_due = time.monotonic() + (REPORT_DELAY_S if share.role() != "client" else 0.2)
        if share.role() == "host":
            share.push_report(report)
        return
    if time.monotonic() < S.report_due:
        return
    S.report_seen, S.report_due = report["id"], 0.0
    S.view = {"scope": "boss"}  # F9 opens on it
    changed()
    if S.report_popup() and S.game_dir is not None:
        try:
            popup_page.post(S.game_dir, popup_event(report))
        except OSError as ex:
            logging.dev_warning(f"[Carnage Meter] pop-up: {ex}")


def tick() -> None:
    publish()
    _tick_report()
    if S.waiting_until and time.monotonic() > S.waiting_until:
        S.waiting_until = 0.0
        if not screen_open():
            logging.error("[Carnage Meter] The screen didn't open. Is BL4 Mod Menu enabled (it adds the screen to the pause menu)?")
    elif S.waiting_until and screen_open():
        S.waiting_until = 0.0


# endregion
