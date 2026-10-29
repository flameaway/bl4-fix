"""
Exercises the in-game updater (mods/bl4_mod_menu/updates.py) offline: a fake game folder, the
release files from tools/release.py served by a local http server instead of GitHub, and the
after-exit helper run against the fake folder. Never touches the real game or launches anything.

    python tools/release.py --offline      # once, to produce dist/release
    python dev/updates_test.py
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from functools import partial
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RELEASE = REPO / "dist" / "release"
sys.path.insert(0, str(REPO / "mods" / "bl4_mod_menu"))
import updates  # noqa: E402  # plain Python: no SDK imports at module level

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def with_version(data: bytes, mod_id: str, version: str) -> bytes:
    """The same .sdkmod with another version in its pyproject.toml."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = src.read(item)
            if item.filename == f"{mod_id}/pyproject.toml":
                text = body.decode("utf-8")
                old = next(line for line in text.splitlines() if line.startswith("version = "))
                body = text.replace(old, f'version = "{version}"', 1).encode("utf-8")
            dst.writestr(item, body)
    return out.getvalue()


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    handler = partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> int:  # noqa: PLR0915
    manifest = json.loads((RELEASE / "manifest.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="bl4_upd_test_"))
    site = tmp / "site"
    shutil.copytree(RELEASE, site / manifest["release"])
    server, base = serve(site)
    updates.ALLOW_HTTP = True
    updates.manifest_url = lambda repo: f"{base}/{manifest['release']}/manifest.json"
    updates.asset_url = lambda repo, tag, asset: f"{base}/{tag}/{asset}"
    try:
        game = tmp / "Borderlands 4"
        mods_dir = game / "sdk_mods"
        (mods_dir / "settings").mkdir(parents=True)
        marker = game / updates.SDK_MARKER_REL
        marker.parent.mkdir(parents=True)
        marker.write_text(f'[mod_manager]\ndisplay_version = "{manifest["sdk"]}"\n', encoding="utf-8")
        # Installed: the Mod Menu one version behind, Less Fog current, BL4 Direct as a folder (dev)
        mm = (RELEASE / "bl4_mod_menu.sdkmod").read_bytes()
        (mods_dir / "bl4_mod_menu.sdkmod").write_bytes(with_version(mm, "bl4_mod_menu", "0.0.1"))
        shutil.copyfile(RELEASE / "bl4_less_fog.sdkmod", mods_dir / "bl4_less_fog.sdkmod")
        (mods_dir / "bl4_direct").mkdir()
        (mods_dir / "bl4_direct.sdkmod").write_bytes(with_version((RELEASE / "bl4_direct.sdkmod").read_bytes(), "bl4_direct", "0.0.1"))
        app_dir = mods_dir / updates.APP_DIR
        app_dir.mkdir()
        (app_dir / updates.APP_EXE).write_bytes(b"MZ old app")
        (app_dir / updates.APP_VERSION_FILE).write_text("0.0.1", encoding="utf-8")

        # --- no repo configured ---
        updates.check(game, repo="")
        check(updates.U.status == "off", "no REPO: no check")

        # Ground Loot was installed once and removed (uninstalling keeps its settings file)
        (mods_dir / "settings" / "bl4_ground_loot.json").write_text('{"enabled": false}', encoding="utf-8")

        # --- check ---
        updates.check(game, repo="test/repo")
        snap = updates.snapshot()
        ids = {i["id"] for i in snap["items"]}
        check(snap["status"] == "available", f"check: status available ({snap['status']}: {snap['message']})")
        check(ids == {"bl4_mod_menu"}, f"check: only the outdated .sdkmod is offered {sorted(ids)}")
        check(snap["items"][0]["installed"] == "0.0.1", "check: shows the installed version")
        new_ids = {i["id"] for i in snap["new"]}
        check("bl4_dialog_skipper" in new_ids and "bl4_less_fog" not in new_ids, "check: lists mods not installed")
        check("bl4_direct" not in ids and "bl4_direct" not in new_ids, "check: a folder copy is left alone")

        # --- new mods: announced once, never for a mod the player removed ---
        fresh = set(snap["fresh"])
        check("bl4_ground_loot" in new_ids and "bl4_ground_loot" not in fresh, "new: a removed mod is listed, not announced")
        check("bl4_dialog_skipper" in fresh, "new: a never-installed mod is announced")
        check(fresh == new_ids - {"bl4_ground_loot"}, f"new: every other mod not installed is announced ({len(fresh)})")
        check(snap["updates"] == 1 and snap["count"] == 1 + len(fresh), f"new: badge = 1 update + {len(fresh)} new ({snap['count']})")
        check("new mods" in snap["message"], f"new: the message counts them ({snap['message']})")
        blurbs = {i["id"]: i["blurb"] for i in snap["new"]}
        check(blurbs["bl4_dialog_skipper"] == manifest["mods"]["bl4_dialog_skipper"]["blurb"] != "", "new: carries the setup app's description")
        updates.set_announce(False)
        check(updates.snapshot()["fresh"] == [] and updates.snapshot()["count"] == 1, "new: option off, no badge for new mods")
        check(len(updates.snapshot()["new"]) == len(new_ids), "new: option off, still listed to install")
        updates.set_announce(True)
        updates.mark_seen(sorted(fresh))
        snap = updates.snapshot()
        check(snap["fresh"] == [] and snap["count"] == 1, "seen: badge back to the update only")
        seen_file = mods_dir / "settings" / updates.SEEN_FILE
        for _ in range(50):
            if seen_file.is_file():
                break
            threading.Event().wait(0.05)
        check(set(json.loads(seen_file.read_text(encoding="utf-8"))["seen"]) == fresh, "seen: written to settings")
        updates.U.seen = None  # a new session: read back from the file
        updates.check(game, repo="test/repo")
        check(updates.snapshot()["count"] == 1, "seen: remembered across sessions")
        updates.mark_seen(["bl4_dialog_skipper"])
        check(updates.snapshot()["count"] == 1, "seen: marking twice changes nothing")

        # --- a damaged download stages nothing ---
        good = (site / manifest["release"] / "bl4_mod_menu.sdkmod").read_bytes()
        (site / manifest["release"] / "bl4_mod_menu.sdkmod").write_bytes(good[:-10] + b"0123456789")
        try:
            updates.apply(game, install_new=["bl4_dialog_skipper"])
            check(False, "damaged: apply should fail")
        except RuntimeError as ex:
            check("checksum" in str(ex), f"damaged: refused ({ex})")
        check(not any(updates.staging(game).glob("*")), "damaged: nothing staged (all or nothing)")
        (site / manifest["release"] / "bl4_mod_menu.sdkmod").write_bytes(good)

        # --- apply ---
        updates.U.helper_started = True  # don't start the real helper (it would wait for the game)
        updates.apply(game, install_new=["bl4_dialog_skipper"])
        staged = updates.staging(game)
        names = sorted(p.name for p in staged.iterdir())
        check(names == sorted(["bl4_mod_menu.sdkmod", "bl4_dialog_skipper.sdkmod"]),
              f"apply: staged the update and the new mod, never the setup app {names}")
        check(hashlib.sha256((staged / "bl4_mod_menu.sdkmod").read_bytes()).hexdigest() == manifest["mods"]["bl4_mod_menu"]["sha256"],
              "apply: staged bytes match the manifest")
        check((mods_dir / "bl4_mod_menu.sdkmod").read_bytes() != good, "apply: the loaded .sdkmod isn't touched")
        enabled = json.loads((mods_dir / "settings" / "bl4_dialog_skipper.json").read_text())
        check(enabled.get("enabled") is True, "apply: new mod switched on")
        check(updates.U.status == "ready", "apply: status ready")

        updates.check(game, repo="test/repo")
        check(updates.U.items[0]["state"] == "staged" and updates.U.status == "ready", "recheck: staged update shows as ready")

        # --- the after-exit helper, against the fake folder (a process name that never runs) ---
        script = updates.helper_script(game, process="bl4_no_such_process")
        check("steam://" in script and "relaunch" in script, "helper: relaunch only behind the flag")
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60, check=False)  # fmt: skip
        check(res.returncode == 0, f"helper: ran ({res.stderr.strip()[:200]})")
        check((mods_dir / "bl4_mod_menu.sdkmod").read_bytes() == good, "helper: update moved into sdk_mods")
        check((mods_dir / "bl4_dialog_skipper.sdkmod").is_file(), "helper: new mod moved into sdk_mods")
        check((app_dir / updates.APP_EXE).read_bytes() == b"MZ old app", "helper: leaves the setup app alone")
        check(not staged.exists(), "helper: staging folder removed")

        updates.check(game, repo="test/repo")
        check(updates.U.status == "current" and not updates.U.items, f"after: all current ({updates.U.message})")

        # --- SDK too old for the release ---
        marker.write_text('[mod_manager]\ndisplay_version = "0.1"\n', encoding="utf-8")
        (mods_dir / "bl4_less_fog.sdkmod").write_bytes(with_version((RELEASE / "bl4_less_fog.sdkmod").read_bytes(), "bl4_less_fog", "0.0.1"))
        updates.check(game, repo="test/repo")
        check(updates.U.status == "error" and "SDK" in updates.U.message, "sdk: an old SDK blocks the update with a pointer to setup")

        # --- the setup app's old copy in the game folder (and what older Mod Menus staged of it) ---
        updates.staging(game).mkdir()
        (updates.staging(game) / updates.APP_EXE).write_bytes(b"MZ staged app")
        (updates.staging(game) / updates.APP_VERSION_FILE).write_text("0.0.2", encoding="utf-8")
        appdata = tmp / "appdata"
        programs = appdata / "Microsoft/Windows/Start Menu/Programs"
        programs.mkdir(parents=True)
        link = programs / updates.SHORTCUT_NAME
        link.write_bytes(b"L\0\0\0" + str(app_dir / updates.APP_EXE).encode("utf-16-le") + b"\0\0")
        old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(appdata)
        try:
            with open(app_dir / updates.APP_EXE, "rb"):  # a running copy can't be deleted: kept for now
                updates.remove_old_app(game)
            check((app_dir / updates.APP_EXE).exists() and link.exists(), "old app: an open copy is kept, and its shortcut")
            updates.remove_old_app(game)
            check(not app_dir.exists(), "old app: the game folder's copy and its folder are gone")
            check(not any(updates.staging(game).iterdir()), "old app: the staged copy is gone")
            check(not link.exists(), "old app: the Start menu entry pointing at it is gone")
            app_dir.mkdir()
            (app_dir / updates.APP_EXE).write_bytes(b"MZ old app")
            link.write_bytes(b"L\0\0\0" + r"C:\Users\x\Downloads\BL4FixSetup.exe".encode("utf-16-le"))
            updates.remove_old_app(game)
            check(not app_dir.exists() and link.exists(), "old app: an entry pointing elsewhere is kept")
        finally:
            if old_appdata is not None:
                os.environ["APPDATA"] = old_appdata

        check(updates.newer("0.10.0", "0.9.9") and not updates.newer("0.1.0", "0.1.0"), "versions compare numerically")
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
