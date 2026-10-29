"""
Offline checks of shared/uilink (no game): python dev/uilink_test.py

A fake game folder in a temp dir, with the game's base pak hard-linked in (so the debug.js pin is
checked against the real file), a stubbed SDK, and two copies of the module loaded under different
package names to check the hand-over between copies.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "shared" / "uilink"
GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
BASE_PAK = "pakchunk0-Windows_0_P.pak"

FAILS: list[str] = []


def ok(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def stub_sdk(settings: Path) -> list[str]:
    logged: list[str] = []
    sdk = types.ModuleType("unrealsdk")
    log = types.ModuleType("unrealsdk.logging")
    for name in ("info", "error", "warning", "dev_warning"):
        setattr(log, name, lambda msg, _n=name: logged.append(f"{_n}: {msg}"))
    log.exception = lambda ex: logged.append(f"exception: {ex!r}")
    sdk.logging = log
    sys.modules["unrealsdk"] = sdk
    sys.modules["unrealsdk.logging"] = log
    mb = types.ModuleType("mods_base")
    mb.SETTINGS_DIR = settings
    sys.modules["mods_base"] = mb
    return logged


def load_copy(name: str, folder: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, folder / "__init__.py", submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def get(port: int, token: str, path: str) -> tuple[int, str]:
    sep = "&" if "?" in path else "?"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}{sep}t={token}", timeout=3) as r:  # noqa: S310
            return r.status, r.read().decode()
    except urllib.error.HTTPError as ex:
        return ex.code, ""


def post(port: int, token: str, body: dict) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/action?t={token}", data=json.dumps(body).encode(), method="POST")  # noqa: S310
    with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310
        return r.status


def wait(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Ticker:
    """Calls tick() every 10 ms on a thread standing in for the game thread."""

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module
        self.running = True
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        while self.running:
            self.module.tick()
            time.sleep(0.01)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="uilink_test_"))
    try:
        game = tmp / "game"
        paks = game / "OakGame" / "Content" / "Paks"
        (paks / "~mods").mkdir(parents=True)
        os.link(GAME / "OakGame" / "Content" / "Paks" / BASE_PAK, paks / BASE_PAK)
        settings = game / "sdk_mods" / "settings"
        settings.mkdir(parents=True)
        logged = stub_sdk(settings)

        # Copy A (the current VERSION) and copy B (same code, one VERSION newer)
        a_dir = tmp / "a" / "uilink"
        b_dir = tmp / "b" / "uilink"
        shutil.copytree(SOURCE, a_dir, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SOURCE, b_dir, ignore=shutil.ignore_patterns("__pycache__"))
        text = (b_dir / "__init__.py").read_text(encoding="utf-8")
        version = int(re.search(r"\nVERSION = (\d+)\n", text).group(1))
        (b_dir / "__init__.py").write_text(text.replace(f"\nVERSION = {version}\n", f"\nVERSION = {version + 1}\n"), encoding="utf-8")

        a = load_copy("moda_uilink", a_dir)
        staged: list[Path] = []
        a._apply_after_exit = lambda target: staged.append(target) or True  # never start PowerShell here
        hub = a._hub()
        ok(hub.pak_at_start is False, "fresh install: no pak at start")

        acted: list[dict] = []
        state = {"n": 1}
        a.register("builds", "coui://uiresources/bl4builds/builds.js", lambda: dict(state), acted.append, views=(a.MENUS,), mod="Build Presets")
        a.register("hud", "coui://uiresources/x/hud.js", lambda: {}, lambda _: None, views=(a.HUD,))
        ok(hub.owner is a, "first copy owns the link")
        ticker = Ticker(a)
        ok(wait(lambda: hub.server is not None), "server started on the first tick")
        port, token = hub.server.server_address[1], hub.token
        link = game / a.UI_DIR / a.LINK_FILE
        ok(wait(lambda: link.is_file() and json.loads(link.read_text())["servers"][-1]["port"] == port), "link.json names the server")
        ok(wait(lambda: (game / a.UI_DIR / "boot.js").is_file() and (game / a.UI_DIR / "hub.js").is_file()), "boot.js and hub.js published")
        ok(wait(lambda: hub.status["state"] != "checking"), "pak check finished")
        ok(hub.status["state"] == "next_start", f"fresh install -> next_start ({hub.status})")
        ours = a.pak_path(game)
        ok(ours.is_file(), "pak created (nothing mounted it yet)")
        ok(not a.live(), "not live in the session that created it")

        # The pak holds exactly the shim, and the game's own debug.js still wins among the base paks
        sys.path.insert(0, str(a_dir))
        entry = a.pak.winning_entry([ours], a.UI_PREFIX + a.SHIM)
        ok(entry is not None, "pak holds _shared/js/debug.js")
        ok(a.pak.patch_number(ours) == 4251, "pak name parses as patch 4251 (beats the game's _0_P.._23_P)")

        status, body = get(port, token, "/hello")
        ok(status == 200, "/hello from our own process: 200")
        status, body = get(port, "wrong", "/ext")
        ok(status == 404, "wrong token: 404")
        status, body = get(port, token, "/ext?view=view_status_menus")
        ok(json.loads(body) == {"builds": "coui://uiresources/bl4builds/builds.js"}, f"menus listing ({body})")
        status, body = get(port, token, "/ext?view=HUD")
        ok(json.loads(body) == {"hud": "coui://uiresources/x/hud.js"}, f"HUD listing ({body})")
        status, body = get(port, token, "/ext?k=builds&open=1&view=view_status_menus")
        snap = json.loads(body)
        ok(snap.get("state") == {"n": 1} and "rev" in snap, f"state answered ({body})")
        status, body = get(port, token, f"/ext?k=builds&v={snap['rev']}&view=view_status_menus")
        ok(json.loads(body).get("same") == snap["rev"], "unchanged state: 'same'")
        ok(a.polling("builds") and a.page_open("builds", 3.0) is False, "polling true; page_open follows the last poll's open=0")
        ok(wait(lambda: a.menus_up()), "menus page polled: menus_up")
        state["n"] = 2
        a.changed("builds")
        status, body = get(port, token, f"/ext?k=builds&v={snap['rev']}")
        ok(json.loads(body).get("state") == {"n": 2}, "changed() -> new state")
        post(port, token, {"op": "ext", "ext": "builds", "do": "save", "n": 3})
        ok(wait(lambda: acted and acted[-1].get("do") == "save"), "action reached act() on the tick")
        before = len(logged)
        post(port, token, {"op": "log", "text": "hello from the page"})
        ok(wait(lambda: any("hello from the page" in m for m in logged[before:])), "page log reaches the SDK log (dev)")
        ok(wait(lambda: not a.menus_up(), 2.0), "menus page stopped polling: menus down")
        ok(a.menus_closed() >= 1, "menus_closed counted")
        # Esc out of one screen and straight into another (F8, Esc, F9): the cursor drop counts the
        # close at once, before the page has been quiet for MENUS_POLL_S
        pc = types.SimpleNamespace(bShowMouseCursor=True)
        sys.modules["mods_base"].get_pc = lambda possibly_loading=False: pc
        get(port, token, "/ext?k=builds&view=view_status_menus")
        ok(wait(lambda: a.menus_up() and hub.cursor_was), "menus up again, cursor shown")
        before = a.menus_closed()
        pc.bShowMouseCursor = False
        ok(wait(lambda: a.menus_closed() > before, 0.3) and a.menus_up(), "cursor hidden: close counted while the page still polls")
        del sys.modules["mods_base"].get_pc

        # Copy B, newer, takes over: new server, registrations kept, old port gone from link.json
        b = load_copy("modb_uilink", b_dir)
        b._apply_after_exit = a._apply_after_exit
        b.register("tidypause", "coui://uiresources/bl4tidypause/pause.js", lambda: {"news": "right"}, lambda _: None, views=(b.MENUS,))
        ok(hub.owner is b, "newer copy took over")
        ok(wait(lambda: hub.server is not None and hub.server.server_address[1] != port), "new server")
        port2, token2 = hub.server.server_address[1], hub.token
        ok(wait(lambda: [s["port"] for s in json.loads(link.read_text())["servers"]] == [port2]), "link.json: only the new server")
        status, body = get(port2, token2, "/ext?view=view_status_menus")
        ok(set(json.loads(body)) == {"builds", "tidypause"}, f"both scripts listed by the new server ({body})")
        status, body = get(port2, token2, "/ext?k=builds")
        ok(json.loads(body).get("state") == {"n": 2}, "old copy's registration still served")
        # The older copy registering again doesn't take the link back
        a.register("builds", "coui://uiresources/bl4builds/builds.js", lambda: dict(state), acted.append, views=(a.MENUS,))
        ok(hub.owner is b, "older copy doesn't take over")

        # Unregister: the listing drops it
        a.unregister("builds")
        status, body = get(port2, token2, "/ext?view=view_status_menus")
        ok(set(json.loads(body)) == {"tidypause"}, "unregistered script leaves the listing")

        # Fallback to the Mod Menu's extensions while not live
        fake = types.ModuleType("bl4_mod_menu.extensions")
        fake.reg = {}
        fake.register = lambda key, script, st, act: fake.reg.__setitem__(key, script)
        fake.unregister = lambda key: fake.reg.pop(key, None)
        fake.changed = lambda key: None
        fake.polling = lambda key, within=3.0: key in fake.reg
        fake.page_open = lambda key, within=3.0: False
        ok(not b.reachable("tidypause"), "not live, no Mod Menu: not reachable")
        sys.modules["bl4_mod_menu.extensions"] = fake
        ok(wait(lambda: "tidypause" in fake.reg), "not live: menu scripts handed to the Mod Menu")
        ok("hud" not in fake.reg, "HUD scripts aren't handed to the Mod Menu")
        ok(b.reachable("tidypause") and not b.reachable("hud") and not b.reachable("nope"),
           "reachable: through the Mod Menu while not live; HUD / unknown scripts aren't")
        fake_web = types.ModuleType("bl4_mod_menu.web")
        fake_web.Link = types.SimpleNamespace(menus_up=True, menus_closed=41)
        sys.modules["bl4_mod_menu.web"] = fake_web
        ok(b.menus_up() is True and b.menus_closed() == 41, "while the Mod Menu carries the scripts: its menu signals")
        del sys.modules["bl4_mod_menu.web"]
        b.unregister("tidypause")
        ok("tidypause" not in fake.reg, "unregister takes it back from the Mod Menu")
        ok(b.menus_closed() == hub.menus_closed, "nothing handed over: the link's own menu signals")
        del sys.modules["bl4_mod_menu.extensions"]
        ok(b.display_name("T") == "T" and b.display_name("Five") == "5" and b.display_name("SpaceBar") == "Space"
           and b.display_name(None) == "Unbound", "display_name")

        # Second start: pak there at start and current -> live
        ticker.running = False
        time.sleep(0.05)
        b._stop_server(hub)
        del sys.modules[a.HUB]
        c = load_copy("modc_uilink", a_dir)
        c._apply_after_exit = a._apply_after_exit
        hub2 = c._hub()
        ok(hub2.pak_at_start is True, "second start: pak there at start")
        c.register("builds", "coui://uiresources/bl4builds/builds.js", lambda: {}, lambda _: None)
        ticker = Ticker(c)
        ok(wait(lambda: hub2.status["state"] != "checking"), "pak check finished")
        ok(c.live() and hub2.status["state"] == "live", f"second start: live ({hub2.status})")
        ok(c.reachable("builds") and not c.reachable("nope"), "live: registered scripts are reachable")

        # Another mod's pak replaces debug.js too: conflict, ours staged for removal
        other = paks / "~mods" / "pakchunk0-Windows_9000_P.pak"
        other.write_bytes(c.build_pak())
        state_, detail, wanted = c.check(game)
        ok(state_ == "conflict" and "9000" in detail and not wanted, f"conflict found ({state_}, {detail})")
        c._sync(game)
        ok((ours.parent / (ours.name + ".remove")).is_file() and staged, "ours staged for removal after exit")
        other.unlink()
        c._sync(game)
        ok(not (ours.parent / (ours.name + ".remove")).exists(), "conflict gone: removal unstaged")

        # A game update changed debug.js: step aside
        d_dir = tmp / "d" / "uilink"
        shutil.copytree(a_dir, d_dir)
        vj = json.loads((d_dir / "page" / "vanilla.json").read_text())
        vj["sha1"] = "0" * 40
        (d_dir / "page" / "vanilla.json").write_text(json.dumps(vj))
        d = load_copy("modd_uilink", d_dir)
        state_, detail, wanted = d.check(game)
        ok(state_ == "game_changed" and not wanted, f"patched game -> game_changed ({state_})")

        # Keys: the hub's own thread when there's no Mod Menu
        ok(c.key_down("F8") is True, "first ask reports held (no press until the thread reads)")
        ok(wait(lambda: hub2.keys_thread is not None and hub2.keys_thread.is_alive()), "key thread running")

        ticker.running = False
        c._stop_server(hub2)
        errors = [m for m in logged if m.startswith(("error", "exception"))]
        ok(not errors, f"no errors logged {errors[:3]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
