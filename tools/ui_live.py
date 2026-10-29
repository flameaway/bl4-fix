"""
Live editing of the item card UI while the game runs (dev only, never shipped).

    python tools/ui_live.py install --bridge C   publish the watcher and run it through the UI Link
    python tools/ui_live.py serve                run the local server the watcher talks to
    python tools/ui_live.py remove --bridge C    stop the watcher (and retire the old 4243 dev pak)

No pak and no restart since 2026-10-29: the watcher is a loose script the BL4 UI Link runs in every
game page (key "uilive", views ANY), registered through the bridge in the running game. Every
second each page fetches dev/ui_live/live.js from the server and runs it once each time it
changes, with `report` in scope; anything it returns or passes to report(...) is posted back here,
printed, and appended to .bridge/ui_live/reports.log. Use it for probes. The registration lasts
until the game exits (or `remove`); run `install` again after a restart.

You don't need this for styling or layout: the card's own script and stylesheet are loose files
(OakGame/Content/uiresources/bl4direct/item_cards.{js,css}) that the link and the script reload
when they change, from the dev junction.

If the game can't reach the server, the watcher tries live.js as a loose file under
OakGame/Content/uiresources/bl4direct/live/. The card's outline shows which works: green = server,
yellow = loose file, red = neither.

Until 2026-10-29 this was a dev pak (4243) replacing item_card.html, which paused Item Cards while
it was installed. `remove` stages an old one's removal after the game exits.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD_DIR = REPO / "mods" / "bl4_item_cards"
LIVE_DIR = REPO / "dev" / "ui_live"
REPORTS = REPO / ".bridge" / "ui_live" / "reports.log"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
DEV_PAK = "pakchunk0-Windows_4243_P.pak"  # retired
WATCHER_PATH = "bl4direct/live_watcher.js"
LINK_KEY = "uilive"
PORT = 8765

sys.path.insert(0, str(MOD_DIR))
import pak  # noqa: E402

WATCHER_JS = """
(function () {
    "use strict";
    var linkNs = typeof bl4linkNs !== "undefined" ? bl4linkNs : null;
    if (window.bl4dLive) return;
    var HTTP = "http://127.0.0.1:%(port)d/";
    var LOOSE = "coui://uiresources/bl4direct/live/";
    var state = { js: null, mode: "none" };
    window.bl4dLive = state;

    function get(url, done) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState === 4) done(xhr.status, xhr.responseText);
        };
        try {
            xhr.open("GET", url, true);
            xhr.send();
        } catch (err) {
            done(-1, String(err));
        }
    }

    function report(value) {
        var text = typeof value === "string" ? value : JSON.stringify(value, null, 1);
        try {
            var xhr = new XMLHttpRequest();
            xhr.open("POST", HTTP + "report", true);
            xhr.send("[" + (document.title || location.href) + "] " + text);
        } catch (err) {}
    }
    window.bl4dReport = report;

    function outline() {
        var colour = { http: "#3c3", loose: "#ec3", none: "#e33" }[state.mode];
        var style = document.getElementById("bl4d_live_mode");
        if (!style) {
            style = document.createElement("style");
            style.id = "bl4d_live_mode";
            document.head.appendChild(style);
        }
        style.textContent = ".item_card_ctr { outline: 0.3rem solid " + colour + "; }";
    }

    function runJs(text) {
        if (text === state.js) return;
        state.js = text;
        try {
            var result = new Function("report", text)(report);
            if (result !== undefined) report(result);
        } catch (err) {
            report("live.js failed: " + err + "\\n" + (err && err.stack));
        }
    }

    function poll(base, mode, failed) {
        var stamp = base === HTTP ? "?t=" + Date.now() : "";
        get(base + "live.js" + stamp, function (status, js) {
            if (status !== 200 && !(status === 0 && js)) return failed();
            if (state.mode !== mode) {
                state.mode = mode;
                outline();
                report("watcher connected via " + mode);
            }
            runJs(js);
        });
    }

    function tick() {
        poll(HTTP, "http", function () {
            poll(LOOSE, "loose", function () {
                if (state.mode !== "none") return;
                outline();
            });
        });
    }

    outline();
    var timer = setInterval(tick, 1000);
    tick();
    if (linkNs && linkNs.ext) {
        linkNs.ext.teardowns["uilive"] = function () {
            clearInterval(timer);
            var style = document.getElementById("bl4d_live_mode");
            if (style) style.parentNode.removeChild(style);
            window.bl4dLive = null;
        };
    }
})();
"""


def game_paks(game: Path) -> Path:
    return game / "OakGame" / "Content" / "Paks" / "~mods"


def _item_cards():  # noqa: ANN202
    """The mod's card_page module, loaded without its SDK-dependent parent package."""
    import types

    package = types.ModuleType("bl4_item_cards")
    package.__path__ = [str(MOD_DIR)]  # type: ignore[attr-defined]
    sys.modules["bl4_item_cards"] = package
    from bl4_item_cards import card_page  # noqa: PLC0415

    return card_page


# Run in the game through the bridge: any mod's uilink copy registers with the one shared link
REGISTER = """
import sys
link = next(m for n, m in list(sys.modules.items()) if n.endswith(".uilink") and hasattr(m, "register"))
link.register("uilive", "coui://uiresources/%s", lambda: {}, lambda _a: None, views=(link.ANY,), mod="ui_live")
print("ui_live watcher registered; link:", link.status())
""" % WATCHER_PATH
UNREGISTER = """
import sys
link = next(m for n, m in list(sys.modules.items()) if n.endswith(".uilink") and hasattr(m, "register"))
link.unregister("uilive")
print("ui_live watcher unregistered")
"""


def _bridge(bridge: str | None, code: str) -> None:
    if not bridge:
        print("no --bridge id given: run this in the game yourself (python tools/bridge.py <id> \"...\"):\n" + code)
        return
    subprocess.run([sys.executable, str(REPO / "tools" / "bridge.py"), bridge, code], check=False)  # noqa: S603


def install(game: Path, bridge: str | None) -> None:
    target = game / _item_cards().UI_PREFIX / WATCHER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(WATCHER_JS % {"port": PORT}, encoding="utf-8")
    tmp.replace(target)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    js = LIVE_DIR / "live.js"
    if not js.exists():
        js.write_text("// Runs in every game page each time this file changes.\n", encoding="utf-8")
    print(f"wrote {target}")
    _bridge(bridge, REGISTER)
    print("then run: python tools/ui_live.py serve")


def remove(game: Path, bridge: str | None) -> None:
    item_cards = _item_cards()
    _bridge(bridge, UNREGISTER)
    (game / item_cards.UI_PREFIX / WATCHER_PATH).unlink(missing_ok=True)
    old = game_paks(game) / DEV_PAK
    if old.exists():
        # An existing pak is never touched while a game may run: removed once none does
        old.with_name(old.name + ".remove").write_bytes(b"")
        item_cards.apply_after_exit(old)
        print(f"{old.name} goes once the game is closed")
    print("removed the watcher")


class Handler(BaseHTTPRequestHandler):
    def _headers(self, status: int, length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        name = self.path.split("?", 1)[0].lstrip("/")
        target = (LIVE_DIR / name).resolve()
        if target.parent != LIVE_DIR.resolve() or not target.is_file():
            self._headers(404)
            return
        data = target.read_bytes()
        self._headers(200, len(data))
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        line = f"{time.strftime('%H:%M:%S')} {body}"
        print(line, flush=True)
        REPORTS.parent.mkdir(parents=True, exist_ok=True)
        with REPORTS.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
        self._headers(204)

    def log_message(self, *_: object) -> None:
        pass


def serve() -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"serving {LIVE_DIR} on http://127.0.0.1:{PORT}/ (reports -> {REPORTS})", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["install", "serve", "remove"])
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME)
    parser.add_argument("--bridge", help="bridge id of the running game (register / unregister the watcher there)")
    args = parser.parse_args()
    match args.action:
        case "install":
            install(args.game, args.bridge)
        case "serve":
            serve()
        case "remove":
            remove(args.game, args.bridge)


if __name__ == "__main__":
    main()
