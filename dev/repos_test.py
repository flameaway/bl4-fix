"""
Exercises the Mod Menu's repositories and hot loading (mods/bl4_mod_menu/repos.py, hotload.py)
offline: a fake game folder, repositories served by a local http server instead of GitHub, and a
stand-in mods_base so mods really import, register, reload and roll back. Never touches the real
game, the real sdk_mods or the network.

    python dev/repos_test.py
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import zipfile
from functools import partial
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "mods" / "bl4_mod_menu"))
import hotload  # noqa: E402
import repos  # noqa: E402
import updates  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: object, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


# region A stand-in mods_base: a registry, and mods that turn on from their settings like the real one


class FakeMod:
    def __init__(self, name: str, settings_file: Path) -> None:
        self.name = name
        self.settings_file = settings_file
        self.is_enabled = False
        self.enabling_locked = False
        self.log: list[str] = []

    def enable(self) -> None:
        self.is_enabled = True
        self.log.append("enable")

    def disable(self, dont_update_setting: bool = False) -> None:
        self.is_enabled = False
        self.log.append("disable")

    def load_settings(self) -> None:
        try:
            if json.loads(self.settings_file.read_text(encoding="utf-8")).get("enabled"):
                self.enable()
        except (OSError, ValueError):
            pass


mod_list: list[FakeMod] = []


def register_mod(mod: FakeMod) -> None:
    if mod not in mod_list:
        mod_list.append(mod)
        mod.load_settings()


def deregister_mod(mod: FakeMod) -> None:
    if mod.is_enabled:
        mod.disable(dont_update_setting=True)
    mod_list.remove(mod)


def install_fake_mods_base(settings_dir: Path) -> None:
    pkg = types.ModuleType("mods_base")
    pkg.__path__ = []  # type: ignore[attr-defined]
    ml = types.ModuleType("mods_base.mod_list")
    ml.mod_list = mod_list  # type: ignore[attr-defined]
    ml.register_mod = register_mod  # type: ignore[attr-defined]
    ml.deregister_mod = deregister_mod  # type: ignore[attr-defined]

    def build(name: str, module: str) -> FakeMod:
        # Like build_mod: drop a mod with the same settings file, register the new one
        settings = settings_dir / f"{module}.json"
        for old in [m for m in mod_list if m.settings_file == settings]:
            deregister_mod(old)
        mod = FakeMod(name, settings)
        register_mod(mod)
        return mod

    pkg.build = build  # type: ignore[attr-defined]
    pkg.mod_list = ml  # type: ignore[attr-defined]
    sys.modules["mods_base"] = pkg
    sys.modules["mods_base.mod_list"] = ml


def sdkmod(mod_id: str, version: str, *, body: str = "", extra: dict[str, bytes] | None = None) -> bytes:
    """A small mod: registers itself through the fake mods_base, records its version."""
    init = (
        "import mods_base\n"
        f"VERSION = {version!r}\n"
        f"mod = mods_base.build('Test {mod_id}', {mod_id!r})\n"
        f"{body}\n"
    )
    toml = f'[project]\nname = "Test {mod_id}"\nversion = "{version}"\ndescription = "A test mod."\nauthors = [{{ name = "tester" }}]\n'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{mod_id}/__init__.py", init)
        zf.writestr(f"{mod_id}/pyproject.toml", toml)
        for name, data in (extra or {}).items():
            zf.writestr(f"{mod_id}/{name}", data)
    return out.getvalue()


# endregion


def png(width: int, height: int) -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def wait(cond, what: str, timeout: float = 10.0) -> bool:  # noqa: ANN001
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    print(f"      (timed out waiting for {what})")
    return False


def flush_worker() -> None:
    """Waits until the repositories' worker ran everything queued so far (reading the settings file
    while it replaces it would make Windows refuse the replace)."""
    done = threading.Event()
    repos._net.put(done.set)
    done.wait(10)


def manifest_for(mods: dict[str, tuple[str, bytes]], base_url: str, **extra: object) -> dict:
    return {
        "format": 1, "release": "v1", **extra,
        "mods": {
            mod_id: {"name": f"Test {mod_id}", "version": ver, "asset": f"{mod_id}.sdkmod",
                     "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "author": "tester",
                     "blurb": f"{mod_id} does things.", "icon": "icon.png", "images": ["shot.png", f"{base_url}/nope.gif"]}
            for mod_id, (ver, data) in mods.items()
        },
    }  # fmt: skip


def main() -> int:  # noqa: PLR0915
    tmp = Path(tempfile.mkdtemp(prefix="bl4_repos_test_"))
    site = tmp / "site"
    game = tmp / "Borderlands 4"
    mods_dir = game / "sdk_mods"
    (mods_dir / "settings").mkdir(parents=True)
    install_fake_mods_base(mods_dir / "settings")
    sys.path.insert(0, str(mods_dir))  # the SDK puts sdk_mods itself on sys.path
    server, base = serve(site)
    updates.ALLOW_HTTP = True
    # A GitHub-style repository ("tester/mods") and a plain https index, both served locally
    updates.manifest_url = lambda repo: f"{base}/gh/{repo}/latest/manifest.json"
    updates.asset_url = lambda repo, tag, asset: f"{base}/gh/{repo}/{tag}/{asset}"
    repos.api_url = lambda key: f"{base}/api/{key}/latest.json"
    updates.U.repo = "official/bl4-fix"
    updates.U.manifest = None
    repos.R.game_dir = game
    try:
        # region Normalising what the player pastes
        check(repos.normalize("tester/mods") == "tester/mods", "owner/name kept")
        check(repos.normalize("https://github.com/tester/mods/releases") == "tester/mods", "github.com link -> owner/name")
        check(repos.normalize("github.com/tester/mods.git") == "tester/mods", "a .git link -> owner/name")
        check(repos.normalize("https://example.com/index.json") == "https://example.com/index.json", "https index kept")
        check(repos.normalize("ftp://x/y z") is None and repos.normalize("") is None, "junk refused")
        updates.ALLOW_HTTP = False
        check(repos.normalize("http://example.com/index.json") is None, "plain http refused outside tests")
        updates.ALLOW_HTTP = True
        # endregion

        # region A GitHub repository with a manifest
        alpha1, beta1 = sdkmod("alpha_mod", "1.0.0"), sdkmod("beta_mod", "2.0.0")
        gh = site / "gh" / "tester" / "mods"
        (gh / "v1").mkdir(parents=True)
        (gh / "latest").mkdir(parents=True)
        for mod_id, data in (("alpha_mod", alpha1), ("beta_mod", beta1)):
            (gh / "v1" / f"{mod_id}.sdkmod").write_bytes(data)
        man = manifest_for({"alpha_mod": ("1.0.0", alpha1), "beta_mod": ("2.0.0", beta1)}, base, name="Tester's Mods")
        man["mods"]["mods_base"] = dict(man["mods"]["alpha_mod"])  # a repository can't ship the frameworks
        man["mods"]["other_game"] = {**man["mods"]["alpha_mod"], "games": ["BL3"]}
        (gh / "latest" / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        (gh / "latest" / "icon.png").write_bytes(png(64, 64))
        (gh / "latest" / "shot.png").write_bytes(png(320, 180))
        (site / "nope.gif").write_bytes(b"GIF89a....")

        info, mods = repos.fetch("tester/mods")
        check(info["name"] == "Tester's Mods" and set(mods) == {"alpha_mod", "beta_mod"}, "manifest repository read; frameworks and other games dropped")
        check(mods["alpha_mod"]["download"] == f"{base}/gh/tester/mods/v1/alpha_mod.sdkmod", "asset resolved to the release download")
        check(mods["alpha_mod"]["icon"] == f"{base}/gh/tester/mods/latest/icon.png", "icon relative to the index")

        # Adding: checked first, then waits for the player's OK
        repos.add_async("https://github.com/tester/mods")
        wait(lambda: repos.R.pending and repos.R.pending.get("status") == "ready", "the pending repository")
        check(repos.R.pending and repos.R.pending["count"] == 2, "pending repository shows its 2 mods")
        repos.confirm_add()
        wait(lambda: len(repos.R.discover) == 2, "Discover to list both")
        check([s["key"] for s in repos.R.sources] == ["tester/mods"], "repository added")
        saved = json.loads(repos.settings_file(game).read_text(encoding="utf-8"))
        check(saved["sources"][0]["key"] == "tester/mods", "sources saved")
        repos.add_async("tester/mods")
        time.sleep(0.2)
        check(repos.R.message["error"] and "already" in repos.R.message["text"], "adding it twice is refused")
        repos.add_async("official/bl4-fix")
        time.sleep(0.1)
        check(repos.R.message["error"] and "built in" in repos.R.message["text"], "the built-in repository can't be added")
        # endregion

        # region A plain https index and a GitHub repository without a manifest
        (site / "idx").mkdir()
        gamma1 = sdkmod("gamma_mod", "0.1.0")
        (site / "idx" / "gamma_mod.sdkmod").write_bytes(gamma1)
        (site / "idx" / "index.json").write_text(json.dumps(manifest_for({"gamma_mod": ("0.1.0", gamma1)}, base)), encoding="utf-8")
        info, mods = repos.fetch(f"{base}/idx/index.json")
        check(mods["gamma_mod"]["download"] == f"{base}/idx/gamma_mod.sdkmod", "index: asset relative to it")

        delta = sdkmod("delta_mod", "3.1.0")
        (site / "rel").mkdir()
        (site / "rel" / "delta_mod.sdkmod").write_bytes(delta)
        api = site / "api" / "plain" / "repo"
        api.mkdir(parents=True)
        (api / "latest.json").write_text(json.dumps({
            "tag_name": "v3.1.0", "name": "Delta 3.1", "body": "Fixes.",
            "assets": [{"name": "delta_mod.sdkmod", "size": len(delta), "digest": "sha256:" + hashlib.sha256(delta).hexdigest(),
                        "browser_download_url": f"{base}/rel/delta_mod.sdkmod"},
                       {"name": "readme.txt", "size": 3, "browser_download_url": f"{base}/rel/readme.txt"}],
        }), encoding="utf-8")  # fmt: skip
        info, mods = repos.fetch("plain/repo")
        check(info["mode"] == "github" and list(mods) == ["delta_mod"] and mods["delta_mod"]["version"] == "v3.1.0",
              "no manifest: the release's .sdkmod assets, tag as version")  # fmt: skip
        check(mods["delta_mod"]["sha256"] == hashlib.sha256(delta).hexdigest(), "GitHub's asset digest used as SHA-256")
        try:
            repos.fetch("nobody/here")
            check(False, "a missing repository fails")
        except Exception as ex:  # noqa: BLE001
            check("404" in str(ex) or "Not Found" in str(ex), f"a missing repository fails ({repos._reason(ex)})")
        # endregion

        # region Install from Discover: a new file, loaded at once
        check(repos.install_async("alpha_mod"), "install started")
        wait(repos.pending_loads, "the load to be queued")
        check((mods_dir / "alpha_mod.sdkmod").read_bytes() == alpha1, "written into sdk_mods")
        check(json.loads((mods_dir / "settings" / "alpha_mod.json").read_text())["enabled"], "settings say enabled")
        repos.drain()
        alpha = sys.modules.get("alpha_mod")
        check(alpha is not None and alpha.VERSION == "1.0.0" and alpha.mod.is_enabled, "loaded and enabled without a restart")
        check(repos.R.jobs["alpha_mod"]["state"] == "loaded", "job says loaded")
        check(repos.R.installed["alpha_mod"]["repo"] == "tester/mods", "remembered as tester/mods'")
        wait(lambda: all(d["id"] != "alpha_mod" for d in repos.R.discover), "Discover to drop it")
        check(all(d["id"] != "alpha_mod" for d in repos.R.discover), "no longer on Discover")

        # A damaged download changes nothing
        (gh / "v1" / "beta_mod.sdkmod").write_bytes(beta1[:-10] + b"0123456789")
        repos.install_async("beta_mod")
        wait(lambda: repos.R.jobs.get("beta_mod", {}).get("state") == "failed", "beta to fail")
        check(not (mods_dir / "beta_mod.sdkmod").exists() and "checksum" in repos.R.jobs["beta_mod"]["message"], "checksum mismatch refused, nothing written")
        (gh / "v1" / "beta_mod.sdkmod").write_bytes(beta1)
        # endregion

        # region Update: staged, reloaded from the staged copy, the loaded file never written
        alpha2 = sdkmod("alpha_mod", "1.1.0")
        (gh / "v2").mkdir()
        (gh / "v2" / "alpha_mod.sdkmod").write_bytes(alpha2)
        man2 = manifest_for({"alpha_mod": ("1.1.0", alpha2), "beta_mod": ("2.0.0", beta1)}, base, name="Tester's Mods")
        man2["release"] = "v2"
        (gh / "latest" / "manifest.json").write_text(json.dumps(man2), encoding="utf-8")
        repos.refresh_async()
        wait(lambda: not repos.R.refreshing and repos.R.updates, "the update to show")
        check([u["id"] for u in repos.R.updates] == ["alpha_mod"] and repos.R.updates[0]["installed"] == "1.0.0", "update found (1.0.0 -> 1.1.0)")
        check(repos.summary()["updates"] == 1, "badge counts it")
        old_mod = alpha.mod
        repos.update_async(["alpha_mod"])
        wait(repos.pending_loads, "the reload to be queued")
        staged = updates.staging(game) / "alpha_mod.sdkmod"
        check(staged.read_bytes() == alpha2 and (mods_dir / "alpha_mod.sdkmod").read_bytes() == alpha1, "staged; the loaded file untouched")
        repos.drain()
        alpha = sys.modules["alpha_mod"]
        check(alpha.VERSION == "1.1.0" and alpha.mod.is_enabled and old_mod not in mod_list, "reloaded to 1.1.0, still on, old mod gone")
        check("disable" in old_mod.log, "the old copy was turned off first")
        check(repos.R.jobs["alpha_mod"]["state"] == "reloaded", "job says reloaded")
        wait(lambda: not repos.R.updates, "updates to clear")
        check(not repos.R.updates, "no update left")
        # endregion

        # region Hot loading from the mods folder
        (mods_dir / "dropped_mod.sdkmod").write_bytes(sdkmod("dropped_mod", "0.5"))
        (mods_dir / "Bad Name (2).sdkmod").write_bytes(b"not a zip")
        (mods_dir / "wrong_root.sdkmod").write_bytes(sdkmod("other_name", "1"))
        hotload.scan(game)
        found = {f["id"]: f for f in hotload.H.found}
        check("dropped_mod" in found and found["dropped_mod"]["kind"] == "new" and not found["dropped_mod"]["problem"], "a dropped mod is found")
        check(found["dropped_mod"]["version"] == "0.5" and found["dropped_mod"]["author"] == "tester", "its pyproject read")
        check(found.get("wrong_root", {}).get("problem"), "a misnamed .sdkmod says what's wrong")
        check("alpha_mod" not in found, "loaded mods aren't listed")
        msg = hotload.load(game, "dropped_mod", enable=True)
        check(sys.modules["dropped_mod"].mod.is_enabled and "Loaded" in msg, f"Load: {msg}")
        try:
            hotload.load(game, "wrong_root")
            check(False, "a misnamed .sdkmod refused")
        except RuntimeError as ex:
            check("must hold one folder" in str(ex), "a misnamed .sdkmod refused")

        # Dropped over the loaded copy: shows as changed, Reload picks it up
        hotload.H.baseline["dropped_mod"] = (0, 0)  # the copy the game loaded "looked" different
        (mods_dir / "dropped_mod.sdkmod").write_bytes(sdkmod("dropped_mod", "0.6"))
        hotload.scan(game)
        check(any(f["id"] == "dropped_mod" and f["kind"] == "changed" for f in hotload.H.found), "a replaced file shows as changed")
        hotload.reload(game, "dropped_mod")
        check(sys.modules["dropped_mod"].VERSION == "0.6", "Reload runs the replaced file")

        # A broken new copy: the old one keeps running
        (mods_dir / "dropped_mod.sdkmod").write_bytes(sdkmod("dropped_mod", "0.7", body="raise RuntimeError('boom')"))
        before = sys.modules["dropped_mod"]
        try:
            hotload.reload(game, "dropped_mod")
            check(False, "a broken copy fails")
        except RuntimeError as ex:
            check("boom" in str(ex), "a broken copy fails with its error")
        check(sys.modules["dropped_mod"] is before and before.mod in mod_list and before.mod.is_enabled, "rolled back: the old copy is registered and on")

        # Another mod imports from it: no reload
        user = types.ModuleType("user_mod")
        user.thing = sys.modules["dropped_mod"]
        sys.modules["user_mod"] = user
        check("user_mod" in hotload.can_reload("dropped_mod"), "a mod others import from can't reload")
        del sys.modules["user_mod"]
        check(hotload.can_reload("bl4_mod_menu") and hotload.can_reload("mods_base"), "the frameworks never reload")
        # endregion

        # region Pictures
        icon_url = f"{base}/gh/tester/mods/latest/icon.png"
        repos.want_images([icon_url, f"{base}/nope.gif", "javascript:alert(1)"])
        wait(lambda: repos.R.images.get(icon_url) and repos.R.images.get(f"{base}/nope.gif") is not None, "the pictures")
        local = repos.R.images.get(icon_url) or ""
        cached = game / repos.IMAGE_DIR / local.split("/")[-1].split("?")[0]
        check(local.startswith(repos.IMAGE_URL) and cached.read_bytes() == png(64, 64), "a PNG cached next to the page")
        check(repos.R.images.get(f"{base}/nope.gif") == "", "a GIF refused")
        check("javascript:alert(1)" not in repos.R.images, "a non-http picture ignored")
        check(repos._image_size(png(5000, 10)) == ("png", 5000, 10), "PNG size read")
        # endregion

        # region Icons the mods carry
        (mods_dir / "icon_mod.sdkmod").write_bytes(sdkmod("icon_mod", "1", extra={"icon.png": png(32, 32)}))
        hotload.load(game, "icon_mod")
        hotload.publish_icons(game)
        check(hotload.H.icons.get("icon_mod", "").startswith(hotload.ICON_URL + "icon_mod.png"), "a mod's icon.png published")
        check((game / hotload.ICON_DIR / "icon_mod.png").read_bytes() == png(32, 32), "icon file written")
        # endregion

        # region Remove: off now, file goes at exit (the helper)
        msg = repos.remove_mod("alpha_mod")
        check(not sys.modules["alpha_mod"].mod.is_enabled and sys.modules["alpha_mod"].mod not in mod_list, "removed: off and out of the list")
        check((updates.staging(game) / "alpha_mod.remove").exists() and not staged.exists(), "removal marker staged, staged update dropped")
        try:
            repos.remove_mod("bl4_mod_menu")
            check(False, "the Mod Menu can't be removed")
        except RuntimeError:
            check(True, "the Mod Menu can't be removed")
        # Re-install in the same session takes the removal back
        repos._recompute()
        # Run the real helper against the fake folder (no game process: it goes at once)
        (updates.staging(game) / "dropped_mod.sdkmod").write_bytes(sdkmod("dropped_mod", "0.9"))
        script = updates.helper_script(game, process="NoSuchProcessBL4Test")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=120)  # noqa: S603, S607
        check(not (mods_dir / "alpha_mod.sdkmod").exists(), "helper deleted the removed mod")
        check(updates._zip_version(mods_dir / "dropped_mod.sdkmod", "dropped_mod") == "0.9", "helper still moved the staged update in")
        check(not updates.staging(game).exists(), "staging folder cleaned up")
        # endregion

        # region The page's data
        rev, data = repos.snapshot_json()
        rev2, data2 = repos.snapshot_json()
        state = json.loads(data)
        check(rev == rev2 and data is data2, "an unchanged state is the same revision (cached)")
        check({"sources", "discover", "updates", "jobs", "images", "found", "icons"} <= set(state), "snapshot has every part")
        repos.remove_source("tester/mods")
        wait(lambda: repos.snapshot_json()[0] != rev, "a new revision")
        check(repos.snapshot_json()[0] != rev and not repos.R.sources, "removing a repository changes the revision")
        # endregion

        # region Editing a repository's link, removing the built-in one
        index = f"{base}/idx/index.json"
        repos.add_async(index)
        wait(lambda: repos.R.pending and repos.R.pending.get("status") == "ready", "the index pending")
        check(repos.confirm_add(), "index added")
        repos.R.installed["gamma_mod"] = {"repo": index, "version": "0.1.0", "sha256": ""}
        repos.add_async("https://github.com/tester/mods", replace=index)
        wait(lambda: repos.R.pending and repos.R.pending.get("status") == "ready", "the edited link pending")
        check(repos.R.pending["replace"] == index and repos.R.pending["replace_name"], "the edit says what it replaces")
        check(repos.confirm_add(), "edit saved")
        check([s["key"] for s in repos.R.sources] == ["tester/mods"], "edited repository replaced in place")
        check(repos.R.installed["gamma_mod"]["repo"] == "tester/mods", "mods installed from it follow the new link")
        repos.add_async("github.com/tester/mods", replace="tester/mods")
        check(repos.R.pending is None and repos.source_url("tester/mods") == "github.com/tester/mods",
              "editing into the same repository just saves the link")  # fmt: skip
        check(not repos.confirm_add(), "nothing to confirm without a pending repository")
        repos.add_async("official/bl4-fix", replace="tester/mods")
        time.sleep(0.1)
        check(repos.R.message["error"] and [s["key"] for s in repos.R.sources] == ["tester/mods"], "an edit can't become the built-in one")
        repos.remove_source("official/bl4-fix")
        flush_worker()
        check(json.loads(repos.settings_file(game).read_text(encoding="utf-8")).get("official_hidden"), "removed built-in repository saved")
        state = json.loads(repos.snapshot_json()[1])
        check(state["sources"][0]["official"] and state["sources"][0]["hidden"], "the built-in repository shows as removed")
        repos._load(game)
        check(repos.R.official_hidden, "removed built-in repository stays removed after a restart")
        repos.add_async("official/bl4-fix")
        flush_worker()
        check("official_hidden" not in json.loads(repos.settings_file(game).read_text(encoding="utf-8")), "shown again saved")
        check(not repos.R.official_hidden, "adding the built-in link brings it back")
        repos.remove_source("tester/mods")
        wait(lambda: not repos.R.sources, "cleanup")
        # A corrupt sources file is set aside, not overwritten
        repos.settings_file(game).write_text("{broken", encoding="utf-8")
        repos._load(game)
        check(any(p.name.startswith("bl4_mod_menu_repos.bad-") for p in (mods_dir / "settings").iterdir()), "a corrupt sources file is set aside")
        # endregion
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
