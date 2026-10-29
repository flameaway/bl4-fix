"""
The link between the HTML mod menu (a loose page script running inside the game's menu view) and
Python: a tiny HTTP server on 127.0.0.1.

The page finds the port and a per-session token in the loose file
uiresources/bl4modmenu/session.json, then:
- GET  /hello   200 only if the request comes from this game process (see below)
- GET  /state   the mods and their options as JSON (built on the game thread, see below)
- POST /action  {op, mod, path, value}: queued, and applied on the game thread by tick()
- GET  /repos   the Discover / Repositories pages' data (repos.py), answered on the server thread:
                plain Python state, no game access, so polls never wait for a frame

The server thread never touches mods or the game: it only hands out the last snapshot and queues
actions. Everything else happens in tick(), which runs every frame on the game thread.

Cost: a GET /state waits (up to WAIT_S, normally one frame) for the next tick, which applies the
queued actions and rebuilds the snapshot only if something changed or, while the mods screen is
showing, the last one is SNAPSHOT_S old. While the screen is closed the snapshot is just the
small header (no mods list), unless an open request is fresh. Nobody polling = no builds, except
for events (actions, messages, open/close requests), which rebuild at once.

Rebinding a key works like the UMG menu: no key events reach page scripts, so after a "capture"
action we read the next key press from Windows and set it ourselves.

Two games on one PC (co-op testing) share the one session.json: the second game's page read the
first game's port, so F6/F7/F8 there only opened the pause menu and then the UMG panels (seen
2026-10-25). Now the file lists every running game's server ("servers": {pid, port, token}). With
more than one, the page asks each for /hello, and a server answers 200 only when the TCP
connection's owner (GetExtendedTcpTable) is its own process: Coherent's XHRs come from inside the
game. The top-level port/token stay (the last game started), so an older page script still works.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path
from collections.abc import Callable
from typing import Any

from unrealsdk import logging

from . import extensions, hotload, keys, model, repos, updates

UI_DIR = "OakGame/Content/uiresources/bl4modmenu"
SESSION_FILE = "session.json"
# The page polls about every 250 ms while it's showing; within this long it counts as connected
CONNECTED_S = 3.0
# While the mods screen shows, a poll gets a snapshot built up to this long ago (option values can
# change from other mods' code without telling us)
SNAPSHOT_S = 0.25
# How long a poll waits for the game thread's next tick before taking the last snapshot
WAIT_S = 0.2
MAX_BODY = 64 * 1024
# The page's scripts, kept as loose files (never in a pak, or they couldn't reload themselves)
UI_FILES = ("link.js", "loader.js", "main_loader.js", "menu.js", "menu.css", "extensions.js")
PUBLISH_S = 1.0
# A packed .sdkmod can't change while the game runs: then the loop only repairs deleted files
PUBLISH_RELEASE_S = 10.0
# Threads serving the page's requests. Polls wait up to a frame for the game tick, and several
# scripts poll at once (menu, Weapondex, Reroll, Build Presets, UVH, BL4 Direct)
WEB_THREADS = 6


class Link:
    server: _PooledServer | None = None
    token = ""
    snapshot = b"{}"
    snapshot_at = 0.0
    snapshot_full = False  # the snapshot carries the mods list
    dirty = True  # something in the snapshot changed since it was built
    # Polls waiting for the game thread: tickets handed out and the last one answered (_cond)
    asked = 0
    answered = 0
    revision = 0
    started_at = 0.0
    last_poll = 0.0
    page_open = False  # the page says its mods screen is showing
    actions: deque[dict[str, Any]] = deque(maxlen=64)
    # Shown by the page as a toast; id lets it show each message once
    message: dict[str, Any] = {"id": 0, "text": "", "error": False}
    # Bumped when Python wants the page to open its mods screen (F7), with the mod to show. The
    # page acts on it while it's fresh, since it may only wake up (a menu opening) a moment later.
    open_request: dict[str, Any] = {"id": 0, "mod": None, "own": False}
    open_requested_at = -1e9
    # Bumped when the open key is pressed while the screen is up: the page closes it
    close_request = 0
    snapshot_fresh = False  # the "fresh" the current snapshot says
    # Recent events with times, for debugging the page link from the bridge
    trace: deque[str] = deque(maxlen=60)
    snapshot_open_id = 0
    capture: dict[str, Any] | None = None  # {mod, path} while waiting for a key
    capture_held: set[int] = set()  # noqa: RUF012
    # Whether one of the game's menus is up; the page closes its screen when they go away
    menus_up = False
    # Counts every time the game's menus close (seen here every frame). The page closes its screen
    # when this changes, even if it slept through the close: its scripts pause with the menus.
    menus_closed = 0
    game_dir: Path | None = None
    updates_rev = -1  # updates.U.revision in the current snapshot
    repos_rev = -1  # repos.summary()["rev"] in the current snapshot
    manifest_seen: Any = None  # the updates.U.manifest repos.py last listed
    quit_at = 0.0  # Restart on the Updates page: quit the game at this time (0: no)


# Restart waits this long, so the page gets its answer and shows "Restarting" first
QUIT_DELAY_S = 0.6

_cond = threading.Condition()


def connected() -> bool:
    """The page is polling right now (it only runs while one of the game's menus is showing)."""
    return Link.server is not None and time.monotonic() - Link.last_poll < CONNECTED_S


def seen() -> bool:
    """The page has talked to this server at least once, so it's loaded in the game's menus."""
    return Link.server is not None and Link.last_poll > Link.started_at


# region Server thread


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        query = self.path.partition("?")[2]
        return f"t={Link.token}" in query.split("&")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/hello") and self._authorized():
            # 421 Misdirected: another game's page (two games on one PC share session.json)
            owner = _peer_pid(self.client_address[1], self.server.server_address[1])
            mine = owner is None or owner == os.getpid()  # unknown: don't lock our own page out
            Link.trace.append(f"{time.monotonic():.2f} hello from pid {owner}: {'ours' if mine else 'not ours'}")
            self._send(200 if mine else 421, b'{"ok":true}' if mine else b"")
            return
        if self.path.startswith("/repos") and self._authorized():
            # Discover / Repositories: plain state, built here (never waits for the game thread)
            self._send(200, _repos_answer(self.path))
            return
        if self.path.startswith("/ext") and self._authorized():
            # Other mods' screens on this page (extensions.py)
            self._send(200, extensions.handle_get(self.path))
            return
        if not self.path.startswith("/state") or not self._authorized():
            self._send(404)
            return
        Link.last_poll = time.monotonic()
        Link.page_open = "open=1" in self.path
        Link.trace.append(f"{Link.last_poll:.2f} poll open={int(Link.page_open)} snap_open_id={Link.snapshot_open_id}")
        # The game thread's next tick applies actions posted before this poll and rebuilds the
        # snapshot if it's stale; it never waits for us
        with _cond:
            Link.asked += 1
            ticket = Link.asked
            _cond.wait_for(lambda: Link.answered >= ticket, WAIT_S)
        self._send(200, Link.snapshot)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/action") or not self._authorized():
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
            Link.actions.append(action)
        self._send(200, b'{"ok":true}')

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


# Look at sdk_mods for new or replaced mods at most this often, while the page asks for /repos
SCAN_S = 2.0


def _repos_answer(path: str) -> bytes:
    """/repos?v=<rev the page holds>: the whole state, or {"same": rev} when nothing changed."""
    game_dir = Link.game_dir
    if game_dir is not None and time.monotonic() - hotload.H.scanned_at >= SCAN_S:
        hotload.scan(game_dir)
    rev, data = repos.snapshot_json()
    query = path.partition("?")[2].split("&")
    if f"v={rev}" in query:
        return b'{"same":%d}' % rev
    return data


# name -> (source stamp, target stamp) at the last publish; unchanged stamps = nothing to read
_published: dict[str, tuple[Any, Any]] = {}


def _stamp(path: Any) -> tuple[int, int] | None:
    """(size, mtime_ns), or None where there's no file to stat (the .sdkmod zip: never changes)."""
    try:
        info = os.stat(str(path))
    except (OSError, ValueError):
        return None
    return info.st_size, info.st_mtime_ns


def publish_files(game_dir: Path) -> None:
    """Copies the page's scripts to uiresources/bl4modmenu/, touching only files that changed."""
    assets = resources.files(__package__) / "ui"
    for name in UI_FILES:
        source = assets / name
        target = game_dir / UI_DIR / name
        stamps = (_stamp(source), _stamp(target))
        if stamps[1] is not None and _published.get(name) == stamps:
            continue
        data = source.read_bytes()
        try:
            same = target.read_bytes() == data
        except OSError:
            same = False
        if not same:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        _published[name] = (stamps[0], _stamp(target))


class _PooledServer(HTTPServer):
    """
    Serves requests on WEB_THREADS long-lived threads. ThreadingHTTPServer started an OS thread
    (and a Python thread state) for every request, ~30 a second with the dex open, each one
    competing with the game thread for the GIL (a sampler saw the game thread waiting on it).
    """

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self._requests: queue.Queue = queue.Queue()
        for i in range(WEB_THREADS):
            threading.Thread(target=self._serve, name=f"bl4_mod_menu.web{i}", daemon=True).start()

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
        for _ in range(WEB_THREADS):
            self._requests.put(None)


def _publish_loop(server: _PooledServer, game_dir: Path) -> None:
    """
    Worker thread: republishes the page's files every PUBLISH_S (picks up edits through the dev
    junction, repairs deleted files) until this server stops. File I/O only. On the game thread
    it cost 1.6 ms a second, and starting a thread each second still cost 0.6 ms.
    """
    every = PUBLISH_S if isinstance(resources.files(__package__), Path) else PUBLISH_RELEASE_S
    while Link.server is server:
        time.sleep(every)
        try:
            publish_files(game_dir)
            # Another game starting at the same moment could have written the list without us
            if Link.server is server and not _session_has_us(_read_session(game_dir / UI_DIR / SESSION_FILE), server):
                _write_session(game_dir, server)
        except OSError as ex:
            logging.dev_warning(f"[Mod Menu] couldn't publish the page's files: {ex}")


def start(game_dir: Path) -> None:
    """Starts the server and tells the page where to find it."""
    if Link.server is not None:
        return
    Link.game_dir = game_dir
    publish_files(game_dir)
    Link.token = secrets.token_hex(8)
    server = _PooledServer(("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, name="bl4_mod_menu.web", daemon=True).start()
    Link.server = server
    threading.Thread(target=_publish_loop, args=(server, game_dir), name="bl4_mod_menu.publish", daemon=True).start()
    Link.started_at = time.monotonic()
    _write_session(game_dir, server)
    Link.dirty = True


# Win32 for the session list, on our own DLL objects (shared windll prototypes stay untouched).
# Only the server and publish threads call these.
_iphlpapi = ctypes.WinDLL("iphlpapi")
_iphlpapi.GetExtendedTcpTable.argtypes = (
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
)
_iphlpapi.GetExtendedTcpTable.restype = ctypes.c_ulong
_k32 = ctypes.WinDLL("kernel32")
_k32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
_k32.GetExitCodeProcess.restype = ctypes.c_int
_k32.CloseHandle.argtypes = (ctypes.c_void_p,)
_k32.CloseHandle.restype = ctypes.c_int


class _TcpRow(ctypes.Structure):  # MIB_TCPROW_OWNER_PID
    _fields_ = (
        ("state", ctypes.c_ulong), ("local_addr", ctypes.c_ulong), ("local_port", ctypes.c_ulong),
        ("remote_addr", ctypes.c_ulong), ("remote_port", ctypes.c_ulong), ("pid", ctypes.c_ulong),
    )


def _peer_pid(peer_port: int, our_port: int) -> int | None:
    """The process that owns the client end of a connection to us (None: not found)."""
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


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def _read_session(target: Path) -> dict[str, Any]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _session_has_us(data: dict[str, Any], server: _PooledServer) -> bool:
    entry = {"pid": os.getpid(), "port": server.server_address[1], "token": Link.token}
    return any(isinstance(s, dict) and all(s.get(k) == v for k, v in entry.items()) for s in data.get("servers") or [])


def _write_session(game_dir: Path, server: _PooledServer) -> None:
    """Adds this game's server to session.json, dropping our old entry and those of games gone."""
    target = game_dir / UI_DIR / SESSION_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    port = server.server_address[1]
    # "dev": the page re-reads its scripts for hot reload only from a source folder (a packed .sdkmod
    # can't change while the game runs)
    dev = isinstance(resources.files(__package__), Path)
    servers = [
        s for s in _read_session(target).get("servers") or []
        if isinstance(s, dict) and s.get("pid") != me and _pid_alive(s.get("pid"))
    ]
    servers.append({"pid": me, "port": port, "token": Link.token, "dev": dev})
    data = {"port": port, "token": Link.token, "dev": dev, "servers": servers}
    tmp = target.with_name(f"{target.name}.{me}.tmp")  # per process: two games may write at once
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(target)


def stop() -> None:
    server, Link.server = Link.server, None
    if server is not None:
        # shutdown() waits for the serve loop; do it off the game thread
        threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()


# endregion
# region Game thread


def say(text: str, *, error: bool = False) -> None:
    Link.message = {"id": Link.message["id"] + 1, "text": text, "error": error}
    (logging.error if error else logging.info)(f"[Mod Menu] {text}")
    Link.dirty = True


OPEN_FRESH_S = 3.0


def request_close() -> None:
    Link.close_request += 1
    Link.dirty = True


# Set by menu.py: closes the game's menu (posts Esc), for Back on a screen F7 opened
close_game_menu: Callable[[], None] | None = None


def request_open(mod_name: str | None = None, *, own_menu: bool = False) -> None:
    """Asks the page to open its screen. own_menu: we opened the game's menu for it (F7)."""
    Link.open_request = {"id": Link.open_request["id"] + 1, "mod": mod_name, "own": own_menu}
    Link.open_requested_at = time.monotonic()
    Link.dirty = True
    Link.trace.append(f"{Link.open_requested_at:.2f} request_open id={Link.open_request['id']}")


def _open_fresh() -> bool:
    return time.monotonic() - Link.open_requested_at < OPEN_FRESH_S


def _want_full() -> bool:
    """The snapshot needs the mods list: the screen shows, or is about to (F7)."""
    return Link.page_open or _open_fresh()


def _build_snapshot(full: bool) -> None:
    """full: with the mods list. Without it, only the header the page acts on while closed."""
    Link.dirty = False
    Link.snapshot_full = full
    Link.snapshot_fresh = _open_fresh()
    Link.snapshot_open_id = Link.open_request["id"] if Link.snapshot_fresh else -Link.open_request["id"]
    state: dict[str, Any] = {}
    if full:
        mods = []
        icons = hotload.H.icons
        for mod in model.mods():
            try:
                entry = model.mod_json(mod)
                # The module it came from (build_mod names the settings file after it), for its icon
                settings = getattr(mod, "settings_file", None)
                entry["id"] = mod_id = Path(settings).stem if settings is not None else ""
                if mod_id in icons:
                    entry["icon"] = icons[mod_id]
                mods.append(entry)
            except Exception as ex:  # noqa: BLE001 - one broken mod shouldn't hide the others
                mods.append({"name": mod.name, "desc": f"Couldn't read this mod's options: {ex}",
                             "status": "Error", "status_kind": "warn", "options": []})  # fmt: skip
        state["mods"] = mods
    state |= {
        "message": Link.message,
        "open": {**Link.open_request, "fresh": _open_fresh()},
        "close": Link.close_request,
        "capture": Link.capture,
        "menus": Link.menus_up,
        "menus_closed": Link.menus_closed,
        "updates": updates.snapshot(),
        "repos": repos.summary(),
    }
    Link.updates_rev = state["updates"]["rev"]
    Link.repos_rev = state["repos"]["rev"]
    data = json.dumps(state, separators=(",", ":"), default=str).encode("utf-8")
    if data != Link.snapshot:
        Link.revision += 1
        Link.snapshot = data


def _apply(action: dict[str, Any]) -> None:
    op = action.get("op")
    mod = model.find_mod(action.get("mod"))
    if op == "close_capture":
        Link.capture = None
        return
    if op == "ext":
        extensions.apply(action)
        return
    if op in ("updates_check", "updates_apply", "updates_restart", "updates_seen", "updates_load_now"):
        _apply_updates(op, action)
        return
    if isinstance(op, str) and op.startswith(("repo_", "mods_")):
        _apply_repos(op, action)
        return
    if op == "close_menus":
        # Back on a screen F7 opened: close the game's menu we opened for it too
        if Link.menus_up and close_game_menu is not None:
            close_game_menu()
        return
    if mod is None:
        return
    if op == "enable":
        say(model.set_enabled(mod, bool(action.get("value"))))
        return
    path = [str(p) for p in action.get("path") or []]
    option = model.resolve(mod, path)
    if option is None:
        say("That setting isn't there any more.", error=True)
        return
    match op:
        case "set":
            model.set_from_json(mod, option, action.get("value"))
        case "reset":
            model.reset(mod, option)
        case "press":
            model.press(option)
        case "capture":
            Link.capture = {"mod": mod.name, "path": path}
            # Only keys pressed from now on count
            Link.capture_held = keys.held_keys()
        case _:
            pass


def _apply_updates(op: str, action: dict[str, Any]) -> None:
    game_dir = Link.game_dir
    if game_dir is None:
        return
    if op == "updates_seen":
        # The Updates page showed these new mods: no badge for them again
        updates.mark_seen([str(i) for i in action.get("value") or [] if isinstance(i, str)])
    elif op == "updates_check":
        if not updates.check_async(game_dir):
            say("Already checking or downloading.")
    elif op == "updates_apply":
        install_new = [str(i) for i in action.get("value") or [] if isinstance(i, str)]
        if not updates.apply_async(game_dir, install_new):
            say("Already checking or downloading.")
    elif op == "updates_load_now":
        _load_staged(game_dir)
    elif op == "updates_restart":
        if updates.U.status != "ready":
            say("Nothing to install yet.", error=True)
        elif updates.mark_restart(game_dir):
            say("Restarting: the game closes, the updates install, and it starts again.")
            Link.quit_at = time.monotonic() + QUIT_DELAY_S
        else:
            say("Quit the game and start it again to finish the update.")


def _load_staged(game_dir: Path) -> None:
    """Load updates now: reloads every staged update whose mod can reload (the rest wait for the
    restart; the helper still moves every file in when the game closes)."""
    done, waiting = [], []
    staged = sorted(updates.staging(game_dir).glob("*.sdkmod"))
    if not staged:
        say("No downloaded updates are waiting.")
        return
    for path in staged:
        mod_id = path.stem
        if hotload.H.loaded_from.get(mod_id) == str(path):
            continue  # already running from there
        try:
            hotload.reload(game_dir, mod_id, path)
            done.append(mod_id)
        except Exception as ex:  # noqa: BLE001 - say why and carry on with the others
            waiting.append(mod_id)
            logging.warning(f"[Mod Menu] {mod_id} waits for the restart: {ex}")
    names = [m.name for i in done for m in hotload.mods_of(i)] or done
    text = f"Loaded {', '.join(names)} now." if done else "Nothing could load now."
    if waiting:
        text += f" At the restart: {', '.join(waiting)} (see the log for why)."
    say(text, error=not done)


def _try(action: Callable[[], str]) -> None:
    """Runs a load/reload/remove and shows what happened, or why it couldn't."""
    try:
        say(action())
    except Exception as ex:  # noqa: BLE001 - the reason is the message (a failed import, a refusal)
        say(str(ex), error=True)


def _str_ids(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(v) for v in values if isinstance(v, str) and hotload.MOD_ID.match(v)]


def _apply_repos(op: str, action: dict[str, Any]) -> None:
    """Discover, Repositories and the mods folder. Network work goes to repos.py's workers; loads
    and removals run here, on the game thread."""
    game_dir = Link.game_dir
    if game_dir is None:
        return
    value = action.get("value")
    match op:
        case "repo_refresh":
            if not repos.refresh_async():
                say("Already checking the repositories.")
            updates.check_async(game_dir)
        case "repo_add_clip":
            repos.add_from_clipboard_async()
        case "repo_add_confirm":
            repos.confirm_add()
        case "repo_add_cancel":
            repos.cancel_add()
        case "repo_remove" if isinstance(value, str):
            repos.remove_source(value)
        case "repo_install":
            for mod_id in _str_ids(value):
                repos.install_async(mod_id)
        case "repo_update":
            repos.update_async(_str_ids(value))
        case "repo_remove_mod":
            for mod_id in _str_ids(value):
                _try(lambda mod_id=mod_id: repos.remove_mod(mod_id))
        case "repo_images" if isinstance(value, list):
            repos.want_images([str(v) for v in value if isinstance(v, str)])
        case "repo_link" if isinstance(value, str):
            repos.open_link(value)
        case "mods_folder":
            hotload.open_folder(game_dir)
            say("Mods folder opened: drop .sdkmod files in, then press Load here.")
        case "mods_load":
            for mod_id in _str_ids(value):
                _try(lambda mod_id=mod_id: hotload.load(game_dir, mod_id))
            threading.Thread(target=hotload.publish_icons, args=(game_dir,), daemon=True).start()
        case "mods_reload":
            for mod_id in _str_ids(value):
                _try(lambda mod_id=mod_id: hotload.reload(game_dir, mod_id))
            threading.Thread(target=hotload.publish_icons, args=(game_dir,), daemon=True).start()
        case _:
            pass


def _quit_game() -> None:
    """Closes the game normally (it saves as usual), so the update helper can install."""
    import unrealsdk  # noqa: PLC0415
    from mods_base import ENGINE, get_pc  # noqa: PLC0415

    world = ENGINE.GameViewport.World
    system = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    system.QuitGame(world, get_pc(possibly_loading=True), 0, False)  # EQuitPreference::Quit


def _poll_capture() -> None:
    capture = Link.capture
    if capture is None or not keys.game_focused():
        return
    held = keys.held_keys()
    new = held - Link.capture_held
    Link.capture_held &= held
    if keys.vk_down(keys.VK_ESCAPE):
        Link.capture = None
        Link.dirty = True
        return
    if not new:
        return
    Link.capture = None
    key = keys.KEY_NAMES[min(new)]
    mod = model.find_mod(capture["mod"])
    option = model.resolve(mod, capture["path"]) if mod is not None else None
    if mod is not None and option is not None:
        try:
            model.set_from_json(mod, option, key)
            say(f"{option.display_name}: {keys.display_name(key)}")
        except model.ChangeError as ex:
            say(str(ex), error=True)
    Link.dirty = True


def tick(menus_up: bool) -> None:
    """Call every frame on the game thread, saying whether one of the game's menus is showing."""
    if Link.server is None:
        return
    # A stale open request must leave the snapshot even while the page sleeps, or it would act on
    # it whenever a menu next opens
    changed = menus_up != Link.menus_up or _open_fresh() != Link.snapshot_fresh
    if Link.menus_up and not menus_up:
        Link.menus_closed += 1
    Link.menus_up = menus_up
    while Link.actions:
        action = Link.actions.popleft()
        try:
            _apply(action)
        except model.ChangeError as ex:
            say(str(ex), error=True)
        except Exception as ex:  # noqa: BLE001
            logging.exception(ex)
            say(f"Something went wrong: {ex}", error=True)
        # An extension's action doesn't change this snapshot (and they come several a second)
        if action.get("op") != "ext":
            changed = True
    _poll_capture()
    extensions.tick(menus_up)
    if repos.pending_loads():
        # Mods the repositories' workers downloaded: load them here, on the game thread
        repos.drain()
        changed = True
    now = time.monotonic()
    if updates.U.revision != Link.updates_rev:
        changed = True
        if updates.U.manifest is not Link.manifest_seen:
            # BL4 Fix's release was read again: Discover lists its mods from it
            Link.manifest_seen = updates.U.manifest
            repos.official_checked()
    if repos.R.events + hotload.H.revision != Link.repos_rev:
        changed = True
    if Link.quit_at and now >= Link.quit_at:
        Link.quit_at = 0.0
        try:
            _quit_game()
        except Exception as ex:  # noqa: BLE001
            say(f"Couldn't close the game ({ex}): quit it yourself to finish the update.", error=True)
    if changed:
        Link.dirty = True
    # Rebuild right after a change (events: cheap and rare), otherwise only for a poll waiting on
    # this tick: the full list while the screen shows (at most every SNAPSHOT_S), else the header
    asked = Link.asked
    full = _want_full()
    if Link.dirty or (asked != Link.answered and (
            full != Link.snapshot_full or (full and now - Link.snapshot_at >= SNAPSHOT_S))):  # fmt: skip
        Link.snapshot_at = now
        _build_snapshot(full)
    if asked != Link.answered:
        with _cond:
            Link.answered = asked
            _cond.notify_all()


# endregion
