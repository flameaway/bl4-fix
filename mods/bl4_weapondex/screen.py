"""
The link between the Kairosdex screen (ui/dex.js, running in the game's menu page) and Python.

The screen lives in the same page as the BL4 Mod Menu, the game's menu page, and borrows its
link: it registers with bl4_mod_menu.extensions, which loads ui/dex.js into the page and serves
state() to it. Actions from the page come back through act(). So this needs the Mod Menu.

The page fetches the dex itself (dex.json, a loose file next to dex.js); the state is only what
changes: which bosses are beaten on this profile, the dev unlock, the search text, open/close
requests, and the screen's own view (tab, picks, filters, scroll), which the page sends here so
it outlives the page and the game (settings/weapondex_view.json).

Search typing: no key events reach page scripts, so while the search box is active a low-level
keyboard hook (keyboard.py) reads the keys and keeps them from the game's menus and from every
mod's hotkeys; the text goes to the page in the state.

Waypoint / Travel buttons on a boss (travel.py): the page asks, the next frame does it, and the
outcome goes back in the state's "trip" (the page closes the menus once the player is there).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging

from . import data, keyboard, owned, picture, progress, roll, savegame, thumbs, travel

KEY = "weapondex"
VIEW_FILE = SETTINGS_DIR / "weapondex_view.json"
MAX_VIEW = 4000  # characters of JSON: the view is a handful of fields
UI_DIR = "OakGame/Content/uiresources/bl4weapondex"
SCRIPT_URL = "coui://uiresources/bl4weapondex/dex.js"
UI_FILES = ("dex.js", "dex.css", "dex.json", "travel.json")
ROOT_FILES = ("dex.json", "travel.json")  # next to __init__.py, not in ui/
PUBLISH_S = 1.0
# An open request stays good until the page says it opened it, the game's menus close, or this
# long: the page may wake (its menu opening) only a moment later, or reload its scripts first
OPEN_MAX_S = 10.0
OPEN_WAIT_S = 4.0
OPEN_SETTLE_S = 1.5  # after F8 opened the pause menu, more presses are ignored this long (until it shows)
# The page polls about every second while our screen is closed and every 0.1 s while it's open
MENU_POLL_S = 1.5
OPEN_POLL_S = 0.5
MAX_SEARCH = 40
SAVE_ONLY = 1  # "defeated" time of a boss only the game's save shows (the page says "from your save")
# The player's UVH level (the drop chances depend on it): read at most this often
UVH_S = 1.0
UVH_ATTRIBUTE = "vault_hunter_level"
MISSING = -1.0e30


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    open_request: dict[str, Any] = {"id": 0, "own": False, "boss": None}  # noqa: RUF012
    open_requested_at = -1e9
    open_pending = False  # until the page acknowledges it (do: "opened")
    open_menus_closed = 0  # the Mod Menu's menus_closed count when it was made
    waiting_until = 0.0
    close_request = 0
    search = ""
    typing: keyboard.Typing | None = None  # while the search box is being typed in
    toast: dict[str, Any] = {"id": 0, "text": ""}  # noqa: RUF012
    view: dict[str, Any] | None = None  # the page's view, as it last sent it
    hover: str | None = None  # the item the mouse is over
    shown: str | None = None  # the item the details show
    uvh: int | None = None  # the player's UVH level (0 = not in UVH), None = unknown
    uvh_at = -1e9
    # A Waypoint / Travel click, done on the next frame: {"do", "boss"}
    trip_request: dict[str, Any] | None = None
    # What came of the last one: {"id", "boss", "do", "state", "text"}; state is "pinned",
    # "arrived", "travelling" (another map), "moving" (the host is moving us) or "refused"
    trip: dict[str, Any] = {"id": 0}  # noqa: RUF012
    # Set by __init__: whether the dev "unlock everything" option is on
    unlocked: Callable[[], bool] = lambda: False  # noqa: E731
    # Set by __init__: whether the "Obtained items only" option is on
    obtained: Callable[[], bool] = lambda: True  # noqa: E731
    # Set by __init__: whether hovering an Items list entry shows its card (boss drop rows always do)
    list_hover: Callable[[], bool] = lambda: False  # noqa: E731


def _menu_module(name: str) -> Any:
    return sys.modules.get(f"bl4_mod_menu.{name}")


def available() -> bool:
    """The Mod Menu is loaded and its link to the page is running."""
    web = _menu_module("web")
    return _menu_module("extensions") is not None and web is not None and web.Link.server is not None


def say(text: str) -> None:
    S.toast = {"id": S.toast["id"] + 1, "text": text}
    logging.info(f"[Kairosdex] {text}")
    _changed()


def _changed() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.changed(KEY)


# region Page link


def state() -> dict[str, Any]:
    web = _menu_module("web")
    return {
        "open": {**S.open_request, "fresh": _open_pending()},
        "close": S.close_request,
        # Bumped by the Mod Menu each time the game's menus close: the page may sleep through
        # the moment they're closed, but sees this change when it wakes
        "menus_closed": getattr(getattr(web, "Link", None), "menus_closed", 0),
        # Wins seen by the mod carry their time; ones only the game's save shows are SAVE_ONLY
        "defeated": {**dict.fromkeys(savegame.beaten(), SAVE_ONLY), **progress.defeated_anywhere()},
        "character": progress.character() is not None,
        "unlock": S.unlocked(),
        "search": S.search,
        "typing": S.typing is not None,
        "outdated": S.game_dir is not None and not data.data_current(S.game_dir),
        "toast": S.toast,
        "view": _view(),
        # Rolls of the item the page shows (roll.py): its card lines and status
        "roll": roll.state(_shown_item()),
        # Its effect texts' numbers that only depend on the player's level
        "fills": roll.level_fills(_shown_item()),
        # The item the mouse is over in the list (its card shows beside it)
        "hover": roll.hover_state(S.hover),
        # Legendaries the player has had (owned.py): the "Obtained items only" option shows only these
        "obtained": S.obtained(),
        "owned": sorted(owned.items()),
        "owned_v": owned.S.version,
        # "Item card on hover in the item list" option (off: only a boss's drop rows show a hover card)
        "list_hover": S.list_hover(),
        # The drop chances shown are at this UVH level
        "uvh": _uvh(),
        "trip": S.trip,
    }


def act(action: dict[str, Any]) -> None:
    match action.get("do"):
        case "opened":
            if action.get("id") == S.open_request["id"]:
                S.open_pending = False
            # What's in the backpack now (the saves can be behind): read over the next frames
            owned.scan_backpack(get_pc(possibly_loading=True))
        case "type":
            if action.get("on"):
                start_typing()
            else:
                stop_typing()
        case "search":
            S.search = str(action.get("text") or "")[:MAX_SEARCH]
            if S.typing is not None:
                S.typing.text = S.search
        case "roll":
            if isinstance(action.get("item"), str):
                roll.request(action["item"], int(action.get("count") or 1))
        case "roll_clear":
            if isinstance(action.get("item"), str):
                roll.clear(action["item"])
        case "show":
            # The item the details show (not always the pick: a filter can hide that)
            item = action.get("item")
            S.shown = item if isinstance(item, str) and item in data.dex()["items"] else None
            # Opening an item rolls one at once
            if S.shown and not roll.S.results.get(S.shown) and not roll.S.status.get(S.shown):
                roll.request(S.shown, 1)
        case "hover":
            item = action.get("item")
            S.hover = item if isinstance(item, str) and item in data.dex()["items"] else None
            roll.drop_hover_requests(S.hover)
            # Rolled once on its first hover (no picture), the same roll shown on later hovers
            if S.hover and not roll.S.results.get(S.hover) and not roll.S.status.get(S.hover):
                roll.request(S.hover, 1, picture=False)
        case "waypoint" | "travel":
            if isinstance(action.get("boss"), str):
                S.trip_request = {"do": action["do"], "boss": action["boss"]}
        case "view":
            view = action.get("view")
            if isinstance(view, dict) and view != S.view:
                text = json.dumps(view)
                if len(text) <= MAX_VIEW:
                    S.view = view
                    try:
                        VIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
                        tmp = VIEW_FILE.with_name(VIEW_FILE.name + ".tmp")
                        tmp.write_text(text, encoding="utf-8")
                        tmp.replace(VIEW_FILE)
                    except OSError:
                        pass
        case _:
            pass


def _uvh() -> int | None:
    """The player's UVH level, read at most every UVH_S (the last one while there's no pawn)."""
    now = time.monotonic()
    if now - S.uvh_at < UVH_S:
        return S.uvh
    S.uvh_at = now
    pc = get_pc(possibly_loading=True)
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return S.uvh
    try:
        lib = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject
        value = float(lib.GetValueOfAttribute(UVH_ATTRIBUTE, pawn, MISSING))
    except Exception:  # noqa: BLE001
        return S.uvh
    S.uvh = None if value == MISSING or value != value else max(0, int(round(value)))  # noqa: PLR0124
    return S.uvh


def _shown_item() -> str | None:
    return S.shown or (S.view or {}).get("item")


def _view() -> dict[str, Any] | None:
    if S.view is None:
        try:
            loaded = json.loads(VIEW_FILE.read_text(encoding="utf-8"))
            S.view = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            S.view = {}
    return S.view or None


def register(game_dir: Path) -> bool:
    S.game_dir = game_dir
    exts = _menu_module("extensions")
    if exts is None:
        return False
    publish(force=True)
    _start_small_backfill(game_dir)
    exts.register(KEY, SCRIPT_URL, state, act)
    return True


SMALL_THREAD = "weapondex-small"


def _start_small_backfill(game_dir: Path) -> None:
    """Small copies of pictures taken before they existed (picture.backfill), once, on a worker."""
    if any(t.name == SMALL_THREAD for t in threading.enumerate()):
        return  # still running from before a hot reload

    def run() -> None:
        try:
            made = picture.backfill([game_dir / UI_DIR / "bosses", game_dir / UI_DIR / "items"])
            if made:
                logging.info(f"[Kairosdex] made {made} small pictures")
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Kairosdex] small pictures: {ex!r}")

    threading.Thread(target=run, name=SMALL_THREAD, daemon=True).start()


def unregister() -> None:
    stop_typing()
    S.trip_request = None
    travel.cancel()
    roll.stop()
    thumbs.stop("stopped (mod disabled)")
    if (exts := _menu_module("extensions")) is not None:
        exts.unregister(KEY)


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4weapondex/, touching only files that changed."""
    now = time.monotonic()
    if S.game_dir is None or (not force and now - S.published_at < PUBLISH_S):
        return
    S.published_at = now
    root = resources.files(__package__)
    from_source = isinstance(root, Path)
    if not force and not from_source:
        return  # a packed .sdkmod can't change while the game runs: copied once, at register
    for name in UI_FILES:
        source = root / name if name in ROOT_FILES else root / "ui" / name
        try:
            if from_source:
                # Dev: re-read only a file whose size or date moved (reading ~600 KB every second
                # cost ~3 ms on the game thread)
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
            S.published.pop(name, None)  # try again next time
            logging.dev_warning(f"[Kairosdex] couldn't publish {name}: {ex}")


# endregion
# region Opening


def screen_open() -> bool:
    exts = _menu_module("extensions")
    return exts is not None and exts.page_open(KEY)


def _menus_closed() -> int:
    return getattr(getattr(_menu_module("web"), "Link", None), "menus_closed", 0)


def _open_pending() -> bool:
    if not S.open_pending:
        return False
    if time.monotonic() - S.open_requested_at > OPEN_MAX_S or _menus_closed() != S.open_menus_closed:
        # Too old, or the game's menus closed before the page got to it: don't open it later
        S.open_pending = False
    return S.open_pending


def _menu_showing(pc: Any) -> bool:
    """
    One of the game's menus is up. bShowMouseCursor alone isn't reliable, so the page (which only
    runs while a menu shows) must also have polled just now.
    """
    exts = _menu_module("extensions")
    if not pc.bShowMouseCursor or exts is None:
        return False
    try:
        return exts.polling(KEY, MENU_POLL_S)
    except TypeError:  # a Mod Menu from before polling() took a time
        return True


def _showing_now() -> bool:
    """Our screen is up right now (screen_open() can lag a few seconds behind a close)."""
    exts = _menu_module("extensions")
    if exts is None:
        return False
    try:
        return exts.page_open(KEY, OPEN_POLL_S)
    except TypeError:
        return exts.page_open(KEY)


def request_open(boss: str | None = None) -> None:
    """Opens the Kairosdex screen (in the pause menu), on a boss's page if one is given."""
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        logging.info("[Kairosdex] The Kairosdex opens in game, not at the title screen.")
        return
    if not available():
        logging.error("[Kairosdex] The Kairosdex needs the BL4 Mod Menu mod (it shows in the same page).")
        return
    own = not _menu_showing(pc)
    if own and (menu := _menu_module("menu")) is not None:
        # In gameplay: open the pause menu, whose page holds our screen (the page only runs while
        # one of the game's menus shows)
        menu.press_escape()
    S.open_request = {"id": S.open_request["id"] + 1, "own": own, "boss": boss}
    S.open_requested_at = time.monotonic()
    S.open_pending = True
    S.open_menus_closed = _menus_closed()
    S.waiting_until = S.open_requested_at + OPEN_WAIT_S
    _changed()


def request_close() -> None:
    S.close_request += 1
    _changed()


def toggle() -> None:
    pc = get_pc(possibly_loading=True)
    menu = pc is not None and _menu_showing(pc)
    showing = _showing_now()
    _trace("toggle", menu=menu, showing=showing, cursor=bool(pc and pc.bShowMouseCursor))
    if (not showing and S.open_request.get("own") and S.open_pending
            and time.monotonic() - S.open_requested_at < OPEN_SETTLE_S):  # fmt: skip
        # Our Esc is still opening the pause menu: another Esc now would close it again (pressing
        # F8 twice while it opened left the game as it was: "F8 works half the time", traced)
        _Trace.entries[-1]["ignored"] = True
        return
    if pc is not None and menu and showing:
        request_close()
    else:
        request_open()


# Dev trace of open key presses and what came of them (F8 "works half the time", 2026-10-24)
class _Trace:
    entries: list[dict[str, Any]] = []  # noqa: RUF012
    key_was = False
    watching: dict[str, Any] | None = None


def _trace(what: str, **info: Any) -> None:
    entry = {"t": round(time.monotonic(), 3), "what": what, **info}
    _Trace.entries = [*_Trace.entries, entry][-60:]
    if what == "toggle":
        _Trace.watching = entry


def _trace_tick() -> None:
    # (no key read of its own: a ctypes call per frame is one more point where a waking worker
    # thread takes the GIL; toggle() is traced instead)
    w = _Trace.watching
    if w is None:
        return
    age = time.monotonic() - w["t"]
    showing = _showing_now()
    if not w["showing"] and showing:
        w["outcome"] = f"shown after {round(age * 1000)} ms"
        _Trace.watching = None
    elif w["showing"] and not showing:
        w["outcome"] = f"closed after {round(age * 1000)} ms"
        _Trace.watching = None
    elif age > 4.0:
        pc = get_pc(possibly_loading=True)
        exts = _menu_module("extensions")
        w["outcome"] = "nothing after 4 s"
        w["after"] = {"cursor": bool(pc and pc.bShowMouseCursor), "pending": S.open_pending,
                      "polling": bool(exts and exts.polling(KEY, MENU_POLL_S)), "menus_closed": _menus_closed(),
                      "request": dict(S.open_request)}  # fmt: skip
        _Trace.watching = None


# endregion
# region Per frame


def start_typing() -> None:
    stop_typing()
    # Starts its hook on its own thread; a hook Windows refused shows up in _poll_typing
    S.typing = keyboard.Typing(S.search, MAX_SEARCH)
    _changed()


def stop_typing() -> None:
    typing, S.typing = S.typing, None
    if typing is None:
        return
    typing.stop()
    S.search = typing.text
    _changed()


def _poll_typing() -> None:
    typing = S.typing
    if typing is None:
        return
    if typing.failed:
        stop_typing()
        say("Couldn't read the keyboard for typing.")
        return
    # The screen closed (or the menus did): don't keep swallowing keys
    if not screen_open():
        stop_typing()
        return
    if typing.poll():
        S.search = typing.text
        _changed()
    if typing.done is not None:  # Enter, Esc or Tab
        stop_typing()


def _trip_result(boss_id: str, do: str, state: str, text: str) -> None:
    S.trip = {"id": S.trip["id"] + 1, "boss": boss_id, "do": do, "state": state, "text": text}
    if text:
        say(text)
    else:
        _changed()


def _do_trip() -> None:
    """A Waypoint / Travel click from the page (one frame later, not inside the page's request)."""
    request, S.trip_request = S.trip_request, None
    boss_id, do = request["boss"], request["do"]
    target = travel.boss(boss_id)
    if target is None:
        _trip_result(boss_id, do, "refused", "This boss has no Big Encore machine to go to.")
        return
    try:
        if do == "waypoint":
            travel.pin(target)
            where = " (shows on its map)" if travel.elsewhere(target) else ""
            _trip_result(boss_id, do, "pinned", f"Waypoint set on {target['boss']}'s Big Encore machine{where}.")
            return
        there = travel.start(target)
    except travel.Refused as ex:
        _trip_result(boss_id, do, "refused", str(ex))
        return
    if there is True:
        _trip_result(boss_id, do, "arrived", "")
    elif there is False:
        _trip_result(boss_id, do, "travelling", f"Travelling to {target['boss']} ({target['place']})...")
    else:
        _trip_result(boss_id, do, "moving", f"The host is moving you to {target['boss']}...")


def _tick_trips() -> None:
    travel.tick()
    if (result := travel.take_hop_result()) is not None:
        hopped, reply = result
        if reply.get("ok"):
            _trip_result(hopped["id"], "travel", "arrived", "")
        elif reply.get("error") == "no room":
            _trip_result(hopped["id"], "travel", "refused",
                         f"No room to stand next to {hopped['boss']}'s Big Encore machine; follow the waypoint.")  # fmt: skip
        else:
            _trip_result(hopped["id"], "travel", "refused", f"The host couldn't move you ({reply.get('error')}); follow the waypoint.")
    if S.trip_request is not None:
        try:
            _do_trip()
        except Exception as ex:  # noqa: BLE001 - a broken trip mustn't break the tick
            logging.error(f"[Kairosdex] trip: {ex!r}")


def tick() -> None:
    _trace_tick()
    publish()
    _poll_typing()
    _tick_trips()
    roll.S.on_change = _changed
    roll.tick()
    thumbs.tick()
    owned.tick()
    if S.waiting_until and time.monotonic() > S.waiting_until:
        S.waiting_until = 0.0
        if not screen_open():
            logging.error("[Kairosdex] The screen didn't open. Is BL4 Mod Menu enabled (it adds the screen to the pause menu)?")
    elif S.waiting_until and screen_open():
        S.waiting_until = 0.0


# endregion
