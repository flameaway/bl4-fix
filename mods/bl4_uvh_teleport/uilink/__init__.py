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
module object): the registered scripts, the server, the key thread. The newest copy (VERSION) runs
the server ("owner"); an older owner is replaced, registrations kept. Shared fields are only ever
added, never renamed or given a new meaning, so any copy can read what an older one left.

Python side, from a mod:

    from . import uilink
    uilink.register("builds", "coui://uiresources/bl4builds/builds.js", state, act, views=(uilink.MENUS,))
    uilink.tick()         # from the mod's per-frame hook (does work at most once a frame overall)
    uilink.changed("builds"), uilink.polling(key), uilink.page_open(key), uilink.key_down("F8")
    uilink.reachable(key), uilink.display_name("T"), uilink.press_escape(), uilink.menus_closed()
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

Threads: the server's few pooled threads and the key thread do no game access. Everything that
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
VERSION = 4
HUB = "_bl4_uilink"
PAK_NAME = "BL4Fix_UILink_4251_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
SHIM = "_shared/js/debug.js"
UI_DIR = UI_PREFIX + "bl4link"
LINK_FILE = "link.json"
UI_FILES = ("boot.js", "hub.js")
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
        hub.keys_asked = {}  # vk -> when it was last asked about
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
    hub.checked = False
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
    if hub.esc_up_at and now >= hub.esc_up_at:
        hub.esc_up_at = 0.0
        post_key(VK_ESCAPE, down=False)
    if not hub.checked:
        hub.checked = True
        _start(hub)
    if not hub.extensions:
        return
    if now >= getattr(hub, "fallback_at", 0.0):
        hub.fallback_at = now + FALLBACK_S
        _fallback(hub)
    if DEV and now - hub.published_at > PUBLISH_S:
        hub.published_at = now
        _publish(hub)
    mono = time.monotonic()
    up = mono - hub.view_polls.get(MENUS, -1e9) < MENUS_POLL_S
    # A close counts when the menus page stops polling, or at once when the game hides its mouse
    # cursor: the page goes quiet only MENUS_POLL_S later, and a menu opened again within that
    # (Esc out of one screen, another screen's key) went uncounted, so the first screen stayed up
    # over the second. Counting one close twice is harmless: pages only look for a change (v3)
    # The cursor only matters while the menus page polls (or just did): in play it isn't read at all
    # (v4: get_pc + the property every frame was half of this tick's cost)
    if hub.menus_up or up:
        cursor = _cursor_shown()
        fell = cursor is False and getattr(hub, "cursor_was", False)
        hub.cursor_was = bool(cursor)
    else:
        fell = hub.cursor_was = False
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
                    ext["act"](action)
                    ext["dirty"] = True
        except Exception as ex:  # noqa: BLE001 - one broken script shouldn't break the others
            logging.error(f"[UI Link] {action.get('ext') or action.get('op')}: {ex!r}")
            logging.error("".join(traceback.format_exception(ex)))
    answered = []
    for ext in list(hub.extensions.values()):
        asked = ext["asked"]
        if asked == ext["answered"]:
            continue
        if ext["dirty"] or not ext["state_json"] or mono - ext["built_at"] >= FRESH_S:
            _build(ext, mono)
        answered.append((ext, asked))
    if answered:
        with hub.cond:
            for ext, asked in answered:
                ext["answered"] = asked
            hub.cond.notify_all()


def _cursor_shown() -> bool | None:
    """Whether the game shows its mouse cursor (a menu is up), None outside the game."""
    try:
        from mods_base import get_pc  # noqa: PLC0415 - the setup app imports this module without the SDK

        pc = get_pc(possibly_loading=True)
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
    if hub.extensions:
        return
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
    for name in UI_FILES:
        try:
            data = (ASSETS / "ui" / name).read_bytes()
        except OSError:
            continue
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
    except OSError:
        return False
    hub.helper_started = True
    return True


# endregion
# region Keys (bl4_mod_menu/keys.py, cut down; the Mod Menu's is used when it's loaded)

# ctypes.WinDLL, not PyDLL: these calls must let go of the GIL. Our own objects, so the shared
# windll prototypes other code uses stay untouched.
_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")
_iphlpapi = ctypes.WinDLL("iphlpapi")
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short
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

# Snapshot age at worst; a tap shorter than this still shows ("pressed since the last read", v4).
# Was 15 ms: each of its Win32 calls hands the GIL back and forth with the game thread
KEY_POLL_S = 0.03
KEY_FORGET_S = 2.0  # a key nobody asked about for this long isn't read any more
KEY_IDLE_EXIT_S = 5.0  # with no asks for this long the thread stops (a later ask starts it again)


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


def _poll_keys(hub: types.ModuleType, token: object) -> None:
    # Win32 calls only: fine off the game thread. A newer thread (new token) replaces this one
    was_focused = False
    polled: set[int] = set()
    while hub.keys_token is token:
        now = time.monotonic()
        asked = list(hub.keys_asked.items())  # the game thread adds keys meanwhile
        wanted = [vk for vk, at in asked if now - at < KEY_FORGET_S]
        if not wanted and now - max((at for _, at in asked), default=0.0) > KEY_IDLE_EXIT_S:
            hub.keys_token = None
            return
        focused = bool(wanted) and game_focused()
        if focused:
            down = set()
            for vk in wanted:
                bits = _user32.GetAsyncKeyState(vk)
                # Bit 0 = pressed since the last read: only trusted for a key read last poll too, with
                # the game in front all along (else a press from long ago, or in another window)
                if bits & 0x8000 or (bits & 1 and was_focused and vk in polled):
                    down.add(vk)
            hub.keys_down = frozenset(down)
            polled = set(wanted)
        else:
            hub.keys_down = frozenset()
            polled = set()
        was_focused = focused
        time.sleep(KEY_POLL_S)


def vk_down(vk: int) -> bool:
    """Whether a virtual key is held, from a worker thread's snapshot (False while the game isn't in front)."""
    keys = sys.modules.get("bl4_mod_menu.keys")
    if keys is not None and hasattr(keys, "vk_down"):
        try:
            return bool(keys.vk_down(vk))
        except Exception:  # noqa: BLE001 - another Mod Menu version: ours below
            pass
    hub = _hub()
    now = time.monotonic()
    first = now - hub.keys_asked.get(vk, -KEY_FORGET_S) >= KEY_FORGET_S
    hub.keys_asked[vk] = now
    thread = hub.keys_thread
    if thread is None or not thread.is_alive() or hub.keys_token is None:
        token = hub.keys_token = object()
        hub.keys_thread = threading.Thread(target=_poll_keys, args=(hub, token), name="bl4_uilink.keys", daemon=True)
        hub.keys_thread.start()
    if first:
        # A key held when we started doesn't count as a press: report it held until the thread reads it
        return True
    return vk in hub.keys_down


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
    """Whether the named key (an Unreal key name, as keybind options hold) is held."""
    vk = VIRTUAL_KEYS.get(name or "")
    return vk is not None and vk_down(vk)


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
    return True


def press_escape() -> None:
    """Esc for the game: opens the pause menu in gameplay, closes the open menu otherwise."""
    hub = _hub()
    if hub.esc_up_at:
        return  # still holding the last one
    if post_key(VK_ESCAPE, down=True):
        hub.esc_up_at = time.perf_counter() + ESC_HOLD_S


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
