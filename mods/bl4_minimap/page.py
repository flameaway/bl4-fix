"""
Page mode: the minimap drawn in the game's own HUD page (Coherent Gameface) instead of UMG.

The game thread only gathers what to show (the same data as the UMG minimap: markers, the map's
icons, the route, settings) and keeps the player's position; the page (ui/minimap.js, run in the
HUD by the BL4 UI Link) draws everything each frame itself: the map from saved tiles (tiles.py's
page mode, PNGs it loads over our server), icons projected around the player, the frame. No
widget, no display render target, no picture captures, no per-frame engine calls.

Measured before building it (2026-11-02, game 33460):
- The HUD page draws in the same frame as UMG (a bar each placed from the clock: within 1 ms in 6
  screenshots).
- `data_def_compass_0.scrollposx` (the HUD compass) maps to yaw by `C_SLOPE * yaw + C_ZERO` (the
  page checks it against our samples and corrects the offset), but it's a frame behind the camera
  (the page draws 2-4 ms after our frame tick, in the same frame), so the page turns the map from it
  half its last frame's change further (ui/minimap.js COMPASS_LEAD: the numbers). Our samples
  predicted forward were closer while turning but overshot when a turn stopped (2026-11-02).
- Position: a sample every frame, sent by long-poll; the page gets it a frame later (Gameface hands
  an XHR's answer over at its next frame) and predicts forward from it (p50 3 cm, p90 15 cm off the
  real position at 20 Hz; up to ~1.4 m when starting or stopping).
- In the HUD page only coui:// and http:// images load; `Texture2D'...'` / `TextureRenderTarget2D'...'`
  URLs (which work in the menus page) show nothing, so the game's art goes by its coui:// PNG and
  tiles over HTTP.

The server: one worker thread, raw sockets and select (no thread per request: they fought the
game for the GIL in the Mod Menu), woken by a socket pair when the game thread stores a sample, so
an answer goes out at once. Requests carry a random token and are refused with a web page's
Origin (the game's pages send none). The game thread answers a waiting long-poll itself when it
stores a sample (one small non-blocking send); the worker answers the rest. Answers:
- `GET /p?t=&q=<last sample>&r=<revisions>`: waits (up to WAIT_S) for a newer sample or a changed
  section, then `{"q", "p": sample, <section>: {"rev", "v"}...}` with only the sections the page
  doesn't have yet.
- `GET /t/<i>_<j>.png?t=&f=1`: a tile of the current session (f=1: a preview under fresh/).
"""

from __future__ import annotations

import json
import re
import secrets
import select
import socket
import threading
import time
import zlib
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import fog, uilink

KEY = "bl4minimap"
SCRIPT_URL = "coui://uiresources/bl4minimap/minimap.js"
UI_DIR = "OakGame/Content/uiresources/bl4minimap"
UI_FILES = ("minimap.js",)
PUBLISH_S = 1.0  # dev: how often the page files are compared with the source
WAIT_S = 0.5  # a long-poll waits at most this long for something new
SECTIONS = ("cfg", "tiles", "slow", "fast", "fog")
MAX_HEAD = 8192
TILE_NAME = re.compile(r"^/t/(-?\d{1,4}_-?\d{1,4})\.png$")
FOG_ROUTE = "/f.png"


class Feed:
    """The server and what it hands out. Fields set by the game thread are whole-object swaps."""

    thread: threading.Thread | None = None
    stop = False
    port = 0
    token = ""
    wake_r: socket.socket | None = None
    wake_w: socket.socket | None = None
    seq = 0
    sample = b"null"
    revs: dict[str, int] = {}  # noqa: RUF012
    data: dict[str, bytes] = {}  # noqa: RUF012
    last: dict[str, Any] = {}  # noqa: RUF012  # the objects data was made from (compare before dumping)
    folder: Path | None = None  # the tile session's folder
    # Fog of war: (revision, explored grid, width, height, opacity 0-1) from the game thread; the
    # worker makes the PNG when the page asks for that revision (`_fog`)
    fog: tuple[int, bytes, int, int, float] | None = None
    fog_png: tuple[int, bytes] | None = None  # (revision, PNG) the worker made last
    # Long-polls waiting for news: socket -> [since, q, revs]. Taken out under `lock` by whichever
    # thread answers: the game thread when it stores a sample (one small non-blocking send, no
    # hand-over to the worker), the worker when one has waited WAIT_S or a request has news at once
    lock = threading.Lock()
    waiting: dict[socket.socket, list[Any]] = {}  # noqa: RUF012
    answers = 0
    errors = 0
    debug = ""


def running() -> bool:
    return Feed.thread is not None and Feed.thread.is_alive() and not Feed.stop


def start() -> None:
    if running():
        return
    Feed.stop = False
    Feed.token = secrets.token_hex(8)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.setblocking(False)
    Feed.port = srv.getsockname()[1]
    Feed.wake_r, Feed.wake_w = socket.socketpair()
    Feed.wake_r.setblocking(False)
    Feed.wake_w.setblocking(False)
    Feed.revs = {name: 0 for name in SECTIONS}
    Feed.data = {name: b"null" for name in SECTIONS}
    Feed.last = {}
    Feed.waiting = {}
    Feed.thread = threading.Thread(target=_serve, args=(srv,), name="bl4_minimap page feed", daemon=True)
    Feed.thread.start()


def stop() -> None:
    Feed.stop = True
    _wake()
    Feed.thread = None


def _wake() -> None:
    w = Feed.wake_w
    if w is None:
        return
    try:
        w.send(b"x")
    except OSError:
        pass  # (full: the worker is awake anyway)


def set_sample(t: float, x: float, y: float, z: float, yaw: float, visible: bool) -> None:
    """
    Stores a position sample and answers the waiting long-poll with it right here: a few hundred
    bytes, one non-blocking send. Waking the worker for it instead cost 0.13 ms a sample (the game
    thread then waited for the GIL while the worker answered; game 33460, 2026-11-02).
    """
    Feed.seq += 1
    Feed.sample = (
        f'{{"t":{t * 1000:.1f},"x":{x:.1f},"y":{y:.1f},"z":{z:.1f},"yaw":{yaw:.3f},"vis":{1 if visible else 0}}}'
    ).encode()
    if not Feed.waiting:
        return
    with Feed.lock:
        taken = list(Feed.waiting.items())
        Feed.waiting.clear()
    for conn, (_, q, revs) in taken:
        _answer(conn, q, revs)


def set_section(name: str, value: Any) -> None:
    """Hands the page a section; dumped only when it changed. It goes out with the next sample (or
    the worker's next answer): nothing is woken for it."""
    if Feed.last.get(name, ...) == value:
        return
    Feed.last[name] = value
    Feed.data[name] = json.dumps(value, separators=(",", ":")).encode()
    Feed.revs[name] = Feed.revs.get(name, 0) + 1


# region Server (worker thread)


def _answer(conn: socket.socket, have_q: int, have: list[int]) -> None:
    """The long-poll answer: the latest sample and the sections the page doesn't have yet. Either
    thread; a non-blocking send (the socket's buffer takes it whole in practice)."""
    revs = Feed.revs
    parts = [b'{"q":', str(Feed.seq).encode(), b',"p":', Feed.sample]
    for i, name in enumerate(SECTIONS):
        rev = revs.get(name, 0)
        if i >= len(have) or have[i] != rev:
            parts += [b',"', name.encode(), b'":{"rev":', str(rev).encode(), b',"v":', Feed.data.get(name, b"null"), b"}"]
    parts.append(b"}")
    msg = _message(200, b"".join(parts), b"application/json", b"no-store")
    try:
        sent = conn.send(msg)
        if sent < len(msg):
            conn.settimeout(1.0)
            try:
                conn.sendall(msg[sent:])
            finally:
                conn.setblocking(False)
    except OSError:
        try:
            conn.close()  # (the worker drops it once select stops listing it)
        except OSError:
            pass
        return
    Feed.answers += 1


def _has_news(have_q: int, have: list[int]) -> bool:
    if Feed.seq > have_q:
        return True
    revs = Feed.revs
    return any(i >= len(have) or have[i] != revs.get(name, 0) for i, name in enumerate(SECTIONS))


def _message(status: int, body: bytes, kind: bytes, cache: bytes) -> bytes:
    return (
        b"HTTP/1.1 " + (b"200 OK" if status == 200 else b"404 Not Found")
        + b"\r\nAccess-Control-Allow-Origin: *\r\nContent-Type: " + kind
        + b"\r\nCache-Control: " + cache + b"\r\nConnection: keep-alive\r\nContent-Length: "
        + str(len(body)).encode() + b"\r\n\r\n" + body
    )  # fmt: skip


def _send(conn: socket.socket, status: int, body: bytes, kind: bytes, cache: bytes) -> None:
    """Worker only: a whole answer (a tile is ~400 KB: blocks until sent; localhost, ~1 ms)."""
    conn.settimeout(2.0)
    try:
        conn.sendall(_message(status, body, kind, cache))
    finally:
        conn.setblocking(False)
    Feed.answers += 1


def _query(path: str) -> tuple[str, dict[str, str]]:
    route, _, query = path.partition("?")
    params = {}
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k:
            params[k] = v
    return route, params


def _tile(conn: socket.socket, name: str, fresh: bool) -> None:
    folder = Feed.folder
    body = None
    if folder is not None:
        try:
            body = ((folder / "fresh") if fresh else folder).joinpath(f"{name}.png").read_bytes()
        except OSError:
            body = None
    if body is None:
        _send(conn, 404, b"", b"text/plain", b"no-store")
    else:
        # The page asks with the tile's version in the query: a new picture is a new URL
        _send(conn, 200, body, b"image/png", b"max-age=86400")


def _fog(conn: socket.socket) -> None:
    """Worker: the fog layer for the current explored grid (unexplored = fog.FOG_RGB at the strength's
    opacity, explored = clear). Its URL carries the revision, so each revision is fetched once."""
    layer = Feed.fog
    if layer is None:
        _send(conn, 404, b"", b"text/plain", b"no-store")
        return
    rev, grid, width, height, opacity = layer
    made = Feed.fog_png
    if made is None or made[0] != rev:
        made = Feed.fog_png = (rev, fog.layer_png(grid, width, height, opacity))
    _send(conn, 200, made[1], b"image/png", b"max-age=86400")


def set_fog(grid: bytes | None, width: int, height: int, opacity: float, square: tuple[float, ...] | None) -> None:
    """Game thread: the explored grid for the page's fog layer (None: no fog). Only a changed grid
    (or strength, or square) makes a new revision."""
    if grid is None or square is None:
        if Feed.fog is not None:
            Feed.fog = None
            set_section("fog", None)
        return
    old = Feed.fog
    if old is not None and old[1] == grid and old[4] == opacity and (Feed.last.get("fog") or {}).get("sq") == list(square):
        return
    rev = (old[0] + 1) if old is not None else 1
    Feed.fog = (rev, grid, width, height, opacity)
    set_section("fog", {"rev": rev, "w": width, "h": height, "sq": list(square)})


def _serve(srv: socket.socket) -> None:
    buffers: dict[socket.socket, bytes] = {}  # open connections -> bytes received, not a whole request yet
    wake = Feed.wake_r
    try:
        while not Feed.stop:
            with Feed.lock:
                deadlines = [since + WAIT_S for since, _, _ in Feed.waiting.values()]
            timeout = 0.25 if not deadlines else max(0.0, min(min(deadlines) - time.monotonic(), 0.25))
            for s in [s for s in buffers if s.fileno() < 0]:
                _drop(s, buffers)  # (closed by the game thread after a failed send)
            try:
                readable, _, _ = select.select([srv, wake, *buffers], [], [], timeout)
            except OSError:
                readable = []
            for s in readable:
                if s is wake:
                    try:
                        while wake.recv(4096):
                            pass
                    except OSError:
                        pass
                    continue
                if s is srv:
                    try:
                        conn, _ = srv.accept()
                        conn.setblocking(False)
                        buffers[conn] = b""
                    except OSError:
                        pass
                    continue
                try:
                    chunk = s.recv(8192)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    _drop(s, buffers)
                    continue
                data = buffers.get(s, b"") + chunk
                if len(data) > MAX_HEAD and b"\r\n\r\n" not in data:
                    _drop(s, buffers)
                    continue
                ok = True
                while ok and b"\r\n\r\n" in data:
                    head, _, data = data.partition(b"\r\n\r\n")
                    ok = _request(s, head)
                if not ok:
                    _drop(s, buffers)
                elif s in buffers:
                    buffers[s] = data
            # Long-polls that waited WAIT_S, or with news no sample carried (sections while hidden)
            now = time.monotonic()
            due = []
            with Feed.lock:
                for s, (since, q, revs) in list(Feed.waiting.items()):
                    if now - since >= WAIT_S or _has_news(q, revs):
                        due.append((s, q, revs))
                        del Feed.waiting[s]
            for s, q, revs in due:
                _answer(s, q, revs)
    except Exception as ex:  # noqa: BLE001
        Feed.errors += 1
        logging.error(f"[Minimap] page feed stopped: {ex!r}")
    finally:
        with Feed.lock:
            Feed.waiting.clear()
        for s in buffers:
            try:
                s.close()
            except OSError:
                pass
        srv.close()


def _drop(s: socket.socket, buffers: dict[socket.socket, bytes]) -> None:
    buffers.pop(s, None)
    with Feed.lock:
        Feed.waiting.pop(s, None)
    try:
        s.close()
    except OSError:
        pass


def _request(conn: socket.socket, head: bytes) -> bool:
    """Handles one request head. False: drop the connection."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2 or parts[0] != "GET":
        return False
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "origin" and not value.strip().lower().startswith("coui://"):
            return False  # a web page (the game's own pages send no Origin)
    route, params = _query(parts[1])
    if not Feed.token or params.get("t") != Feed.token:
        _send(conn, 404, b"", b"text/plain", b"no-store")
        return True
    if route == "/p":
        Feed.debug = params.get("d", "")  # the page's own counters (dev)
        try:
            q = int(params.get("q", "0") or 0)
            revs = [int(r) for r in params.get("r", "").split(".") if r]
        except ValueError:
            q, revs = 0, []
        if _has_news(q, revs):
            _answer(conn, q, revs)
        else:
            with Feed.lock:
                Feed.waiting[conn] = [time.monotonic(), q, revs]
        return True
    if route == FOG_ROUTE:
        _fog(conn)
        return True
    match = TILE_NAME.match(route)
    if match:
        _tile(conn, match.group(1), params.get("f") == "1")
        return True
    _send(conn, 404, b"", b"text/plain", b"no-store")
    return True


# endregion
# region The page script


class Page:
    game_dir: Path | None = None
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012
    published_at = 0.0
    packed = False
    registered = False
    # The script's URL carries a hash of its text (?v=). The link runs a script again only when its
    # URL changes unless its owner copy is a dev one, and the owner is often another mod's release
    # copy: with a plain URL, a page that ran the installed script kept it after the repo's was
    # loaded (no spread, no live fog; 2026-11-05)
    script_hash = ""


def state() -> dict[str, Any]:
    """What the page asks the UI Link for: where our feed is."""
    return {"port": Feed.port, "token": Feed.token, "on": running()}


def register() -> None:
    if Page.registered:
        return
    Page.game_dir = uilink.GAME_DIR
    publish(force=True)
    _register()
    Page.registered = True


def _register() -> None:
    url = f"{SCRIPT_URL}?v={Page.script_hash}"
    uilink.register(KEY, url, state, lambda _action: None, views=(uilink.HUD,), mod="Minimap")


def unregister() -> None:
    if not Page.registered:
        return
    uilink.unregister(KEY)
    uilink.stop()  # the link's server goes once no script of any mod is left
    Page.registered = False


def publish(*, force: bool = False) -> None:
    """Copies the page script to uiresources/bl4minimap/, touching only files that changed."""
    now = time.monotonic()
    if Page.game_dir is None or (not force and (Page.packed or now - Page.published_at < PUBLISH_S)):
        return
    Page.published_at = now
    root = resources.files(__package__)
    from_source = isinstance(root, Path)
    Page.packed = not from_source
    changed = False
    for name in UI_FILES:
        source = root / "ui" / name
        try:
            if from_source:
                st = source.stat()
                stamp = (st.st_size, st.st_mtime_ns)
                if not force and Page.published.get(name) == stamp:
                    continue
                Page.published[name] = stamp
            content = source.read_bytes()
            if name == UI_FILES[0]:
                Page.script_hash = f"{zlib.crc32(content):08x}"
            target = Page.game_dir / UI_DIR / name
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(target)
            changed = True
        except OSError as ex:
            Page.published.pop(name, None)
            logging.dev_warning(f"[Minimap] couldn't publish {name}: {ex}")
    if changed and from_source and not force and Page.registered:
        _register()


# endregion
# region Art


_URLS: dict[str, str] = {}


def art_url(path: str | None) -> str | None:
    """The coui:// (or DLC scheme://) PNG of one of the game's UI textures, by its asset path:
    `/Game/uiresources/a/b.b` -> `coui://uiresources/a/b.png`,
    `/Game/DLC/Cowbell/uiresources/a/b.b` -> `cowbell://uiresources/a/b.png`."""
    if not path:
        return None
    url = _URLS.get(path)
    if url is None:
        base = path.split(".", 1)[0]
        lower = base.lower()
        if lower.startswith("/game/uiresources/"):
            url = "coui://uiresources/" + base[len("/game/uiresources/"):] + ".png"
        elif lower.startswith("/game/dlc/") and "/uiresources/" in lower:
            dlc, _, rest = base[len("/game/dlc/"):].partition("/")
            url = dlc.lower() + "://" + rest + ".png"
        else:
            url = ""
        if len(_URLS) > 512:
            _URLS.clear()
        _URLS[path] = url
    return url or None


def css_color(rgba: tuple[float, ...]) -> str:
    """A UMG (linear) colour as CSS (sRGB)."""

    def enc(v: float) -> int:
        v = min(1.0, max(0.0, float(v)))
        s = v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
        return round(s * 255)

    alpha = float(rgba[3]) if len(rgba) > 3 else 1.0
    return f"rgba({enc(rgba[0])},{enc(rgba[1])},{enc(rgba[2])},{alpha:.3g})"


# endregion
