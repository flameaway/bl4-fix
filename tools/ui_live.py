"""
Live editing of the item card UI while the game runs (dev only, never shipped).

    python tools/ui_live.py install     write the dev pak (then restart the game once)
    python tools/ui_live.py serve       run the local server the dev pak talks to
    python tools/ui_live.py remove      delete the dev pak and refresh the release pak (then restart)

The dev pak is the normal item card pak plus a watcher script. Every second, each game page that
has loaded an item card fetches dev/ui_live/live.js from the server and runs it once each time it
changes, with `report` in scope; anything it returns or passes to report(...) is posted back here,
printed, and appended to .bridge/ui_live/reports.log. Use it for probes.

You don't need this for styling or layout: the card's own script and stylesheet are loose files
(OakGame/Content/uiresources/bl4direct/item_cards.{js,css}) that reload themselves when they
change. With the dev junction in place the mod copies mods/bl4_item_cards/card_page/ there every
second, so editing those files shows up in game within a few seconds. Only the page in the pak
(tools/gen_item_cards.py) needs `install` again and a game restart.

If the game can't reach the server, the watcher tries live.js as a loose file under
OakGame/Content/uiresources/bl4direct/live/. The card's outline shows which works: green = server,
yellow = loose file, red = neither.

The dev pak has its own file name and a higher number, so it wins over the release pak while it's
installed, and the mod's sync pauses the release pak (it sees another pak changing item cards).
`remove` brings it back.
"""

from __future__ import annotations

import argparse
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD_DIR = REPO / "mods" / "bl4_item_cards"
LIVE_DIR = REPO / "dev" / "ui_live"
REPORTS = REPO / ".bridge" / "ui_live" / "reports.log"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
DEV_PAK = "pakchunk0-Windows_4243_P.pak"
PORT = 8765

sys.path.insert(0, str(MOD_DIR))
import pak  # noqa: E402

WATCHER_JS = """
(function () {
    "use strict";
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
    setInterval(tick, 1000);
    tick();
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


def install(game: Path) -> None:
    item_cards = _item_cards()
    assets = MOD_DIR / "card_page"
    html = (assets / "item_card.html").read_text(encoding="utf-8")
    html += '\n<script src="coui://uiresources/bl4direct/live_watcher.js"></script>\n'
    files = {
        item_cards.CARD_PAGE: html.encode("utf-8"),
        "bl4direct/live_watcher.js": (WATCHER_JS % {"port": PORT}).encode("utf-8"),
    }
    # The card's script and stylesheet stay loose files (a pak copy would block their reloading)
    item_cards.publish_files(game)

    out = game_paks(game) / DEV_PAK
    out.parent.mkdir(parents=True, exist_ok=True)
    # Never change an existing pak while the game runs: it keeps the index from startup and would
    # read the new file at old offsets (raw script text on screen). Create it now if it's new,
    # otherwise stage it and let the mod's helper swap it in once the game has closed.
    data = pak.build_pak("../../../" + item_cards.UI_PREFIX, files, DEV_PAK)
    if out.exists() and out.read_bytes() != data:
        out.with_name(out.name + ".next").write_bytes(data)
        item_cards.apply_after_exit(out)
        print(f"staged {out.name}.next: it replaces the dev pak once the game is closed")
    elif not out.exists():
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    js = LIVE_DIR / "live.js"
    if not js.exists():
        js.write_text("// Runs in every page with an item card each time this file changes.\n", encoding="utf-8")
    print(f"wrote {out}\nrestart the game once, then run: python tools/ui_live.py serve")


def remove(game: Path) -> None:
    (game_paks(game) / DEV_PAK).unlink(missing_ok=True)
    # Bring the release pak up to date with what's in the repo, as the mod would on startup
    print(_item_cards().sync(game, enabled=True))
    print("removed the dev pak; restart the game to go back to the normal one")


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
    args = parser.parse_args()
    match args.action:
        case "install":
            install(args.game)
        case "serve":
            serve()
        case "remove":
            remove(args.game)


if __name__ == "__main__":
    main()
