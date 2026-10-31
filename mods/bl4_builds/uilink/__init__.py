"""
BL4 UI Link: runs our mods' page scripts in the game's HTML pages, with no Mod Menu needed and
without replacing any of the game's pages.

Source of truth: shared/uilink/ in the repo. tools/sync_shared.py copies it, byte for byte, into
every mod that draws in the game's pages (`<mod>/uilink/`), so each mod works on its own. Never
edit a mod's copy; edit shared/uilink/, bump VERSION, run the sync.

How it gets into the pages
--------------------------
Every game screen (HUD, menus, title screen) is `ingame.html?view=...`, and its last script is the
game's `_shared/js/debug.js` (console helpers, unchanged since launch). Our pak (PAK_NAME) holds
that file plus one line that loads `uiresources/bl4link/boot.js`, a loose file. Nothing else in the
pak: other mods that replace pages (pause menu, item card, HUD) don't touch it, so they and ours
both apply. boot.js loads hub.js, which asks this module's server which scripts to run in its view.

The pak is identical whichever mod writes it, and it's only installed while the game's own
debug.js is exactly the copy it was made from (page/vanilla.json, tools/gen_uilink_page.py) and no
other pak in ~mods replaces it. Otherwise the link steps aside (status() says why) and our pages
fall back to the BL4 Mod Menu's link when it's there. Paks only load at startup: a pak created this
session works from the next start (status "next_start"); meanwhile the Mod Menu's link carries the
menu-page scripts if it's installed.

Many copies, one link
---------------------
All our mods run in one Python. The copies share one state object, `sys.modules[HUB]` (a plain
module object): the registered scripts, the server. The newest copy (VERSION) runs
the server ("owner"); an older owner is replaced, registrations kept. Shared fields are only ever
added, never renamed or given a new meaning, so any copy can read what an older one left.

Python side, from a mod:

    from . import uilink
    uilink.register("builds", "coui://uiresources/bl4builds/builds.js", state, act, views=(uilink.MENUS,))
    uilink.tick()         # from the mod's per-frame hook (does work at most once a frame overall)
    uilink.changed("builds"), uilink.polling(key), uilink.page_open(key), uilink.key_down("F8")
    uilink.reachable(key), uilink.display_name("T"), uilink.press_escape(), uilink.menus_closed()
    uilink.pause_menu()   # opens / closes the pause menu through the game's UI state (v9)
    box = uilink.TextBox(text, max_len)   # typing with the game's own text input (v8, see TextBox)
    grab = uilink.KeyGrab(("Escape",))    # keeps keys from the game's menus, counts presses (v8)
    uilink.unregister("builds")   # on disable: the page tears the script down

The page side (ns = bl4linkNs, the same shape as the Mod Menu's window.bl4ModMenu): ns.ext.get /
send / http / teardowns, ns.get, ns.log, ns.wake.hooks, ns.closeGameMenu, ns.templates.button,
ns.dev, ns.view. A script written for the Mod Menu runs here if it starts with
`var ns = (typeof bl4linkNs !== "undefined" && bl4linkNs) || window.bl4ModMenu;`.

Server: 127.0.0.1, random port, a token; its address in uiresources/bl4link/link.json
({servers: [{pid, port, token, dev, v}]}, newest last; with two games on one PC the page asks
each /hello, answered 200 only for a connection from our own process, as the Mod Menu does).

The setup app (installer/core.py PAGE_MODULES) imports this module without the SDK and calls
sync(game_dir), so a fresh install has the pak before its first launch: keep it plain Python 3.11.

Threads: the server's few pooled threads do no game access (keys come from gamekeys since v7,
the game's own input; there was a key thread polling Windows). Everything that
touches the game (state(), act()) runs in tick(), on the game thread.
"""

from __future__ import annotations

import ctypes
import itertools
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import traceback
import types
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import pak

try:
    from unrealsdk.unreal import WeakPointer as _WeakPointer
except ImportError:  # the setup app and the offline tests: no SDK, the PC isn't cached
    _WeakPointer = None

try:
    from unrealsdk import logging
except ImportError:  # the setup app imports this module for sync() and check() (no SDK there)

    class logging:  # noqa: N801
        @staticmethod
        def info(msg: str) -> None:
            print(msg)

        error = warning = dev_warning = info

        @staticmethod
        def exception(ex: BaseException) -> None:
            print(repr(ex))


# Bump on every change to this package: the newest copy loaded runs the link
VERSION = 10
HUB = "_bl4_uilink"
PAK_NAME = "BL4Fix_UILink_4251_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
SHIM = "_shared/js/debug.js"
UI_DIR = UI_PREFIX + "bl4link"
LINK_FILE = "link.json"
UI_FILES = ("boot.js", "hub.js", "text.js")  # text.js: v8 (TextBox)
# Views (the `view=` of ingame.html): the menus page holds the pause menu, inventory, skills, Options
MENUS = "view_status_menus"
HUD = "HUD"
ANY = "*"

ASSETS = resources.files(__name__)
# A source folder (dev junction), not a packed .sdkmod: page files can change while the game runs
DEV = Path(__file__).is_file()

# A poll gets the state built up to this long ago, unless changed() said it moved on since
FRESH_S = 0.25
# How long a poll waits for the game thread's next tick before taking the last state
WAIT_S = 0.2
# A script that polled within this long counts as running (the menu page sleeps while no menu shows)
POLLING_S = 3.0
# The menus page polled within this long: one of the game's menus is up
MENUS_POLL_S = 1.0
# tick() does its work at most this often (several mods call it each frame)
TICK_GAP_S = 0.004
# The Mod Menu fallback (it may load after us, the link may go live) is checked this often, not
# every frame (v4)
FALLBACK_S = 1.0
# While a menu is up, the game's mouse cursor is read this often, not every frame, and the PC it's
# read from is looked up again this often (a weak pointer in between) (v5)
CURSOR_S = 0.05
CURSOR_PC_S = 1.0
# State builds (each mod's state(), then JSON) in one tick stop after this long; the rest are
# built in the next ticks (a poll waits up to WAIT_S). The menus page waking makes every script
# ask at once (v5)
BUILD_BUDGET_S = 0.002
# A state build or action this slow is logged once per script (dev builds only), to find hitches
# that land in whichever mod's tick ran the link (v5)
SLOW_S = 0.004
PUBLISH_S = 1.0
MAX_BODY = 64 * 1024
THREADS = 4
ESC_HOLD_S = 0.08
# Revisions start from the clock, so a page that kept one from an older server can't match ours
_revs = itertools.count(int(time.time() * 1000))
# Identifies this load of this file: a hot reload of the owner's module takes over from itself
_STAMP = secrets.token_hex(4)


def _game_dir() -> Path | None:
    try:
        from mods_base import SETTINGS_DIR  # noqa: PLC0415

        return Path(SETTINGS_DIR).parent.parent
    except Exception:  # noqa: BLE001 - tests set GAME_DIR themselves
        return None


GAME_DIR = _game_dir()


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


# region Shared state


def _hub() -> types.ModuleType:
    """The state every copy shares (created by the first one)."""
    hub = sys.modules.get(HUB)
    if hub is None:
        hub = types.ModuleType(HUB)
        hub.__doc__ = "BL4 UI Link state shared by every mod's copy of uilink (see its docstring)."
        hub.extensions = {}  # key -> dict (see register)
        hub.owner = None  # the copy running the server (a module object)
        hub.owner_stamp = ""
        hub.owner_version = 0
        hub.server = None
        hub.token = ""
        hub.cond = threading.Condition()
        hub.actions = deque(maxlen=128)
        hub.view_polls = {}  # view -> monotonic time of its last poll
        hub.menus_up = False
        hub.menus_closed = 0
        hub.cursor_was = False  # the game showed its mouse cursor last tick (v3)
        hub.esc_up_at = 0.0
        hub.ticked_at = 0.0
        hub.published = {}  # file -> bytes last written
        hub.published_at = 0.0
        # Our pak was in ~mods when the first copy loaded, so the game mounted it this session
        hub.pak_at_start = GAME_DIR is not None and pak_path(GAME_DIR).exists()
        hub.status = {"state": "checking", "detail": ""}
        hub.checked = False
        hub.helper_started = False
        hub.menu_exts = None  # the Mod Menu's extensions module we registered fallbacks with
        hub.menu_registered = set()
        hub.keys_asked = {}  # vk -> when it was last asked about (unused since v7: gamekeys)
        hub.keys_down = frozenset()
        hub.keys_thread = None
        hub.keys_token = None
        hub.link_lock = threading.Lock()
        sys.modules[HUB] = hub
    return hub


def _me() -> types.ModuleType:
    return sys.modules[__name__]


def _elect(hub: types.ModuleType) -> bool:
    """Makes this copy the owner if it's newer (or a hot reload of the owner). Returns whether it owns."""
    if hub.owner is _me() and hub.owner_stamp == _STAMP:
        return True
    reload_of_owner = hub.owner is _me()
    if hub.owner is not None and not reload_of_owner and getattr(hub, "owner_version", 0) >= VERSION:
        return False
    _stop_server(hub)
    hub.owner, hub.owner_stamp, hub.owner_version = _me(), _STAMP, VERSION
    hub.published = {}
    hub.published_sig = {}  # v5: file -> (mtime, size) of the source last published (dev)
    hub.checked = False
    # v5: polls counted by the server, so a tick with nothing asked skips the scripts' loop
    if not hasattr(hub, "asks"):
        hub.asks = 0
    hub.asks_done = -1
    return True


# endregion
# region Registering (any copy)


def register(
    key: str,
    script: str,
    state: Callable[[], Any],
    act: Callable[[dict[str, Any]], None],
    views: tuple[str, ...] = (MENUS,),
    mod: str = "",
) -> None:
    """
    Adds (or replaces, after a hot reload) a page script. `script` is a coui:// URL, `views` the
    `view=` values of the pages it runs in (ANY for every page). state() runs on the game thread
    when a poll asks; act(action) gets the dicts the script sends.
    """
    hub = _hub()
    old = hub.extensions.get(key) or {}
    hub.extensions[key] = {
        "key": key, "script": script, "state": state, "act": act, "views": tuple(views), "mod": mod,
        "snap": (0, b""), "state_json": b"", "built_at": 0.0, "dirty": True,
        "last_poll": old.get("last_poll", 0.0), "page_open": old.get("page_open", False),
        "asked": 0, "answered": 0,
    }  # fmt: skip
    hub.menu_registered.discard(key)  # re-register the fallback with the new callables
    _elect(hub)


def unregister(key: str) -> None:
    hub = _hub()
    hub.extensions.pop(key, None)
    if key in hub.menu_registered:
        hub.menu_registered.discard(key)
        exts = hub.menu_exts
        if exts is not None:
            try:
                exts.unregister(key)
            except Exception:  # noqa: BLE001
                pass


def changed(key: str) -> None:
    """The state changed: the next poll gets a fresh build."""
    hub = _hub()
    ext = hub.extensions.get(key)
    if ext is not None:
        ext["dirty"] = True
    if key in hub.menu_registered and hub.menu_exts is not None:
        try:
            hub.menu_exts.changed(key)
        except Exception:  # noqa: BLE001
            pass


def polling(key: str, within: float = POLLING_S) -> bool:
    """The script is running in a page right now (it polled within this many seconds)."""
    hub = _hub()
    ext = hub.extensions.get(key)
    if ext is not None and time.monotonic() - ext["last_poll"] < within:
        return True
    return _menu_call(hub, key, "polling", within)


def page_open(key: str, within: float = POLLING_S) -> bool:
    """The script last said its screen is showing (only meaningful while polling())."""
    hub = _hub()
    ext = hub.extensions.get(key)
    if ext is not None and ext["page_open"] and time.monotonic() - ext["last_poll"] < within:
        return True
    return _menu_call(hub, key, "page_open", within)


def _menu_call(hub: types.ModuleType, key: str, name: str, within: float) -> bool:
    if key not in hub.menu_registered or hub.menu_exts is None:
        return False
    try:
        return bool(getattr(hub.menu_exts, name)(key, within))
    except Exception:  # noqa: BLE001
        return False


def menus_up() -> bool:
    """One of the game's menus shows (its page polled us lately)."""
    if (link := _menu_link()) is not None:
        return bool(getattr(link, "menus_up", False))
    return _hub().menus_up


def menus_closed() -> int:
    """Counts every time the game's menus close."""
    if (link := _menu_link()) is not None:
        return int(getattr(link, "menus_closed", 0))
    return _hub().menus_closed


def _menu_link() -> Any:
    """While the Mod Menu's link carries our scripts (ours isn't live), its menu signals count (v2)."""
    if live() or not _hub().menu_registered:
        return None
    return getattr(sys.modules.get("bl4_mod_menu.web"), "Link", None)


def reachable(key: str) -> bool:
    """
    The script can show this session: through the link, or through the Mod Menu's link while ours
    waits for the next start (v2).
    """
    hub = _hub()
    return key in hub.extensions and (live() or key in hub.menu_registered)


def live() -> bool:
    """Our pak was mounted this session and is current: scripts run through the link."""
    hub = _hub()
    return bool(hub.pak_at_start) and hub.status.get("state") == "live"


def status() -> dict[str, str]:
    """
    {"state", "detail"}: "checking" (not yet), "live", "next_start" (installed now, works from
    the next game start), "game_changed" (a game update changed the page we hook: needs a mod
    update), "conflict" (another mod's pak replaces it: detail names it), "error".
    """
    return dict(_hub().status)


# endregion
# region Game thread


def tick() -> None:
    """Call every frame from the mod's tick. Cheap when nothing is asked; works once a frame overall."""
    hub = _hub()
    now = time.perf_counter()
    if now - hub.ticked_at < TICK_GAP_S:
        return
    hub.ticked_at = now
    if not hub.extensions and hub.server is None:
        return
    owner = hub.owner
    if owner is None or (owner is _me() and hub.owner_stamp != _STAMP):
        _elect(hub)
        owner = hub.owner
    try:
        owner._owner_tick(hub, now)  # noqa: SLF001 - the owner may be another mod's copy
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[UI Link] {ex!r}")
        logging.error("".join(traceback.format_exception(ex)))


def _owner_tick(hub: types.ModuleType, now: float) -> None:
    if getattr(hub, "pause_wait", None):
        _pause_check(hub, now)  # v9: a few frames after pause_menu() only
    if hub.esc_up_at and now >= hub.esc_up_at:
        hub.esc_up_at = 0.0
        post_key(VK_ESCAPE, down=False)
    _text_tick(hub)  # v8: first, also in play (the text context must never stay on)
    if not hub.checked:
        hub.checked = True
        _start(hub)
    if not hub.extensions:
        return
    # v6: in play (no menu, nothing polled or posted since the last full pass) nothing below has
    # anything to do until the next fallback / dev publish look: ~20 us a frame of compares before
    if (not hub.menus_up and not hub.actions and hub.asks == hub.asks_done
            and now < getattr(hub, "idle_until", 0.0)):  # fmt: skip
        return
    if now >= getattr(hub, "fallback_at", 0.0):
        hub.fallback_at = now + FALLBACK_S
        _fallback(hub)
    if DEV and now - hub.published_at > PUBLISH_S:
        hub.published_at = now
        _publish(hub)
    hub.idle_until = min(hub.fallback_at, hub.published_at + PUBLISH_S) if DEV else hub.fallback_at  # v6
    mono = time.monotonic()
    up = mono - hub.view_polls.get(MENUS, -1e9) < MENUS_POLL_S
    # A close counts when the menus page stops polling, or at once when the game hides its mouse
    # cursor: the page goes quiet only MENUS_POLL_S later, and a menu opened again within that
    # (Esc out of one screen, another screen's key) went uncounted, so the first screen stayed up
    # over the second. Counting one close twice is harmless: pages only look for a change (v3)
    # The cursor only matters while the menus page polls (or just did): in play it isn't read at all
    # (v4: get_pc + the property every frame was half of this tick's cost)
    # v5: read every CURSOR_S (not every frame), from a PC kept in a weak pointer for CURSOR_PC_S
    if hub.menus_up or up:
        if now >= getattr(hub, "cursor_at", 0.0):
            hub.cursor_at = now + CURSOR_S
            cursor = _cursor_shown(hub, now)
            fell = cursor is False and getattr(hub, "cursor_was", False)
            hub.cursor_was = bool(cursor)
        else:
            fell = False
    else:
        fell = hub.cursor_was = False
        hub.cursor_at = 0.0  # the first tick of the next menu reads it at once
    if hub.menus_up and (not up or fell):
        hub.menus_closed += 1
    hub.menus_up = up
    while hub.actions:
        action = hub.actions.popleft()
        try:
            if action.get("op") == "close_menus":
                if hub.menus_up:
                    press_escape()
            elif action.get("op") == "ext":
                ext = hub.extensions.get(str(action.get("ext")))
                if ext is not None:
                    started = time.perf_counter()
                    ext["act"](action)
                    ext["dirty"] = True
                    _note_slow(ext, "act", time.perf_counter() - started)
        except Exception as ex:  # noqa: BLE001 - one broken script shouldn't break the others
            logging.error(f"[UI Link] {action.get('ext') or action.get('op')}: {ex!r}")
            logging.error("".join(traceback.format_exception(ex)))
    # v5: nothing polled since the last full pass: no walk over every script
    asks = getattr(hub, "asks", None)
    if asks is not None and asks == getattr(hub, "asks_done", -1):
        return
    answered = []
    left = False
    budget_end = 0.0
    for ext in list(hub.extensions.values()):
        asked = ext["asked"]
        if asked == ext["answered"]:
            continue
        if ext["dirty"] or not ext["state_json"] or mono - ext["built_at"] >= FRESH_S:
            # At least one build a tick; more only within the budget (v5)
            if budget_end and time.perf_counter() > budget_end:
                left = True
                continue
            started = time.perf_counter()
            budget_end = budget_end or started + BUILD_BUDGET_S
            _build(ext, mono)
            _note_slow(ext, "state", time.perf_counter() - started)
        answered.append((ext, asked))
    if answered:
        with hub.cond:
            for ext, asked in answered:
                ext["answered"] = asked
            hub.cond.notify_all()
    if not left and asks is not None:
        hub.asks_done = asks


def _note_slow(ext: dict[str, Any], what: str, took: float) -> None:
    """Keeps each script's slowest state build / action (`worst_state_ms`, `worst_act_ms`; v5)."""
    ms = took * 1000.0
    field = f"worst_{what}_ms"
    if ms <= ext.get(field, 0.0):
        return
    ext[field] = ms
    if DEV and took >= SLOW_S:
        # Lands in whichever mod's tick ran the link: name the script it really was
        logging.warning(f"[UI Link] {ext['key']} {what} took {ms:.1f} ms (its slowest so far)")


def _cursor_shown(hub: types.ModuleType | None = None, now: float = 0.0) -> bool | None:
    """Whether the game shows its mouse cursor (a menu is up), None outside the game."""
    try:
        pc = None
        kept = getattr(hub, "cursor_pc", None) if hub is not None else None
        if kept is not None and now < kept[1]:
            pc = kept[0]()
        if pc is None:
            from mods_base import get_pc  # noqa: PLC0415 - the setup app imports this module without the SDK

            pc = get_pc(possibly_loading=True)
            if hub is not None:
                # A weak pointer only: never a raw UObject across frames (v5)
                hub.cursor_pc = (_WeakPointer(pc), now + CURSOR_PC_S) if pc is not None and _WeakPointer else None
        return None if pc is None else bool(pc.bShowMouseCursor)
    except Exception:  # noqa: BLE001
        return None


def _build(ext: dict[str, Any], now: float) -> None:
    ext["dirty"] = False
    ext["built_at"] = now
    try:
        text = json.dumps(ext["state"](), separators=(",", ":"), default=str).encode("utf-8")
        if text != ext["state_json"]:
            ext["snap"] = (next(_revs), text)
        ext["state_json"] = text
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[UI Link] {ext['key']} state failed: {ex!r}")


def _fallback(hub: types.ModuleType) -> None:
    """
    While the link isn't running in this session's pages, menu-page scripts go through the BL4
    Mod Menu's own link if it's loaded (it may load after us, so this is re-checked).
    """
    exts = sys.modules.get("bl4_mod_menu.extensions")
    # Our pak was mounted at start and its check is still running: most likely live, so wait for it
    # rather than handing every script over and taking it back seconds later
    waiting = bool(hub.pak_at_start) and hub.status.get("state") == "checking"
    if waiting or live() or exts is None:
        want = set()
    else:
        want = {k for k, e in hub.extensions.items() if MENUS in e["views"]}
    if exts is not hub.menu_exts:
        hub.menu_registered = set()
        hub.menu_exts = exts
    for key in hub.menu_registered - want:
        try:
            exts.unregister(key)
        except Exception:  # noqa: BLE001
            pass
        hub.menu_registered.discard(key)
    for key in want - hub.menu_registered:
        ext = hub.extensions[key]
        try:
            exts.register(key, ext["script"], ext["state"], ext["act"])
            hub.menu_registered.add(key)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[UI Link] couldn't hand {key} to the Mod Menu: {ex!r}")
            hub.menu_registered.add(key)  # don't retry every frame


# endregion
# region Server


class _Handler(BaseHTTPRequestHandler):
    hub: types.ModuleType  # set per server (a class made in _start)

    def _send(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _query(self) -> dict[str, list[str]] | None:
        # The game's pages send no Origin (measured 2026-10-30); a browser always does on a request
        # to another site, so a web page never gets in, even with the token
        origin = self.headers.get("Origin")
        if origin is not None and not origin.lower().startswith("coui://"):
            return None
        query = parse_qs(self.path.partition("?")[2])
        token = self.hub.token
        return query if token and (query.get("t") or [""])[0] == token else None

    def do_GET(self) -> None:  # noqa: N802
        query = self._query()
        if query is None:
            self._send(404)
            return
        hub = self.hub
        if self.path.startswith("/hello"):
            # 421: another game's page (two games on one PC share link.json)
            owner = _peer_pid(self.client_address[1], self.server.server_address[1])
            mine = owner is None or owner == os.getpid()
            self._send(200 if mine else 421, b'{"ok":true}' if mine else b"")
            return
        if not self.path.startswith("/ext"):
            self._send(404)
            return
        view = (query.get("view") or [""])[0]
        if view:
            hub.view_polls[view] = time.monotonic()
        key = (query.get("k") or [""])[0]
        if not key:
            listing = {k: e["script"] for k, e in list(hub.extensions.items()) if ANY in e["views"] or view in e["views"]}
            self._send(200, json.dumps(listing).encode("utf-8"))
            return
        ext = hub.extensions.get(key)
        if ext is None:
            self._send(200, b"null")
            return
        ext["last_poll"] = time.monotonic()
        ext["page_open"] = (query.get("open") or ["0"])[0] == "1"
        # The game thread's next tick applies actions posted before this poll and rebuilds the
        # state if it's stale; it never waits for us
        with hub.cond:
            ext["asked"] += 1
            ticket = ext["asked"]
            hub.asks = getattr(hub, "asks", 0) + 1  # after ext["asked"]: the tick reads them the other way round (v5)
            hub.cond.wait_for(lambda: ext["answered"] >= ticket, WAIT_S)
        rev, state = ext["snap"]
        if not state:
            self._send(200, b"{}")
            return
        menus = b"true" if hub.menus_up else b"false"
        if (query.get("v") or [""])[0] == str(rev):
            self._send(200, b'{"menus":' + menus + b',"same":' + str(rev).encode() + b"}")
            return
        # "state" last: the page cuts its text out of the answer as is
        self._send(200, b'{"menus":' + menus + b',"rev":' + str(rev).encode() + b',"state":' + state + b"}")

    def do_POST(self) -> None:  # noqa: N802
        if self._query() is None or not self.path.startswith("/action"):
            self._send(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._send(400)
            return
        try:
            action = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send(400)
            return
        if isinstance(action, dict):
            if action.get("op") == "log":
                if DEV:
                    logging.dev_warning(f"[UI Link page] {str(action.get('text'))[:500]}")
            else:
                self.hub.actions.append(action)
        self._send(200, b'{"ok":true}')

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class _Server(HTTPServer):
    """Serves requests on THREADS long-lived threads (a thread per request fought the game for the GIL)."""

    def __init__(self, address: tuple[str, int], handler: type[_Handler]) -> None:
        super().__init__(address, handler)
        self._requests: queue.Queue = queue.Queue()
        for i in range(THREADS):
            threading.Thread(target=self._serve, name=f"bl4_uilink.server{i}", daemon=True).start()

    def process_request(self, request: Any, client_address: Any) -> None:
        self._requests.put((request, client_address))

    def _serve(self) -> None:
        while (item := self._requests.get()) is not None:
            request, client_address = item
            try:
                self.finish_request(request, client_address)
            except Exception:  # noqa: BLE001
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        for _ in range(THREADS):
            self._requests.put(None)


def _start(hub: types.ModuleType) -> None:
    """Owner's first tick: the server, link.json, the page files, then the pak check on a worker."""
    if GAME_DIR is None:
        return
    if hub.server is None:
        hub.token = secrets.token_hex(8)
        handler = type("_BoundHandler", (_Handler,), {"hub": hub})
        try:
            server = _Server(("127.0.0.1", 0), handler)
        except OSError as ex:
            logging.error(f"[UI Link] couldn't start its server: {ex}")
            return
        threading.Thread(target=server.serve_forever, name="bl4_uilink.server", daemon=True).start()
        hub.server = server
        threading.Thread(target=_write_link, args=(GAME_DIR, server.server_address[1], hub.token), name="bl4_uilink.link", daemon=True).start()  # fmt: skip
    _publish(hub)
    _text_register(hub)  # v8: the overlay / title pages run it from the start (no wait on a first box)
    threading.Thread(target=_check_pak, args=(hub, GAME_DIR), name="bl4_uilink.pak", daemon=True).start()


def _stop_server(hub: types.ModuleType) -> None:
    server, hub.server = hub.server, None
    if server is None:
        return
    # shutdown() waits for the serve loop: off the game thread
    threading.Thread(target=lambda: (server.shutdown(), server.server_close()), name="bl4_uilink.stop", daemon=True).start()
    if GAME_DIR is not None:
        port = server.server_address[1]
        threading.Thread(target=_write_link, args=(GAME_DIR, 0, "", port), name="bl4_uilink.link", daemon=True).start()


def stop() -> None:
    """Stops the link if no script is registered any more (call after unregister on disable)."""
    hub = _hub()
    if set(hub.extensions) - {TEXT_KEY}:
        return
    _text_release(hub)  # v8
    hub.extensions.pop(TEXT_KEY, None)
    _stop_server(hub)
    hub.checked = False


# endregion
# region Files (worker threads, or the owner's first tick for small writes)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _write_link(game_dir: Path, port: int, token: str, drop_port: int = 0) -> None:
    """Adds this game's server to link.json (port 0: removes `drop_port`). Dead games are pruned."""
    path = game_dir / UI_DIR / LINK_FILE
    with _hub().link_lock:
        _write_link_locked(path, port, token, drop_port)


def _write_link_locked(path: Path, port: int, token: str, drop_port: int) -> None:
    for _ in range(5):
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            servers = [
                s for s in (data.get("servers") or []) if isinstance(s, dict) and _alive(s.get("pid"))
                and not (s.get("pid") == os.getpid() and (port or s.get("port") == drop_port))
            ]  # fmt: skip
            if port:
                servers.append({"pid": os.getpid(), "port": port, "token": token, "dev": DEV, "v": VERSION})
            _write_json(path, {"servers": servers})
            return
        except OSError:
            time.sleep(0.2)  # another game writing at the same moment


def _publish(hub: types.ModuleType) -> None:
    """Copies boot.js / hub.js to the loose UI folder when they differ (packed: once)."""
    if GAME_DIR is None:
        return
    target = GAME_DIR / UI_DIR
    sigs = getattr(hub, "published_sig", None)
    if sigs is None:
        sigs = hub.published_sig = {}
    for name in UI_FILES:
        # Dev (a source folder): a file whose date and size didn't move isn't read again (v5: both
        # files were read every second)
        sig = None
        if DEV:
            try:
                st = os.stat(Path(__file__).parent / "ui" / name)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
            if sig is not None and name in hub.published and sigs.get(name) == sig:
                continue
        try:
            data = (ASSETS / "ui" / name).read_bytes()
        except OSError:
            continue
        sigs[name] = sig
        if hub.published.get(name) == data:
            continue
        try:
            path = target / name
            if not (path.is_file() and path.read_bytes() == data):
                target.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(name + f".{os.getpid()}.tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            hub.published[name] = data
        except OSError as ex:
            logging.error(f"[UI Link] couldn't write {name}: {ex}")


def build_pak() -> bytes:
    return pak.build_pak("../../../" + UI_PREFIX, {SHIM: (ASSETS / "page" / "debug.js").read_bytes()}, PAK_NAME)


def check(game_dir: Path) -> tuple[str, str, bool]:
    """
    (state, detail, wanted): what the pak should be. Plain file reads, any thread (also the
    installer). wanted False: remove ours.
    """
    expected = json.loads((ASSETS / "page" / "vanilla.json").read_text(encoding="utf-8"))["sha1"]
    paks_dir = game_dir / "OakGame" / "Content" / "Paks"
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + SHIM)
    if game_copy is None or game_copy.sha1 != expected:
        return "game_changed", "a game update changed the page script the link hooks", False
    ours = pak_path(game_dir)
    others = [
        p.name for p in sorted(ours.parent.glob("*.pak"))
        if p.name.lower() != ours.name.lower() and pak.winning_entry([p], UI_PREFIX + SHIM) is not None
    ]  # fmt: skip
    if others:
        return "conflict", ", ".join(others), False
    return "ok", "", True


def sync(game_dir: Path) -> str:
    """Installs, keeps or removes our pak (the setup app's entry point). Returns a status line."""
    state, detail, _ = check(game_dir)
    result = _sync(game_dir)
    if state == "ok":
        return "UI link ready." if result == "ok" else "UI link installed: mod pages show from the next game start."
    return f"UI link paused: {_MESSAGES[state].format(detail=detail)}"


_MESSAGES = {
    "game_changed": "a game update changed the page script it hooks. Our mods need an update for it.",
    "conflict": "{detail} in Paks/~mods replaces the same page script.",
}


def _sync(game_dir: Path) -> str:
    """Installs, keeps or removes our pak: "ok" (current), "next_start" (written now), or the check's state."""
    state, detail, wanted = check(game_dir)
    path = pak_path(game_dir)
    if not wanted:
        _remove(path)
        return state
    data = build_pak()
    replacement, removal = _staged(path)
    removal.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        replacement.unlink(missing_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return "next_start"
    if path.read_bytes() == data:
        replacement.unlink(missing_ok=True)
        return "ok"
    replacement.write_bytes(data)
    _apply_after_exit(path)
    return "next_start"


def _check_pak(hub: types.ModuleType, game_dir: Path) -> None:
    try:
        state, detail, _ = check(game_dir)
        result = _sync(game_dir)
    except Exception as ex:  # noqa: BLE001 - never let a file problem break a mod
        hub.status = {"state": "error", "detail": str(ex)}
        logging.error(f"[UI Link] pak check failed: {ex}")
        return
    if state == "ok":
        state = "live" if hub.pak_at_start and result == "ok" else "next_start"
    hub.status = {"state": state, "detail": detail}
    messages = {
        "live": "running in the game's pages.",
        "next_start": "installed: mod pages show from the next game start.",
        **{k: "paused: " + v.format(detail=detail) for k, v in _MESSAGES.items()},
    }
    logging.info(f"[UI Link] {messages.get(state, state)}")


def _staged(target: Path) -> tuple[Path, Path]:
    """`<pak>.next` (replace it with this) and `<pak>.remove` (delete it), applied after exit."""
    return target.with_name(target.name + ".next"), target.with_name(target.name + ".remove")


def _remove(path: Path) -> None:
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return
    removal.write_bytes(b"")
    _apply_after_exit(path)


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _apply_after_exit(target: Path) -> bool:
    """
    Starts (once per game session) a hidden helper that waits until no game is running, then
    applies whatever is staged next to `target` at that point (a pak changed under a running game
    is read at its old offsets). Same helper as the Mod Menu's pause_page.
    """
    hub = _hub()
    if hub.helper_started:
        return True
    replacement, removal = _staged(target)
    apply = (
        f"if (Test-Path -LiteralPath {_ps_quote(removal)}) {{ "
        f"Remove-Item -LiteralPath {_ps_quote(target)} -Force -ErrorAction Stop; "
        f"Remove-Item -LiteralPath {_ps_quote(removal)}, {_ps_quote(replacement)} -Force -ErrorAction SilentlyContinue }} "
        f"elseif (Test-Path -LiteralPath {_ps_quote(replacement)}) {{ "
        f"Move-Item -LiteralPath {_ps_quote(replacement)} -Destination {_ps_quote(target)} -Force -ErrorAction Stop }}"
    )
    script = (
        "while (Get-Process -Name Borderlands4 -ErrorAction SilentlyContinue) { Start-Sleep 2 }; "
        f"for ($i = 0; $i -lt 30; $i++) {{ try {{ {apply}; break }} catch {{ Start-Sleep 1 }} }}"
    )
    try:
        subprocess.Popen(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as ex:
        # Nothing will apply the staged change: the pak stays as it is past the next start
        logging.error(f"[UI Link] couldn't start the helper that updates {target.name} after the game exits: {ex!r}")
        return False
    hub.helper_started = True
    return True


# endregion
# region Text boxes (v8): typing with the game's own text input, no keyboard hook
#
# No key events reach the page our screens draw in (the menus page): while a menu is up the game
# gives the keyboard to the ECHO overlay page (`overlay_menu`), whose focused <input> gets real
# typing (2026-10-30). So a TextBox has an invisible <input> there (ui/text.js, a link script of
# ours), focused from code, which sends what's typed; our screens draw the box and text as before.
# The same keys still reach the game's input, so while a box is typed in an Enhanced Input mapping
# context of ours (TEXT_KEYS -> one do-nothing action that consumes them, priority TEXT_PRIORITY)
# keeps the menus from acting on them (A/S/D navigated the inventory without it), and gamekeys is
# muted so our own hotkeys don't fire. Until v8 each mod had a WH_KEYBOARD_LL hook for this.
#
# The context blocks those keys everywhere, so it's only on while a box is typed in with a page of
# ours polling, and it comes off when: the box stops (Enter, Esc, a click elsewhere, the mod), no
# overlay / title page polled for TEXT_QUIET_S (the menus closed), nothing was typed for
# TEXT_IDLE_S, the map changed, or the link stops. Checked first thing in every owner tick, also in
# play.

TEXT_KEY = "bl4text"
OVERLAY = "overlay_menu"  # the ECHO tab bar's page: it has the keyboard while a menu is up
TITLE = "menu_main"  # the title screen's page
TEXT_SCRIPT = "coui://uiresources/bl4link/text.js"
TEXT_PRIORITY = 100000  # above every context of the game's
TEXT_QUIET_S = 1.0  # no overlay / title page polled for this long: the menus closed, typing stops
TEXT_IDLE_S = 120.0  # nothing typed for this long: typing stops (the player walked away)
TEXT_KEYS = (
    *(chr(c) for c in range(ord("A"), ord("Z") + 1)),
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "NumPadZero", "NumPadOne", "NumPadTwo", "NumPadThree", "NumPadFour", "NumPadFive", "NumPadSix",
    "NumPadSeven", "NumPadEight", "NumPadNine", "Multiply", "Add", "Subtract", "Decimal", "Divide",
    "SpaceBar", "BackSpace", "Delete", "Enter", "Escape", "Tab", "Left", "Right", "Up", "Down",
    "Home", "End", "Semicolon", "Equals", "Comma", "Hyphen", "Period", "Slash", "Tilde",
    "LeftBracket", "Backslash", "RightBracket", "Apostrophe",
)  # fmt: skip


class TextBox:
    """
    A line being typed, through the game's own text input (see the region's comment). One at a time:
    a new one stops the last. Read .text (settable: the page shows it at once) and .done: None while
    typing, else True (Enter) / False (Esc, Tab, stopped) or, with named=True, "enter" / "tab" /
    "esc". poll() (game thread, every frame while typing) applies what was typed and says whether
    anything changed; stop() ends it. `allowed` filters typed characters; spaces=False drops spaces
    (links); paste reads the clipboard for Ctrl+V when the game's input doesn't paste by itself
    (called on a short-lived thread). .failed is always False (kept for the hook's interface).
    """

    failed = False

    def __init__(self, text: str, max_len: int, paste: Callable[[], str] | None = None, *,
                 allowed: Callable[[str], bool] | None = None, spaces: bool = True, named: bool = False) -> None:  # fmt: skip
        self.max_len = max_len
        self.allowed = allowed
        self.spaces = spaces
        self.named = named
        self.done: bool | str | None = None
        self.id = next(_revs)
        self.rev = 0  # moves when the text changes here, not from the page (the page then copies it)
        self.last_key = time.monotonic()
        self._paste = paste
        self._events: deque[tuple[str, str]] = deque()  # from the page: (op, value)
        self._pasted: deque[str] = deque()
        self._text = self._clean(text)
        hub = _hub()
        old = getattr(hub, "text_box", None)
        if old is not None and old is not self:
            old.stop()
        hub.text_box = self
        hub.text_since = time.monotonic()
        _text_register(hub)
        changed(TEXT_KEY)

    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        value = self._clean(value)
        if value != self._text:
            self._text = value
            self.rev += 1
            changed(TEXT_KEY)

    def _clean(self, text: str) -> str:
        text = "".join(c for c in str(text) if c.isprintable() and (self.allowed is None or self.allowed(c)))
        if not self.spaces:
            text = text.replace(" ", "")
        return text[: self.max_len]

    def _end(self, how: str) -> None:
        if self.done is None:
            self.done = how if self.named else how == "enter"

    def poll(self) -> bool:
        """Applies what was typed since the last call (game thread). True if anything changed."""
        changed_ = False
        while self._pasted and self.done is None:
            pasted = " ".join((self._pasted.popleft().strip().splitlines() or [""])[0].split())
            self.text = self._text + pasted
            changed_ = True
        while self._events and self.done is None:
            op, value = self._events.popleft()
            self.last_key = time.monotonic()
            changed_ = True
            if op == "text":
                clean = self._clean(value)
                if clean != value:
                    self.rev += 1  # the page shows what we kept
                    changed(TEXT_KEY)
                self._text = clean
            elif op in ("enter", "tab", "esc"):
                self._end(op)
            elif op == "clear":
                self.text = ""
            elif op == "paste" and self._paste is not None:
                threading.Thread(target=self._read_clipboard, name="bl4_uilink paste", daemon=True).start()
        if self.done is None and time.monotonic() - self.last_key > TEXT_IDLE_S:
            self._end("esc")
            changed_ = True
        if self.done is not None:
            self.stop()
        return changed_

    def _read_clipboard(self) -> None:
        try:
            text = self._paste() if self._paste is not None else ""
        except Exception:  # noqa: BLE001 - an unreadable clipboard pastes nothing
            text = ""
        self._pasted.append(text or "")

    def stop(self) -> None:
        """Ends typing (the text stays). The game's keys work again from the next frame."""
        self._end("esc")
        hub = _hub()
        if getattr(hub, "text_box", None) is self:
            hub.text_box = None
            changed(TEXT_KEY)


class KeyGrab:
    """
    Keeps these keys (Unreal key names) from the game's menus while it lives, and counts their
    presses: .take() (game thread, every frame) returns how many came since the last call. The
    Mod Menu's title screen uses it so Esc goes back a page instead of opening the game's "quit?"
    dialog. Stops by itself when take() isn't called for TEXT_QUIET_S.
    """

    def __init__(self, keys: tuple[str, ...] = ("Escape",)) -> None:
        self.keys = tuple(keys)
        self.taken_at = time.monotonic()
        self._held: set[str] = set()
        hub = _hub()
        grabs = getattr(hub, "key_grabs", None)
        if grabs is None:
            grabs = hub.key_grabs = []
        grabs.append(self)

    def take(self) -> int:
        self.taken_at = time.monotonic()
        count = 0
        gk = _gamekeys()
        for key in self.keys:
            held = gk.down(key)
            if held and key not in self._held:
                count += 1
            (self._held.add if held else self._held.discard)(key)
        return count

    def stop(self) -> None:
        grabs = getattr(_hub(), "key_grabs", None) or []
        if self in grabs:
            grabs.remove(self)


def _text_register(hub: types.ModuleType) -> None:
    if TEXT_KEY not in hub.extensions:
        register(TEXT_KEY, TEXT_SCRIPT, _text_state, _text_act, views=(OVERLAY, TITLE), mod="BL4 UI Link")


def _text_view(hub: types.ModuleType) -> str:
    """The page that has the keyboard: the overlay while a menu is up, else the title screen's."""
    now = time.monotonic()
    if now - hub.view_polls.get(OVERLAY, -1e9) < TEXT_QUIET_S:
        return OVERLAY
    if now - hub.view_polls.get(TITLE, -1e9) < TEXT_QUIET_S:
        return TITLE
    return ""


def _text_state() -> dict[str, Any]:
    hub = _hub()
    box = getattr(hub, "text_box", None)
    if box is None or box.done is not None:
        return {"id": 0}
    return {"id": box.id, "text": box.text, "rev": box.rev, "view": _text_view(hub)}


def _text_act(action: dict[str, Any]) -> None:
    box = getattr(_hub(), "text_box", None)
    if box is None or action.get("id") != box.id:
        return
    op = str(action.get("kind") or "")
    if op in ("text", "enter", "tab", "esc", "clear", "paste"):
        box._events.append((op, str(action.get("value") or "")))  # noqa: SLF001


def _text_tick(hub: types.ModuleType) -> None:
    """Owner, every tick, before anything else: the text context on only while it may be (above)."""
    box = getattr(hub, "text_box", None)
    grabs = getattr(hub, "key_grabs", None)
    if box is None and not grabs and not getattr(hub, "text_ctx_on", False) and not getattr(hub, "grab_ctx_on", False):
        return
    now = time.monotonic()
    view = _text_view(hub)
    if box is not None and box.done is None and (not view and now - hub.text_since > TEXT_QUIET_S):
        box.stop()  # the menus closed (or no page of ours ever answered)
        box = None
    if grabs:
        for grab in list(grabs):
            if now - grab.taken_at > TEXT_QUIET_S:
                grab.stop()
        grabs = hub.key_grabs
    typing = box is not None and box.done is None
    if typing:
        _gamekeys().mute(0.25)  # renewed every tick: lapses by itself if this stops running
    _context(hub, "text", TEXT_KEYS if typing else ())
    _context(hub, "grab", tuple(sorted({k for g in grabs or () for k in g.keys})) if not typing else ())


def _context(hub: types.ModuleType, which: str, keys: tuple[str, ...]) -> None:
    """Applies our mapping context `which` with these keys, or removes it (no keys)."""
    on_attr, ptr_attr, keys_attr = f"{which}_ctx_on", f"{which}_ctx", f"{which}_ctx_keys"
    on = getattr(hub, on_attr, False)
    if on and keys == getattr(hub, keys_attr, ()):
        return
    if not on and not keys:
        return
    try:
        import unrealsdk  # noqa: PLC0415
        from unrealsdk.unreal import BoundFunction, WeakPointer  # noqa: PLC0415

        iface = unrealsdk.find_class("EnhancedInputSubsystemInterface")
        sub = _input_subsystem(hub)
        opts = unrealsdk.make_struct("ModifyContextOptions", bIgnoreAllPressedKeysUntilRelease=False,
                                     bForceImmediately=True, bNotifyUserSettings=False)  # fmt: skip
        kept = getattr(hub, ptr_attr, None)
        ctx = kept() if kept is not None else None
        if on and ctx is not None and sub is not None:
            BoundFunction(iface._find("RemoveMappingContext"), sub)(ctx, opts)
        setattr(hub, on_attr, False)
        setattr(hub, keys_attr, ())
        if not keys or sub is None:
            return
        # A new context each time: it lives while applied (EnhancedPlayerInput.AppliedInputContexts
        # is a reflected property), and may be collected once removed
        ctx = unrealsdk.construct_object("InputMappingContext", sub, f"bl4fix_{which}_keys_{next(_revs)}")
        action = unrealsdk.construct_object("InputAction", ctx, "bl4fix_keys")  # consumes input by default
        for key in keys:
            ctx.MapKey(action, unrealsdk.make_struct("Key", KeyName=key))
        BoundFunction(iface._find("AddMappingContext"), sub)(ctx, TEXT_PRIORITY, opts)
        setattr(hub, ptr_attr, WeakPointer(ctx))
        setattr(hub, on_attr, True)
        setattr(hub, keys_attr, keys)
    except Exception as ex:  # noqa: BLE001 - typing still works, the menus may react to it
        setattr(hub, on_attr, False)
        logging.error(f"[UI Link] the {which} key context failed: {ex!r}")


def _input_subsystem(hub: types.ModuleType) -> Any:
    """The local player's EnhancedInputLocalPlayerSubsystem (kept in a weak pointer)."""
    kept = getattr(hub, "input_sub", None)
    sub = kept() if kept is not None else None
    if sub is not None:
        return sub
    import unrealsdk  # noqa: PLC0415
    from mods_base import get_pc  # noqa: PLC0415
    from unrealsdk.unreal import WeakPointer  # noqa: PLC0415

    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Player is None:
        return None
    lib = unrealsdk.find_class("SubsystemBlueprintLibrary").ClassDefaultObject
    sub = lib.GetLocalPlayerSubSystemFromPlayerController(pc, unrealsdk.find_class("EnhancedInputLocalPlayerSubsystem"))
    hub.input_sub = WeakPointer(sub) if sub is not None else None
    return sub


def _text_release(hub: types.ModuleType) -> None:
    """Every context of ours off, at once (the link stops, the owner is replaced)."""
    box = getattr(hub, "text_box", None)
    if box is not None:
        box.stop()
    for grab in list(getattr(hub, "key_grabs", None) or ()):
        grab.stop()
    _context(hub, "text", ())
    _context(hub, "grab", ())


# endregion
# region Keys (bl4_mod_menu/keys.py, cut down; the Mod Menu's is used when it's loaded)

# ctypes.WinDLL, not PyDLL: these calls must let go of the GIL. Our own objects, so the shared
# windll prototypes other code uses stay untouched.
_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")
_iphlpapi = ctypes.WinDLL("iphlpapi")
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
_user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
_user32.PostMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
_user32.EnumWindows.argtypes = (_ENUM_PROC, ctypes.c_void_p)
_kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
_kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
_iphlpapi.GetExtendedTcpTable.argtypes = (
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
)  # fmt: skip
_iphlpapi.GetExtendedTcpTable.restype = ctypes.c_ulong

_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
VK_ESCAPE = 0x1B
# Unreal key names (what mods_base keybinds hold) -> Windows virtual keys
VIRTUAL_KEYS: dict[str, int] = {
    **{chr(0x41 + i): 0x41 + i for i in range(26)},
    **{name: 0x30 + i for i, name in enumerate(_DIGITS)},
    **{f"F{i + 1}": 0x70 + i for i in range(12)},
    **{f"NumPad{name}": 0x60 + i for i, name in enumerate(_DIGITS)},
    "Multiply": 0x6A, "Add": 0x6B, "Subtract": 0x6D, "Decimal": 0x6E, "Divide": 0x6F,
    "BackSpace": 0x08, "Tab": 0x09, "Enter": 0x0D, "Pause": 0x13, "CapsLock": 0x14,
    "SpaceBar": 0x20, "PageUp": 0x21, "PageDown": 0x22, "End": 0x23, "Home": 0x24,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "Insert": 0x2D, "Delete": 0x2E,
    "NumLock": 0x90, "ScrollLock": 0x91,
    "LeftShift": 0xA0, "RightShift": 0xA1, "LeftControl": 0xA2, "RightControl": 0xA3,
    "LeftAlt": 0xA4, "RightAlt": 0xA5,
    "Semicolon": 0xBA, "Equals": 0xBB, "Comma": 0xBC, "Hyphen": 0xBD, "Period": 0xBE,
    "Slash": 0xBF, "Tilde": 0xC0, "LeftBracket": 0xDB, "Backslash": 0xDC, "RightBracket": 0xDD,
    "Apostrophe": 0xDE,
    "LeftMouseButton": 0x01, "RightMouseButton": 0x02, "MiddleMouseButton": 0x04,
    "ThumbMouseButton": 0x05, "ThumbMouseButton2": 0x06,
}  # fmt: skip

# The last foreground window and whether it's ours: one tuple, replaced whole (any thread)
_focus: tuple[int, bool] = (0, False)


def game_focused() -> bool:
    """The game window is in front. One Win32 call while the foreground window stays the same (v4)."""
    global _focus  # noqa: PLW0603
    hwnd = _user32.GetForegroundWindow() or 0
    known, ours = _focus
    if hwnd != known or not hwnd:
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        ours = bool(hwnd) and pid.value == os.getpid()
        _focus = (hwnd, ours)
    return ours


def _gamekeys() -> Any:
    """The carrying mod's gamekeys copy (imported when first used: the setup app imports this
    package without the SDK), else whichever copy reads keys for every mod."""
    parent = __name__.rpartition(".")[0]
    if parent:
        import importlib  # noqa: PLC0415

        return importlib.import_module(parent + ".gamekeys")
    owner = getattr(sys.modules.get("_bl4_gamekeys"), "owner", None)
    if owner is None:
        raise ImportError("no gamekeys loaded")
    return owner


def vk_down(vk: int) -> bool:
    """
    Whether a virtual key is held, from the game's own input (gamekeys, v7, 2026-10-30; until then
    a worker thread of ours polled GetAsyncKeyState). False while the game isn't in front.
    """
    return _gamekeys().vk_down(vk)


# How a few key names read on screen (as the Mod Menu shows them)
_DISPLAY = {
    "SpaceBar": "Space", "BackSpace": "Backspace", "LeftControl": "Left Ctrl",
    "RightControl": "Right Ctrl", "LeftShift": "Left Shift", "RightShift": "Right Shift",
    "LeftAlt": "Left Alt", "RightAlt": "Right Alt", "RightMouseButton": "Right Mouse",
    "MiddleMouseButton": "Middle Mouse", "ThumbMouseButton": "Mouse 4",
    "ThumbMouseButton2": "Mouse 5", "PageUp": "Page Up", "PageDown": "Page Down",
}  # fmt: skip


def display_name(name: str | None) -> str:
    """A key name as a prompt shows it ("T", "5", "Right Mouse"; "Unbound" for none) (v2)."""
    if not name:
        return "Unbound"
    if name in _DIGITS:
        return str(_DIGITS.index(name))
    return _DISPLAY.get(name, name)


def key_down(name: str | None) -> bool:
    """Whether the named key (an Unreal key name, as keybind options hold; pads too) is held."""
    return _gamekeys().down(name)


def _game_window() -> int | None:
    found: list[int] = []
    pid = os.getpid()

    def check(hwnd: int, _: int) -> bool:
        owner = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(_ENUM_PROC(check), None)
    return found[0] if found else None


WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101


def post_key(vk: int, *, down: bool) -> bool:
    """A key down or up message to the game window (input is sampled once a frame: hold it a bit)."""
    hwnd = _game_window()
    if hwnd is None:
        return False
    _user32.PostMessageW(hwnd, WM_KEYDOWN if down else WM_KEYUP, vk, 1 if down else 0xC0000001)
    # The game's input sees a posted key as held (Windows' key state never did): ours isn't a press (v7)
    try:
        gk = _gamekeys()
        if name := gk.VK_NAMES.get(vk):
            gk.ignore(name, 5.0 if down else 0.25)
    except Exception:  # noqa: BLE001
        pass
    return True


def press_escape() -> None:
    """
    What Esc does for the game: opens the pause menu in gameplay, closes the open menu otherwise.
    The pause menu through its UI state (v9, pause_menu()); Esc posted to the window for the rest.
    """
    hub = _hub()
    if not _pause_native(hub):
        _post_escape(hub)


def _post_escape(hub: types.ModuleType) -> None:
    if hub.esc_up_at:
        return  # still holding the last one
    if post_key(VK_ESCAPE, down=True):
        hub.esc_up_at = time.perf_counter() + ESC_HOLD_S


# region The pause menu through the game's UI state (v9)
#
# The game's menus are UI states: gameplay tags `UI.State.<NAME>` (the `uistate` NCS; the pause
# menu is MENU_PAUSE, a child of MENU). GbxUIUMGPlayerSubsystem.AddUIStateToPlayer /
# RemoveUIStateToPlayer open and close the pause menu as Esc does, with no key sent to the window
# (so it also works with the game in the back). Checked in game 2026-10-30: added in play, the
# pause menu showed and its page woke; removed, it closed back to play. The bare MENU state (and
# MENU_UI_DEV) only put the weapon away and showed the cursor, with no page of ours: not usable.
# Esc stays for: another menu up (not the pause menu: Esc closes it), no BASE_HUD state
# (cinematics remove it: the game decides there), and a state that didn't take within
# PAUSE_CHECK_S (a game patch): Esc then, and for the rest of the session.
PAUSE_STATE = "UI.State.MENU_PAUSE"
HUD_STATE = "UI.State.BASE_HUD"
PAUSE_CHECK_S = 0.5


def pause_menu() -> bool:
    """
    Opens the pause menu in gameplay, or closes it when it's up, through the game's UI state. False
    when that isn't the right way here (another menu, a cinematic, it failed before): press Esc.
    """
    return _pause_native(_hub())


def _ui_states() -> tuple[Any, Any] | None:
    """(the player controller, the local player's UI state subsystem), or None."""
    import unrealsdk  # noqa: PLC0415
    from mods_base import get_pc  # noqa: PLC0415

    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Player is None:
        return None
    lib = unrealsdk.find_class("SubsystemBlueprintLibrary").ClassDefaultObject
    sub = lib.GetLocalPlayerSubSystemFromPlayerController(pc, unrealsdk.find_class("GbxUIUMGPlayerSubsystem"))
    return None if sub is None else (pc, sub)


def _has_state(sub: Any, name: str) -> bool:
    import unrealsdk  # noqa: PLC0415

    got = sub.HasUIState(unrealsdk.make_struct("GameplayTag", TagName=name))
    return bool(got[0] if isinstance(got, tuple) else got)


def _pause_native(hub: types.ModuleType) -> bool:
    if getattr(hub, "pause_native", True) is False:
        return False
    wait = getattr(hub, "pause_wait", None)
    if wait is not None and time.perf_counter() < wait[0] + 1.0:
        return True  # the last change is still landing (a second press, a repeated close)
    try:
        import unrealsdk  # noqa: PLC0415

        found = _ui_states()
        if found is None:
            return False
        pc, sub = found
        tag = unrealsdk.make_struct("GameplayTag", TagName=PAUSE_STATE)
        if _has_state(sub, PAUSE_STATE):
            sub.RemoveUIStateToPlayer(pc, tag, pc.Player)
            want = False
        elif pc.bShowMouseCursor or not _has_state(sub, HUD_STATE):
            return False  # another menu is up, or a cinematic / load: Esc, and the game decides
        else:
            sub.AddUIStateToPlayer(pc, tag, pc.Player)
            want = True
    except Exception as ex:  # noqa: BLE001 - Esc still works
        hub.pause_native = False
        logging.warning(f"[UI Link] the pause menu's UI state failed ({ex!r}); using Esc instead")
        return False
    hub.pause_wait = (time.perf_counter() + PAUSE_CHECK_S, want)
    return True


def _pause_check(hub: types.ModuleType, now: float) -> None:
    """Owner tick after pause_menu(): did the state change? Else Esc, and Esc from now on."""
    until, want = hub.pause_wait
    try:
        found = _ui_states()
        ok = found is not None and _has_state(found[1], PAUSE_STATE) == want
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        hub.pause_wait = None
    elif now >= until:
        hub.pause_wait = None
        hub.pause_native = False
        logging.warning("[UI Link] the pause menu's UI state didn't change; using Esc from now on")
        _post_escape(hub)


# endregion


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


class _TcpRow(ctypes.Structure):  # MIB_TCPROW_OWNER_PID
    _fields_ = (
        ("state", ctypes.c_ulong), ("local_addr", ctypes.c_ulong), ("local_port", ctypes.c_ulong),
        ("remote_addr", ctypes.c_ulong), ("remote_port", ctypes.c_ulong), ("pid", ctypes.c_ulong),
    )  # fmt: skip


def _peer_pid(peer_port: int, our_port: int) -> int | None:
    """The process that owns the client end of a connection to us (None: not found). Server threads only."""
    size = ctypes.c_ulong(0)
    buf = None
    for _ in range(3):  # the table can grow between the size query and the read
        # AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS
        if _iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), 0, 2, 4, 0) == 0 and buf is not None:
            break
        buf = ctypes.create_string_buffer(size.value + 4096)
        size.value = len(buf)
    else:
        return None
    count = ctypes.c_ulong.from_buffer(buf).value
    rows = (_TcpRow * count).from_buffer(buf, 4)
    # Ports are in network byte order in the low 16 bits
    want_local = ((peer_port & 0xFF) << 8) | (peer_port >> 8)
    want_remote = ((our_port & 0xFF) << 8) | (our_port >> 8)
    for row in rows:
        if (row.local_port & 0xFFFF) == want_local and (row.remote_port & 0xFFFF) == want_remote:
            return int(row.pid)
    return None


# endregion
