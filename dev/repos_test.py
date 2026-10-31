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


# Every path the local "GitHub" was asked for (counts API calls and downloads)
seen: list[str] = []


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.path)
            super().do_GET()

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


def install_reviewed(mod_id: str) -> bool:
    """Install from an added repository: the download, its confirm step (what its code uses), Install."""
    if not repos.install_async(mod_id):
        return False
    wait(lambda: repos.R.jobs.get(mod_id, {}).get("state") in ("review", "failed"), f"{mod_id}'s confirm step")
    return repos.install_confirm(mod_id)


def update_reviewed(mod_ids: list[str]) -> int:
    """Update from an added repository: the downloads, the confirm step (what's new), Update."""
    repos.update_async(mod_ids)
    wait(lambda: all(repos.R.jobs.get(i, {}).get("state") in ("review", "failed", "staged") for i in mod_ids), "the confirm step")
    return repos.update_confirm(mod_ids)


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
    real_asset_url = updates.asset_url
    # A GitHub-style repository ("tester/mods") and a plain https index, both served locally
    updates.manifest_url = lambda repo: f"{base}/gh/{repo}/latest/manifest.json"
    updates.asset_url = lambda repo, tag, asset: f"{base}/gh/{repo}/{tag}/{asset}"
    repos.api_url = lambda key: f"{base}/api/{key}/latest.json"
    # GitHub's repository numbers (takeover detection): 404 here unless a test writes one
    repos.repo_api_url = lambda ident: f"{base}/repoapi/{ident}.json"
    updates.U.repo = "official/bl4-fix"
    updates.U.manifest = None
    repos.R.game_dir = game
    try:
        # region Normalising what the player pastes
        check(repos.normalize("tester/mods") == "tester/mods", "owner/name kept")
        check(repos.normalize("https://github.com/tester/mods/releases") == "tester/mods", "github.com link -> owner/name")
        check(repos.normalize("github.com/tester/mods.git") == "tester/mods", "a .git link -> owner/name")
        raw = "https://raw.githubusercontent.com/tester/mods/main/index.json"
        check(repos.normalize(raw) == raw, "an index file on GitHub kept")
        check(repos.normalize("https://example.com/index.json") is None, "an index anywhere else refused (GitHub only)")
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
        check(repos.R.pending["auto"] is False, "a new repository's check at launch starts off")
        check("Only add repositories from modders you trust" in json.loads(repos.snapshot_json()[1])["risk"], "the page gets the warning")
        repos.confirm_add()
        check(repos.R.sources[0]["auto"] is False, "added with check at launch off")
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
        check(install_reviewed("alpha_mod"), "install started (through its confirm step)")
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
        update_reviewed(["alpha_mod"])
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

        # region A hostile repository: nothing in its index gets further than plain, checked data
        updates.ALLOW_HTTP = False  # the real rules
        for url, ok in (
            ("https://github.com/tester/mods", True), ("https://raw.githubusercontent.com/a/b/main/i.json", True),
            ("https://api.github.com/repos/a/b/releases/latest", True), ("https://objects.githubusercontent.com/x", True),
            ("https://release-assets.githubusercontent.com/x", True), ("https://GitHub.com/a/b", True),
            ("https://example.com/i.json", False), ("https://evilgithub.com/x", False), ("https://github.com.evil.example/x", False),
            ("https://githubusercontent.com.evil.example/x", False), ("https://me.github.io/i.json", False),
            ("https://www.nexusmods.com/borderlands4/mods/1", False), ("http://github.com/a/b", False),
            ("http://example.com/i.json", False), ("https://github.com@evil.example/i.json", False),
            ("https://user:pw@example.com/i.json", False), ("https://10.0.0.1/i.json", False),
            ("https://192.168.1.1/x.png", False), ("https://127.0.0.1:8080/x", False), ("https://[::1]/x", False),
            ("https://100.64.0.1/x", False), ("https://169.254.169.254/x", False), ("https://localhost/x", False),
            ("https://router.local/x", False), ("https://intranet/x", False), ("https://2130706433/x", False),
            ("https://0x7f.1/x", False), ("https://exa mple.com/x", False), ("https://example.com/a\u202egnp.exe", False),
            ("https://example.com\\@evil/x", False), ("javascript:alert(1)", False), ("file:///C:/Windows/win.ini", False),
            ("https://example.com:99999/x", False),
        ):  # fmt: skip
            check(repos.safe_url(url) is ok, f"link {'allowed' if ok else 'refused'}: {url!r}")
        check(repos.normalize("https://192.168.1.1/index.json") is None, "a repository on the home network can't be added")
        for target in ("https://127.0.0.1/admin", "https://example.com/evil.sdkmod"):
            try:
                req = __import__("urllib.request").request.Request("https://github.com/a/b/releases/download/v1/x.sdkmod")
                updates._GitHubRedirects().redirect_request(req, None, 302, "Found", {}, target)
                check(False, f"a redirect away from GitHub is refused ({target})")
            except Exception as ex:  # noqa: BLE001
                check("refused a redirect away from GitHub" in str(ex), f"a redirect away from GitHub is refused before connecting ({target})")
        try:
            updates._get("https://example.com/x.sdkmod", 100)
            check(False, "a download from elsewhere is refused")
        except RuntimeError as ex:
            check("isn't on GitHub" in str(ex), "a download from elsewhere is refused (BL4 Fix's updater too)")
        check(real_asset_url("o/r", "../../evil", "a/b.sdkmod") == "https://github.com/o/r/releases/download/..%2F..%2Fevil/a%2Fb.sdkmod",
              "release tag and asset name can't leave the release's download folder")  # fmt: skip
        updates.ALLOW_HTTP = True

        evil = sdkmod("evil_mod", "1.0.0")
        (site / "evil").mkdir()
        (site / "evil" / "evil_mod.sdkmod").write_bytes(evil)
        good = {"name": "Evil", "version": "1.0.0", "asset": "evil_mod.sdkmod", "sha256": hashlib.sha256(evil).hexdigest(), "size": len(evil)}
        hostile = {
            "format": 1, "name": "BL4 Fix (official)", "description": "Line one\r\nline two\u0007",
            "homepage": "https://user:pw@github.com/flameaway/bl4-fix",
            "mods": {
                "evil_mod": {**good, "name": "Kairosdex\u202e\u200b\x1b[31m", "author": "x\ny", "blurb": "\u2066hidden\u2069",
                             "homepage": "https://10.0.0.5/", "icon": "https://192.168.0.1/i.png",
                             "images": ["https://localhost/a.png", "javascript:alert(1)", "https://cdn.example.com/a.png",
                                        "https://raw.githubusercontent.com/me/mods/main/a.png",
                                        "//192.168.0.9/b.png"],
                             "size": True, "version": "1.0.0\u0000"},
                "json": dict(good), "CON": dict(good), "Mods_Base": dict(good), "bl4_direct": dict(good),
                "BL4_REROLL": dict(good), "../evil": dict(good), "a b": dict(good),
                "path_asset": {**good, "asset": "../evil_mod.sdkmod"}, "dir_asset": {**good, "asset": "x/evil_mod.sdkmod"},
                "dot_asset": {**good, "asset": ".evil_mod.sdkmod"}, "http_url": {**good, "asset": None, "url": "http://192.168.0.9/x.sdkmod"},
                "big_size": {**good, "size": 10**40}, "not_a_dict": "oops",
            },
        }  # fmt: skip
        (site / "evil" / "index.json").write_text(json.dumps(hostile), encoding="utf-8")
        repos.R.official_ids = frozenset({"bl4_reroll"})
        info, mods = repos.fetch(f"{base}/evil/index.json")
        check(info["name"] == f"{base}/evil/index.json", "a repository calling itself BL4 Fix is shown by its link")
        check(info["homepage"] == "" and info["description"] == "Line one\nline two", "repository links and text cleaned")
        check(set(mods) == {"evil_mod", "big_size"}, f"only plain module names with plain assets kept ({sorted(mods)})")
        e = mods["evil_mod"]
        check(e["name"] == "Kairosdex[31m" and e["author"] == "x y" and e["blurb"] == "hidden" and e["version"] == "1.0.0",
              "control, bidi and zero-width characters removed; one-line fields stay one line")  # fmt: skip
        check(e["homepage"] == "" and e["icon"] == "" and e["images"] == ["https://raw.githubusercontent.com/me/mods/main/a.png"],
              "only pictures on GitHub kept: other sites, the home network, scripts, protocol-relative links dropped")  # fmt: skip
        check(e["size"] == 0 and mods["big_size"]["size"] == 0, "a size that's not a sane number is ignored")
        repos.R.official_ids = frozenset()
        check(repos.reserved("BL4_Reroll") and not repos.reserved("tester_mod"), "BL4 Fix's names reserved before its release was read")
        sys.path.insert(0, str(REPO / "installer"))
        import catalog  # noqa: PLC0415
        missing = {m.id for m in catalog.MODS} - repos.OFFICIAL_IDS
        check(not missing, f"repos.OFFICIAL_IDS lists every mod in installer/catalog.py (missing: {sorted(missing)})")
        for bad, what in (("{\"format\": 1, \"mods\": {\"a\": {\"size\": Infinity}}}", "Infinity"),
                          ("[" * 100_000 + "]" * 100_000, "deep nesting")):  # fmt: skip
            (site / "evil" / "bad.json").write_text(bad, encoding="utf-8")
            try:
                repos.fetch(f"{base}/evil/bad.json")
                check(False, f"an index with {what} is refused")
            except Exception as ex:  # noqa: BLE001
                check(repos._reason(ex) == "the index isn't valid JSON", f"an index with {what} is refused ({repos._reason(ex)})")

        def zipped(entries: list[tuple[str, bytes]], symlink: str = "") -> bytes:
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as zf:
                for name, data in entries:
                    zf.writestr(name, data)
                if symlink:
                    link = zipfile.ZipInfo(symlink)
                    link.external_attr = 0o120777 << 16
                    zf.writestr(link, "../../outside")
            return out.getvalue()

        base_files = [("evil_mod/__init__.py", b""), ("evil_mod/pyproject.toml", b'[project]\nversion = "1.0.0"\n')]
        for data, what in (
            (zipped([*base_files, ("evil_mod/../../escape.py", b"x")]), "a .. path"),
            (zipped([*base_files, ("evil_mod/sub\\..\\x.py", b"x")]), "a backslash path"),
            (zipped([*base_files, ("evil_mod/C:x.py", b"x")]), "a drive path"),
            (zipped(base_files, symlink="evil_mod/link"), "a symlink"),
            (zipped([*base_files, ("evil_mod/A.py", b"1"), ("evil_mod/a.py", b"2")]), "the same name twice"),
        ):
            try:
                repos._verify(data, "evil_mod", "1.0.0")
                check(False, f"a download with {what} is refused")
            except RuntimeError as ex:
                check(True, f"a download with {what} is refused ({ex})")
        repos.MAX_UNPACKED, limit = 10, repos.MAX_UNPACKED
        try:
            repos._verify(evil, "evil_mod", "1.0.0")
            check(False, "a download that unpacks to too much is refused")
        except RuntimeError as ex:
            check("far too much" in str(ex), "a download that unpacks to too much is refused")
        repos.MAX_UNPACKED = limit
        repos._verify(evil, "evil_mod", "1.0.0")
        check(True, "a normal download still passes")
        # BL4 Fix's names stay reserved on a launch that doesn't read its release (saved)
        updates.U.manifest = {"format": 1, "release": "v9", "mods": {"gamma_mod": {"name": "G", "version": "1", "asset": "g.sdkmod"}}}
        repos.R.loaded = True
        repos._recompute()
        check("gamma_mod" in repos.R.official_ids and repos.reserved("Gamma_Mod"), "BL4 Fix's mod names reserved, case-blind")
        check("gamma_mod" in json.loads(repos.settings_file(game).read_text(encoding="utf-8"))["official_ids"], "and saved")
        updates.U.manifest = None
        repos.R.official_ids = frozenset()
        repos._load(game)
        check("gamma_mod" in repos.R.official_ids, "read back at the next launch")
        check("gamma_mod" not in repos.fetch(f"{base}/idx/index.json")[1], "another repository can't list one of them")
        repos.R.official_ids = frozenset()
        # endregion

        # region Check at launch, per repository
        repos.settings_file(game).write_text(json.dumps({"format": 1, "sources": [
            {"key": "tester/mods", "url": "tester/mods", "auto": True}, {"key": index, "url": index}]}), encoding="utf-8")  # fmt: skip
        repos._load(game, legacy_auto=False)
        check([s["auto"] for s in repos.R.sources] == [True, False], "saved switches kept; older entries take the old option's value")
        repos._load(game, legacy_auto=True)
        check([s["auto"] for s in repos.R.sources] == [True, True], "(the old option was on: on)")
        repos.set_auto(index, False)
        flush_worker()
        check([s.get("auto") for s in json.loads(repos.settings_file(game).read_text(encoding="utf-8"))["sources"]] == [True, False], "a switch change is saved")
        repos.R.info.clear()
        repos.start(game, legacy_auto=True)
        flush_worker()
        wait(lambda: not repos.R.refreshing and repos.R.info.get("tester/mods", {}).get("status") == "ok", "the launch check")
        check(repos.R.info.get("tester/mods", {}).get("status") == "ok" and index not in repos.R.info, "at launch only the switched-on repository is read")
        state = json.loads(repos.snapshot_json()[1])
        check([s["auto"] for s in state["sources"][1:]] == [True, False], "the page sees each switch")
        repos.refresh_async()
        wait(lambda: not repos.R.refreshing and repos.R.info.get(index, {}).get("status") == "ok", "Refresh")
        check(repos.R.info.get(index, {}).get("status") == "ok", "Refresh reads the switched-off one too")
        repos.add_async("https://github.com/tester/other", replace=index)
        check(repos.R.pending and repos.R.pending["status"] == "checking", "(an edit is checked)")
        repos.cancel_add()
        # The first-run question (updates.py's side)
        updates.set_auto(True, asked=False)
        snap = updates.snapshot()
        check(snap["ask"] is True and snap["auto"] is True, "an unanswered question shows on the Updates page")
        chosen = []
        updates.U.chooser = chosen.append
        updates.choose(False)
        updates.set_auto(False, asked=True)
        check(chosen == [False] and updates.snapshot()["ask"] is False and "off" in updates.snapshot()["message"],
              "the answer goes to the option; the page says checks are off")  # fmt: skip
        # endregion

        # region Takeover detection (GitHub's repository number), the confirm step's code scan, required checksums
        flush_worker()
        import codescan  # noqa: PLC0415

        # What a mod's code uses: each kind from a small source
        def uses(src: str) -> list[str]:
            scan = codescan.Scan()
            scan.python("m/x.py", src.encode("utf-8"))
            return scan.result()["uses"]

        for src, want in (
            ("import socket\n", ["network"]), ("import urllib.request\n", ["network"]), ("from http import client\n", ["network"]),
            ("import requests, ssl\n", ["network"]), ("import urllib.parse\nimport http\nx = http.HTTPStatus.OK\n", []),
            ("import subprocess\n", ["runs programs"]), ("import os\nos.system('x')\n", ["runs programs"]),
            ("import os as o\no.startfile('x')\n", ["runs programs"]), ("from os import popen\n", ["runs programs"]),
            ("import os\nos.execv('a', [])\n", ["runs programs"]), ("import os\nos.spawnl(0, 'a')\n", ["runs programs"]),
            ("import os\nos.path.join('a')\n", []), ("import ctypes\n", ["native code"]), ("from cffi import FFI\n", ["native code"]),
            ("import winreg\n", ["Windows registry"]), ("exec('1')\n", ["dynamic code"]), ("eval('1')\n", ["dynamic code"]),
            ("compile('1', 'x', 'exec')\n", ["dynamic code"]), ("import marshal\n", ["dynamic code"]),
            ("__import__(name)\n", ["dynamic code"]), ("__import__('socket')\n", ["network"]),
            ("import importlib\nimportlib.import_module(n)\n", ["dynamic code"]),
            ("import importlib\nimportlib.import_module('subprocess')\n", ["runs programs"]),
            ("import re\nre.compile('x')\n", []), ("def (:\n", ["unreadable code"]),
        ):  # fmt: skip
            check(uses(src) == want, f"scan: {src.strip()!r} -> {want} ({uses(src)})")
        # A big file is parsed in parts (a few KB each) with the same result as in one go
        big = "".join(
            f"@decorator\ndef f{i}(a,\n      b):\n    '''doc\n\nnot code: import socket\n'''\n    return [a,\n        b]\n\n"
            f"class C{i}:\n    x = 1\n\n    def m(self):\n        return 1\n\n    @staticmethod\n    def s():\n        pass\n"
            f"if a:\n    pass\nelse:\n    pass\n"
            for i in range(300)
        ) + "import subprocess\n"
        scan = codescan.Scan()
        parts = scan._parse(big)
        check(parts is not None and len(parts) > 10, f"scan: a {len(big) // 1024} KB file is parsed in {len(parts or [])} parts")
        check(uses(big) == ["runs programs"], "scan: the parts find the same (the docstring's import isn't code)")
        check(sum(len(p.body) for p in parts or []) >= 900, "scan: no statement lost between parts")  # type: ignore[attr-defined]
        codescan.MAX_SOURCE, cap = 10, codescan.MAX_SOURCE
        check(codescan.scan_zip(sdkmod("cap_mod", "1"))["uses"] == ["unreadable code"], "scan: more source than the cap -> unreadable code")
        codescan.MAX_SOURCE = cap
        check(codescan.scan_zip(sdkmod("pyd_mod", "1", extra={"fast.pyd": b"MZ"}))["uses"] == ["native code"], "scan: a .pyd in the zip -> native code")
        check(codescan.scan_zip(sdkmod("pyc_mod", "1", extra={"x.pyc": b"\x00"}))["uses"] == ["unreadable code"], "scan: compiled Python -> unreadable code")

        # A repository with one mod of each kind, one without a checksum
        sg = site / "gh" / "scan" / "mods"
        for d in ("v1", "v2", "latest"):
            (sg / d).mkdir(parents=True)
        bodies = {"net_mod": "import socket", "prog_mod": "import subprocess", "native_mod": "x = 1", "reg_mod": "import winreg",
                  "dyn_mod": "exec('1')", "bad_mod": "def (:", "plain_mod": "import json"}  # fmt: skip
        scan_v1 = {m: sdkmod(m, "1.0.0", body=body, extra={"fast.pyd": b"MZ"} if m == "native_mod" else None) for m, body in bodies.items()}
        for m, data in scan_v1.items():
            (sg / "v1" / f"{m}.sdkmod").write_bytes(data)
        scan_man = manifest_for({m: ("1.0.0", d) for m, d in scan_v1.items()}, base, name="Scan Mods")
        scan_man["mods"]["nosha_mod"] = {**scan_man["mods"]["plain_mod"], "sha256": ""}
        scan_man["mods"]["badsha_mod"] = {**scan_man["mods"]["plain_mod"], "sha256": "not-a-checksum"}
        (sg / "latest" / "manifest.json").write_text(json.dumps(scan_man), encoding="utf-8")
        info, mods = repos.fetch("scan/mods")
        check("nosha_mod" not in mods and "badsha_mod" not in mods and sorted(info["no_checksum"]) == ["badsha_mod", "nosha_mod"],
              "checksums required: mods without a valid sha256 are left out (and named)")  # fmt: skip
        try:
            repos._download("x_mod", {"download": f"{base}/gh/scan/mods/v1/plain_mod.sdkmod", "repo": "scan/mods", "sha256": ""})
            check(False, "a download without a checksum is refused")
        except RuntimeError as ex:
            check("SHA-256" in str(ex), "a download without a checksum is refused (also past the catalog)")
        # GitHub's API mode: an asset without a digest is left out; none with one = a clear error
        nod = sdkmod("nodigest_mod", "1")
        (site / "rel" / "nodigest_mod.sdkmod").write_bytes(nod)
        for key, assets in (("digest/some", [{"name": "nodigest_mod.sdkmod", "size": len(nod), "browser_download_url": f"{base}/rel/nodigest_mod.sdkmod"},
                                             {"name": "delta_mod.sdkmod", "size": len(delta), "digest": "sha256:" + hashlib.sha256(delta).hexdigest(),
                                              "browser_download_url": f"{base}/rel/delta_mod.sdkmod"}]),
                            ("digest/none", [{"name": "nodigest_mod.sdkmod", "size": len(nod), "browser_download_url": f"{base}/rel/nodigest_mod.sdkmod"}])):  # fmt: skip
            (site / "api" / key).mkdir(parents=True)
            (site / "api" / key / "latest.json").write_text(json.dumps({"tag_name": "v1", "assets": assets}), encoding="utf-8")
        info, mods = repos.fetch("digest/some")
        check(list(mods) == ["delta_mod"] and info["no_checksum"] == ["nodigest_mod"], "API mode: an asset GitHub gives no digest for is left out")
        try:
            repos.fetch("digest/none")
            check(False, "API mode: no asset with a digest is an error")
        except RuntimeError as ex:
            check("checksum" in str(ex), f"API mode: no asset with a digest is an error ({ex})")

        # Which repository a link leads to
        check(repos.repo_ident("tester/mods") == "tester/mods", "repository number asked for owner/name")
        check(repos.repo_ident("https://raw.githubusercontent.com/me/my-mods/main/index.json") == "me/my-mods", "an index file's repository read from its link")
        check(repos.repo_ident(f"{base}/idx/index.json") is None, "a link that isn't a repository's file has none")
        check(repos.REPO_API_URL.format("a/b") == "https://api.github.com/repos/a/b", "the number comes from api.github.com/repos/<owner>/<name>")

        idfile = site / "repoapi" / "scan" / "mods.json"
        idfile.parent.mkdir(parents=True)

        def set_id(number: int | None) -> None:
            if number is None:
                idfile.unlink(missing_ok=True)  # GitHub can't be asked (404 here; offline, rate limit)
            else:
                idfile.write_text(json.dumps({"id": number, "full_name": "scan/mods"}), encoding="utf-8")

        def api_calls() -> int:
            return sum(1 for p in seen if p.startswith("/repoapi/scan/mods"))

        def source() -> dict:
            return next(s for s in repos.R.sources if s["key"] == "scan/mods")

        def recheck() -> None:
            repos.R.id_checked.clear()  # as if the hour since the last check were over
            repos.refresh_async(["scan/mods"])
            wait(lambda: not repos.R.refreshing, "the check")
            flush_worker()

        def row() -> dict:
            return next(s for s in json.loads(repos.snapshot_json()[1])["sources"] if s["key"] == "scan/mods")

        def listed() -> set[str]:
            return {d["id"] for d in repos.R.discover if d["repo"] == "scan/mods"}

        set_id(1111)
        repos.add_async("scan/mods")
        wait(lambda: repos.R.pending and repos.R.pending.get("status") == "ready", "scan/mods pending")
        check(repos.R.pending["repo_id"] == 1111 and api_calls() == 1, "adding asks GitHub for the repository's number once")
        repos.confirm_add()
        flush_worker()
        saved = json.loads(repos.settings_file(game).read_text(encoding="utf-8"))
        check(next(s for s in saved["sources"] if s["key"] == "scan/mods").get("repo_id") == 1111, "the number is saved with the repository")
        wait(lambda: "net_mod" in listed(), "scan/mods on Discover")
        repos.refresh_async(["scan/mods"])
        wait(lambda: not repos.R.refreshing, "a check")
        check(api_calls() == 1, "a check within the hour doesn't ask GitHub again (60 calls an hour)")
        recheck()
        check(api_calls() == 2 and row()["status"] == "ok" and not row().get("blocked") and "net_mod" in listed(), "the same number passes")

        set_id(2222)  # the account was renamed or deleted, someone else took the name
        recheck()
        check(source().get("blocked_id") == 2222 and source()["repo_id"] == 1111, "a different number blocks the repository")
        check(row()["status"] == "error" and row().get("blocked") and "points to a different repository" in row()["message"], f"its row says why ({row()['message'][:80]})")
        check(not listed() and not any(u["repo"] == "scan/mods" for u in repos.R.updates), "its mods leave Discover and Updates")
        check(not repos.install_async("net_mod"), "nothing installs from it")
        saved = json.loads(repos.settings_file(game).read_text(encoding="utf-8"))
        check(next(s for s in saved["sources"] if s["key"] == "scan/mods").get("blocked_id") == 2222, "the block is saved")
        repos._load(game)
        check(source().get("blocked_id") == 2222 and row().get("blocked"), "still blocked after a restart, before it's read again")
        set_id(None)
        recheck()
        check(source().get("blocked_id") == 2222 and not listed(), "GitHub can't be asked: the last known state (blocked) stays")
        set_id(1111)
        recheck()
        check(not source().get("blocked_id") and "net_mod" in listed(), "the number matches again: unblocked")
        set_id(2222)
        recheck()
        check(source().get("blocked_id") == 2222, "(blocked again)")
        check(repos.trust_again("scan/mods") and source()["repo_id"] == 2222 and not source().get("blocked_id"), "Trust again accepts the new number")
        flush_worker()
        check(row()["status"] == "ok" and not row().get("blocked") and "net_mod" in listed(), "trusted again: its row and its mods are back")
        check(not repos.trust_again("scan/mods"), "Trust again does nothing for a repository that isn't blocked")
        set_id(None)
        recheck()
        check(not source().get("blocked_id") and "net_mod" in listed() and "Couldn't confirm" in row()["id_note"],
              "GitHub can't be asked: nothing blocked, the row says it couldn't confirm")  # fmt: skip
        source().pop("repo_id")  # saved by a Mod Menu before this check
        set_id(2222)
        recheck()
        check(source().get("repo_id") == 2222 and not source().get("blocked_id"), "a repository saved without a number gets it at its first check")

        # Install: download, scan, confirm step, then the same bytes
        def downloads(mod_id: str, tag: str = "v1") -> int:
            return seen.count(f"/gh/scan/mods/{tag}/{mod_id}.sdkmod")

        check(repos.install_async("net_mod"), "Install pressed")
        wait(lambda: repos.R.jobs.get("net_mod", {}).get("state") == "review", "net_mod's confirm step")
        job = repos.R.jobs["net_mod"]
        check(job["kind"] == "install" and job["uses"] == ["network"] and job["size"] == len(scan_v1["net_mod"]) and job["version"] == "1.0.0",
              f"the confirm step says what it uses, its size and version ({job})")  # fmt: skip
        check("hint, not a guarantee" in job["hint"] and "network" in job["where"], "with where it was seen and the hint")
        check(not (mods_dir / "net_mod.sdkmod").exists() and not repos.pending_loads(), "nothing written or loaded before the OK")
        check(json.loads(repos.snapshot_json()[1])["jobs"]["net_mod"]["uses"] == ["network"] and b"MZ" not in repos.snapshot_json()[1],
              "the page gets the scan (not the bytes)")  # fmt: skip
        check(not repos.install_async("net_mod"), "Install again while it waits does nothing")
        check(repos.install_confirm("net_mod"), "confirmed")
        wait(repos.pending_loads, "net_mod's load")
        check((mods_dir / "net_mod.sdkmod").read_bytes() == scan_v1["net_mod"] and downloads("net_mod") == 1,
              "the confirmed install writes the reviewed bytes: downloaded once")  # fmt: skip
        repos.drain()
        check(sys.modules["net_mod"].mod.is_enabled and repos.R.jobs["net_mod"]["state"] == "loaded", "and loads")
        check(not repos.install_confirm("net_mod"), "a second confirm does nothing")

        # Cancel drops the bytes
        repos.install_async("prog_mod")
        wait(lambda: repos.R.jobs.get("prog_mod", {}).get("state") == "review", "prog_mod's confirm step")
        check(repos.R.jobs["prog_mod"]["uses"] == ["runs programs"], "prog_mod: runs programs")
        repos.review_cancel(["prog_mod"])
        check("prog_mod" not in repos.R.jobs and "prog_mod" not in repos._held and not (mods_dir / "prog_mod.sdkmod").exists(), "Cancel: nothing kept, nothing written")
        check(not repos.install_confirm("prog_mod"), "nothing to confirm after Cancel")
        repos.install_async("prog_mod")
        wait(lambda: repos.R.jobs.get("prog_mod", {}).get("state") == "review", "prog_mod again")
        check(downloads("prog_mod") == 2, "Install after Cancel downloads again")
        # Each kind on the confirm step
        for mod_id, want in (("native_mod", ["native code"]), ("reg_mod", ["Windows registry"]), ("dyn_mod", ["dynamic code"]),
                             ("bad_mod", ["unreadable code"]), ("plain_mod", [])):  # fmt: skip
            repos.install_async(mod_id)
            wait(lambda mod_id=mod_id: repos.R.jobs.get(mod_id, {}).get("state") == "review", f"{mod_id}'s confirm step")
            check(repos.R.jobs.get(mod_id, {}).get("uses") == want, f"confirm step: {mod_id} uses {want}")
        # Blocked while it waited: the OK doesn't install it
        set_id(3333)
        recheck()
        check(not repos.install_confirm("reg_mod") and repos.R.jobs["reg_mod"]["state"] == "failed" and not (mods_dir / "reg_mod.sdkmod").exists(),
              "a repository blocked while its mod waited: the OK installs nothing")  # fmt: skip
        repos.trust_again("scan/mods")
        flush_worker()
        repos.review_cancel(["prog_mod", "native_mod", "dyn_mod", "bad_mod"])
        check(repos.install_confirm("plain_mod"), "plain_mod confirmed")
        wait(repos.pending_loads, "plain_mod's load")
        repos.drain()

        # Update: what's new since the installed copy, for each mod in one confirm (Update all)
        scan_v2 = {"net_mod": sdkmod("net_mod", "2.0.0", body="import socket\nimport subprocess\n" + "x = 1\n" * 300),
                   "plain_mod": sdkmod("plain_mod", "1.1.0", body="import json")}  # fmt: skip
        for m, data in scan_v2.items():
            (sg / "v2" / f"{m}.sdkmod").write_bytes(data)
        man_v2 = manifest_for({**{m: ("1.0.0", d) for m, d in scan_v1.items() if m not in scan_v2}, "net_mod": ("2.0.0", scan_v2["net_mod"]),
                               "plain_mod": ("1.1.0", scan_v2["plain_mod"])}, base, name="Scan Mods")  # fmt: skip
        for m in scan_v1:
            if m not in scan_v2:
                man_v2["mods"][m]["url"] = f"{base}/gh/scan/mods/v1/{m}.sdkmod"
        man_v2["release"] = "v2"
        (sg / "latest" / "manifest.json").write_text(json.dumps(man_v2), encoding="utf-8")
        repos.refresh_async(["scan/mods"])
        wait(lambda: not repos.R.refreshing and {"net_mod", "plain_mod"} <= {u["id"] for u in repos.R.updates}, "both updates")
        check(repos.update_async(["net_mod", "plain_mod"]) == 2, "Update all: both downloading")
        wait(lambda: all(repos.R.jobs.get(m, {}).get("state") == "review" for m in scan_v2), "both confirm steps")
        net, plain = repos.R.jobs["net_mod"], repos.R.jobs["plain_mod"]
        check(net["kind"] == "update" and net["new_uses"] == ["runs programs"] and net["uses"] == ["network", "runs programs"],
              f"update: only what's new is called new ({net.get('new_uses')})")  # fmt: skip
        check(net["old_version"] == "1.0.0" and net["version"] == "2.0.0" and net["size"] == len(scan_v2["net_mod"])
              and net["old_size"] == len(scan_v1["net_mod"]) and net["old_read"], "update: version and size change against the installed copy")  # fmt: skip
        check(plain["new_uses"] == [] and plain["uses"] == [], "update: nothing new for the other")
        check(not (updates.staging(game) / "net_mod.sdkmod").exists(), "nothing staged before the OK")
        check(repos.update_confirm(["net_mod", "plain_mod"]) == 2, "one OK for both")
        wait(lambda: len(repos._todo) >= 2, "both reloads queued")
        check((updates.staging(game) / "net_mod.sdkmod").read_bytes() == scan_v2["net_mod"] and downloads("net_mod", "v2") == 1,
              "the confirmed update stages the reviewed bytes: downloaded once")  # fmt: skip
        repos.drain()
        check(sys.modules["net_mod"].VERSION == "2.0.0" and sys.modules["plain_mod"].VERSION == "1.1.0", "both reloaded")
        check(not repos._held, "no download kept after the confirm steps")

        # Clean up for the regions below: this repository and its mods go
        for m in ("net_mod", "plain_mod"):
            repos.remove_mod(m)
        repos.review_cancel(list(repos._held))
        repos.remove_source("scan/mods")
        repos.R.jobs.clear()
        flush_worker()
        # endregion

        # region Saves backed up before another modder's mod loads (a stand-in Save Backups)
        flush_worker()
        import undo  # noqa: PLC0415

        calls: list[tuple[str, float, bool]] = []
        save_backup = types.ModuleType("bl4_save_backup")

        def backup_now(reason: str, timeout: float = 10.0) -> bool:
            calls.append((reason, timeout, repos.pending_loads()))
            return True

        save_backup.backup_now = backup_now  # type: ignore[attr-defined]
        sys.modules["bl4_save_backup"] = save_backup
        wait(lambda: any(d["id"] == "beta_mod" for d in repos.R.discover), "beta_mod on Discover")
        (gh / "v2" / "beta_mod.sdkmod").write_bytes(beta1)  # the v2 release lists beta 2.0.0 too
        install_reviewed("beta_mod")
        wait(repos.pending_loads, "beta_mod's load to be queued")
        check(len(calls) == 1 and "Test beta_mod" in calls[0][0] and calls[0][1] == repos.SAVE_BACKUP_WAIT_S and calls[0][2] is False,
              f"install: Save Backups asked first, with a bounded wait, before the load was queued ({calls})")  # fmt: skip
        repos.drain()
        check(sys.modules["beta_mod"].mod.is_enabled and repos.R.jobs["beta_mod"]["message"].startswith(repos.SAVES_NOTE),
              f"install: loaded; the job says \"Saves backed up first\" ({repos.R.jobs['beta_mod']['message']})")  # fmt: skip
        beta2 = sdkmod("beta_mod", "2.1.0")
        (gh / "v3").mkdir()
        (gh / "v3" / "beta_mod.sdkmod").write_bytes(beta2)
        man3 = manifest_for({"alpha_mod": ("1.1.0", alpha2), "beta_mod": ("2.1.0", beta2)}, base, name="Tester's Mods")
        man3["release"] = "v3"
        (gh / "latest" / "manifest.json").write_text(json.dumps(man3), encoding="utf-8")
        repos.refresh_async(["tester/mods"])
        wait(lambda: not repos.R.refreshing and any(u["id"] == "beta_mod" for u in repos.R.updates), "beta_mod's update")
        update_reviewed(["beta_mod"])
        wait(repos.pending_loads, "beta_mod's reload to be queued")
        check(len(calls) == 2 and calls[1][2] is False, "update: Save Backups asked again before the reload was queued")
        repos.drain()
        check(sys.modules["beta_mod"].VERSION == "2.1.0" and repos.R.jobs["beta_mod"]["message"].startswith(repos.SAVES_NOTE), "update: reloaded, saves noted")
        check(repos._backup_saves(repos.official_key(), {"name": "x"}) == "" and len(calls) == 2, "BL4 Fix's own mods: no backup asked")
        save_backup.backup_now = lambda reason, timeout=10.0: False  # type: ignore[attr-defined]  # off, or too slow
        check(repos._backup_saves("tester/mods", {"name": "x"}) == "", "Save Backups couldn't: no note, the install goes on")
        save_backup.backup_now = lambda reason, timeout=10.0: 1 / 0  # type: ignore[attr-defined]
        check(repos._backup_saves("tester/mods", {"name": "x"}) == "", "Save Backups raising: no note, the install goes on")
        del sys.modules["bl4_save_backup"]
        wait(lambda: any(d["id"] == "gamma_mod" for d in repos.R.discover), "gamma_mod on Discover")
        install_reviewed("gamma_mod")
        wait(repos.pending_loads, "gamma_mod's load to be queued")
        repos.drain()
        check(sys.modules.get("gamma_mod") is not None and not repos.R.jobs["gamma_mod"]["message"].startswith(repos.SAVES_NOTE),
              "without Save Backups the install goes on, with no note")  # fmt: skip
        # endregion

        # region Roll back (undo.py) through the real helper
        script = updates.helper_script(game, process="NoSuchProcessBL4Test")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=120)  # noqa: S603, S607
        rb = mods_dir / updates.ROLLBACK
        check(updates._zip_version(rb / "beta_mod.sdkmod", "beta_mod") == "2.0.0" and updates._zip_version(mods_dir / "beta_mod.sdkmod", "beta_mod") == "2.1.0",
              "helper: the update moved in, the copy it replaced kept in .bl4_rollback")  # fmt: skip
        undo.scan(game)
        listed = {r["id"]: r for r in undo.B.rollbacks}
        check(listed.get("beta_mod", {}).get("installed") == "2.1.0" and listed["beta_mod"]["previous"] == "2.0.0", f"listed: 2.1.0 -> back to 2.0.0 ({undo.B.rollbacks})")
        check(not undo.roll_back_async("beta_mod") or wait(lambda: undo.B.jobs.get("beta_mod", {}).get("state") == "failed", "the refusal"), "(roll back ran)")
        check("restart the game first" in undo.B.jobs["beta_mod"]["message"], "a mod running from its staged update this session can't roll back yet")
        hotload.H.loaded_from.pop("beta_mod", None)  # the next launch: it runs from sdk_mods (2.1.0)
        hotload.reload(game, "beta_mod")
        undo.B.jobs.clear()
        check(sys.modules["beta_mod"].VERSION == "2.1.0", "(next launch: 2.1.0 from sdk_mods)")
        check(undo.roll_back_async("beta_mod"), "roll back started")
        wait(repos.pending_loads, "the roll back's reload to be queued")
        check((updates.staging(game) / "beta_mod.sdkmod").read_bytes() == beta1, "roll back: the 2.0.0 copy staged as an update")
        repos.drain()
        check(sys.modules["beta_mod"].VERSION == "2.0.0" and sys.modules["beta_mod"].mod.is_enabled, "roll back: reloaded to 2.0.0 right away, still on")
        check(undo.B.jobs["beta_mod"]["state"] == "reloaded" and "2.0.0" in repos.R.message["text"], f"roll back: says so ({repos.R.message['text']})")
        check(repos.R.installed["beta_mod"]["version"] == "2.0.0", "roll back: the repository record says 2.0.0")
        flush_worker()
        check(not any(u["id"] == "beta_mod" for u in repos.R.updates), "roll back: 2.1.0 isn't offered again this session")
        check(json.loads(repos.snapshot_json()[1])["undo"]["jobs"]["beta_mod"]["state"] == "reloaded", "the page gets the roll back's state")
        held, updates.U.held = updates.U.held, {}
        repos._recompute()
        check(any(u["id"] == "beta_mod" for u in repos.R.updates), "(the next launch offers it again)")
        updates.U.held = held
        # A copy that can't reload (here: it fails to import) waits for the restart
        check(updates._zip_version(rb / "dropped_mod.sdkmod", "dropped_mod") == "0.7", "(the earlier helper run kept dropped_mod 0.7)")
        undo.scan(game)
        running = sys.modules["dropped_mod"]
        check(undo.roll_back_async("dropped_mod"), "roll back of a broken copy started")
        wait(repos.pending_loads, "its reload to be queued")
        repos.drain()
        check(undo.B.jobs["dropped_mod"]["state"] == "staged" and "when the game closes" in undo.B.jobs["dropped_mod"]["message"],
              f"a copy that can't reload: applies when the game closes ({undo.B.jobs['dropped_mod']['message'][:120]})")  # fmt: skip
        check(sys.modules["dropped_mod"] is running and running.mod.is_enabled, "and the running copy keeps running")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=120)  # noqa: S603, S607
        check(updates._zip_version(mods_dir / "beta_mod.sdkmod", "beta_mod") == "2.0.0" and updates._zip_version(rb / "beta_mod.sdkmod", "beta_mod") == "2.1.0",
              "helper: rolled back in sdk_mods; the newer copy is now the one kept")  # fmt: skip
        check(not updates.staging(game).exists(), "helper: staging cleaned up")
        # endregion

        # region Safe mode: the setup app set other modders' mods aside
        safe = mods_dir / undo.SAFE_DIR
        (safe / "aside_dir").mkdir(parents=True)
        (safe / "aside_mod.sdkmod").write_bytes(sdkmod("aside_mod", "1.0"))
        (safe / "aside_dir" / "__init__.py").write_text("import mods_base\nmod = mods_base.build('Test aside_dir', 'aside_dir')\n", encoding="utf-8")
        (safe / "aside_dir" / "pyproject.toml").write_text('[project]\nname = "Aside Folder"\nversion = "3"\n', encoding="utf-8")
        (safe / "clash_mod.sdkmod").write_bytes(sdkmod("clash_mod", "1.0"))
        (mods_dir / "clash_mod.sdkmod").write_bytes(sdkmod("clash_mod", "2.0"))
        record = {"format": 1, "moved": ["aside_mod.sdkmod", "aside_dir", "clash_mod.sdkmod", "ghost.sdkmod", "../../evil", 5], "time": 1700000000}
        (safe / undo.SAFE_RECORD).write_text(json.dumps(record), encoding="utf-8")
        undo.scan(game)
        info = undo.B.safe or {}
        check(info.get("count") == 3 and [n["file"] for n in info["names"]] == ["aside_mod.sdkmod", "aside_dir", "clash_mod.sdkmod"],
              f"safe mode: the record's entries that are there, in its order ({info})")  # fmt: skip
        check(info["names"][0]["name"] == "Test aside_mod" and info["names"][1]["name"] == "Aside Folder" and info["time"] == 1700000000, "safe mode: names from each mod's pyproject")
        check(json.loads(repos.snapshot_json()[1])["undo"]["safe"]["count"] == 3, "the page gets it (Discover, Repositories)")
        (safe / undo.SAFE_RECORD).write_text("{corrupt", encoding="utf-8")
        undo.scan(game)
        check((undo.B.safe or {}).get("count") == 3, "safe mode: a corrupt record -> the folder's own entries")
        (safe / undo.SAFE_RECORD).write_text(json.dumps(record), encoding="utf-8")
        check(undo.restore_safe_async(), "restore started")
        wait(lambda: not undo.B.restoring, "the restore")
        check((mods_dir / "aside_mod.sdkmod").is_file() and (mods_dir / "aside_dir" / "__init__.py").is_file(), "restore: moved back into sdk_mods")
        check((safe / "clash_mod.sdkmod").is_file() and updates._zip_version(mods_dir / "clash_mod.sdkmod", "clash_mod") == "2.0",
              "restore: a name sdk_mods already has is left where it is (nothing overwritten)")  # fmt: skip
        check("clash_mod.sdkmod" in repos.R.message["text"] and "Put back 2 mods" in repos.R.message["text"], f"restore: says what it did ({repos.R.message['text'][:160]})")
        check(json.loads((safe / undo.SAFE_RECORD).read_text(encoding="utf-8"))["moved"] == ["clash_mod.sdkmod"], "restore: the record lists what's left")
        check((undo.B.safe or {}).get("count") == 1, "restore: the page now says 1 is set aside")
        found = {f["id"]: f["kind"] for f in hotload.H.found}
        check(found.get("aside_mod") == "new" and found.get("aside_dir") == "new", "restore: listed under \"In your mods folder\" for Load")
        hotload.load(game, "aside_mod")
        check(sys.modules["aside_mod"].mod.is_enabled, "a restored mod loads with Load")
        (mods_dir / "clash_mod.sdkmod").unlink()
        undo.restore_safe_async()
        wait(lambda: not undo.B.restoring, "the second restore")
        check(not safe.exists() and undo.B.safe is None and (mods_dir / "clash_mod.sdkmod").is_file(), "restore: all back, the folder and record gone")
        # endregion
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
