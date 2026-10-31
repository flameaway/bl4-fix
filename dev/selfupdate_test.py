"""
Offline checks of the setup app's self-update (installer/selfupdate.py): a release served by a
local http server instead of GitHub, a fake exe in a temp folder instead of the running app.

    python installer/build.py      # once, for installer/build/payload (the versions compared)
    python dev/selfupdate_test.py
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "installer"))

import catalog  # noqa: E402
import core  # noqa: E402
import selfupdate  # noqa: E402
import shared  # noqa: E402

sys.path.insert(0, str(REPO / "tools"))
import release_key  # noqa: E402  # Windows' crypto helpers (signs with throwaway keys here)

results: list[tuple[bool, str]] = []


def throwaway_key() -> bytes:
    """A new ECDSA P-256 private key blob, never saved."""
    import ctypes  # noqa: PLC0415

    _, bc = release_key._libs()
    alg, key = release_key.handle(), release_key.handle()
    release_key._ok(bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "ECDSA_P256", None, 0), "open")
    try:
        release_key._ok(bc.BCryptGenerateKeyPair(alg, ctypes.byref(key), 256, 0), "generate")
        release_key._ok(bc.BCryptFinalizeKeyPair(key, 0), "finalize")
        try:
            return release_key._export_bc(bc, key, "ECCPRIVATEBLOB")
        finally:
            bc.BCryptDestroyKey(key)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)


def sign(private: bytes, path: Path) -> None:
    raw = release_key._sign_backup(private, hashlib.sha256(path.read_bytes()).digest())
    path.with_name(path.name + ".sig").write_bytes(base64.b64encode(raw) + b"\n")


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def manifest_like_payload(exe: bytes, app_version: str = shared.MOD_VERSION) -> dict:
    """A release identical to this build's payload."""
    mods = {
        info.id: {"name": info.name, "version": core.sdkmod_version(core.payload_mod(info.id), info.id)}
        for info in catalog.MODS
    }
    return {
        "format": 1, "release": "v-test", "sdk": shared.SDK_VERSION, "mods": mods,
        "app": {"version": app_version, "asset": "BL4FixSetup.exe", "sha256": hashlib.sha256(exe).hexdigest(), "size": len(exe)},
    }  # fmt: skip


def main() -> int:
    selfupdate.ALLOW_HTTP = True
    tmp = Path(tempfile.mkdtemp(prefix="bl4_selfupdate_"))
    release = tmp / "release"
    release.mkdir()
    new_exe = b"MZ new app " + b"x" * 300_000
    (release / "BL4FixSetup.exe").write_bytes(new_exe)
    server, base = serve(release)
    try:
        # --- compare ---
        same = manifest_like_payload(new_exe)
        check(not selfupdate.compare(same)["stale"], "same release as this build: not stale")
        m = json.loads(json.dumps(same))
        m["mods"]["bl4_brand_new"] = {"name": "Brand New", "version": "0.1.0"}
        r = selfupdate.compare(m)
        check(r["stale"] and r["new_mods"] == ["Brand New"], f"a mod this exe doesn't know: stale, named {r['new_mods']}")
        m = json.loads(json.dumps(same))
        m["mods"]["bl4_minimap"]["version"] = "99.0.0"
        r = selfupdate.compare(m)
        check(r["stale"] and r["newer_mods"] == ["Minimap"], f"a newer mod: stale {r['newer_mods']}")
        m = json.loads(json.dumps(same))
        m["mods"]["bl4_minimap"]["version"] = "0.0.1"
        check(not selfupdate.compare(m)["stale"], "an older mod in the release: not stale")
        check(selfupdate.compare(manifest_like_payload(new_exe, "99.0"))["stale"], "a newer app version: stale")
        m = json.loads(json.dumps(same))
        m["sdk"] = "9.9"
        check(selfupdate.compare(m)["sdk"], "a newer SDK: stale")

        # --- check (network) ---
        (release / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        r, manifest = selfupdate.check(f"{base}/manifest.json")
        check(r is None, "check: an unsigned release is no offer")
        private = throwaway_key()
        selfupdate.TRUSTED_KEYS = (base64.b64encode(release_key.backup_public(private)).decode(),)
        sign(private, release / "manifest.json")
        r, manifest = selfupdate.check(f"{base}/manifest.json")
        check(r is not None and r["stale"] and manifest["release"] == "v-test", "check: reads the served (signed) manifest")
        signed = (release / "manifest.json").read_bytes()
        (release / "manifest.json").write_bytes(signed.replace(b"v-test", b"v-tesT"))
        check(selfupdate.check(f"{base}/manifest.json") == (None, None), "check: a changed manifest is no offer (signature)")
        (release / "manifest.json").write_bytes(signed)
        try:
            selfupdate._get("https://example.com/BL4FixSetup.exe", 100)
            check(False, "a download from anywhere but GitHub is refused")
        except RuntimeError as ex:
            check("isn't from GitHub" in str(ex), "a download from anywhere but GitHub is refused")
        check(selfupdate.check(f"{base}/missing.json") == (None, None), "check: a missing manifest is no offer, no error")
        check(selfupdate.check("http://127.0.0.1:9/manifest.json") == (None, None), "check: offline is no offer, no error")
        (release / "bad.json").write_text('{"format": 2}', encoding="utf-8")
        check(selfupdate.check(f"{base}/bad.json") == (None, None), "check: an unknown format is no offer")
        selfupdate.ALLOW_HTTP = False
        check(selfupdate.check(f"{base}/manifest.json") == (None, None), "check: plain http refused outside tests")
        selfupdate.ALLOW_HTTP = True

        # --- download ---
        url = f"{base}/BL4FixSetup.exe"
        logs: list[str] = []
        check(selfupdate.download(same, logs.append, url) == new_exe, "download: verified bytes")
        check(any("100%" in line for line in logs), f"download: progress logged ({len(logs)} lines)")
        bad = json.loads(json.dumps(same))
        bad["app"]["sha256"] = "0" * 64
        try:
            selfupdate.download(bad, print, url)
            check(False, "download: wrong SHA-256 refused")
        except RuntimeError:
            check(True, "download: wrong SHA-256 refused")
        (release / "notexe.bin").write_bytes(b"PK" + b"y" * 100)
        notexe = json.loads(json.dumps(same))
        notexe["app"].update(size=102, sha256=hashlib.sha256(b"PK" + b"y" * 100).hexdigest())
        try:
            selfupdate.download(notexe, print, f"{base}/notexe.bin")
            check(False, "download: a non-exe refused")
        except RuntimeError:
            check(True, "download: a non-exe refused")

        # --- swap in place, then the new copy's cleanup ---
        app_dir = tmp / "downloads"
        app_dir.mkdir(parents=True)
        exe = app_dir / "BL4FixSetup.exe"
        exe.write_bytes(b"MZ old app")
        selfupdate.swap_in(exe, new_exe)
        check(exe.read_bytes() == new_exe, "swap: the new app is in place")
        old = exe.with_name(exe.name + ".old")
        check(old.read_bytes() == b"MZ old app", "swap: the old app is kept as .old")
        check(not exe.with_name(exe.name + ".new").exists(), "swap: no .new left")
        selfupdate.this_exe = lambda: exe
        check(selfupdate.finish_update(), "next start: sees it follows an update")
        for _ in range(20):
            if not old.exists():
                break
            time.sleep(0.1)
        check(not old.exists(), "next start: .old removed")
        check(not selfupdate.finish_update(), "the start after: no update notice")

        # --- the whole update, from the served release ---
        selfupdate.asset_url = lambda tag, asset, repo=selfupdate.REPO: f"{base}/{asset}"
        exe.write_bytes(b"MZ old again")
        got = selfupdate.update(same, logs.append)
        check(got == exe and exe.read_bytes() == new_exe, "update: downloaded and swapped in")
        selfupdate.this_exe = lambda: None
        try:
            selfupdate.update(same, print)
            check(False, "update: refused when running from source")
        except RuntimeError:
            check(True, "update: refused when running from source")
    finally:
        server.shutdown()

    failed = [what for ok, what in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
