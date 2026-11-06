"""
The setup app updating itself from the latest GitHub release.

At launch the app reads the newest release's manifest.json (tools/release.py writes it; the format
is in mods/bl4_mod_menu/updates.py) and compares it with what this exe carries: the app version,
the SDK, and every mod in its payload. It's out of date when the release has a newer app or SDK,
a mod this exe doesn't know (a new mod), or a newer version of a mod. Installing from an old exe
would otherwise leave new mods out (the Mod Menu would then offer them in game, but the app's
Loadout wouldn't list them).

Updating in place: download the release's exe next to this one (size, SHA-256 and "MZ" checked),
rename the running exe to <name>.old (Windows lets a running exe be renamed, not replaced), move
the download to <name>, start it and quit. The new copy deletes the .old file once this process
has gone. The app lives only where the player keeps it (the Start menu entry points there); there's
no copy in the game folder any more (core._settle_app), so the Mod Menu never downloads the app.

Mods update without a new app (fetch_mods, 2026-11-04): before an install the app compares each
mod it's installing with the release (and with what's installed and its own copy), downloads the
newer ones (size and SHA-256 from the signed manifest, the zip's own pyproject.toml version) into
core.mod_cache_dir(), and core.install takes the newest copy. So a release that only updates mods
needs no app update; the "Update App" offer stays for a newer app, SDK or a mod the catalog lacks.

No unreal, no admin: everything here runs as the player, from a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import catalog
import core
import shared
import signing  # a byte-identical copy of mods/bl4_mod_menu/signing.py

# Same repository as the Mod Menu's updater (mods/bl4_mod_menu/updates.py REPO)
REPO = "flameaway/bl4-fix"
MANIFEST_FORMAT = 1
USER_AGENT = "BL4-Fix-Setup"
TIMEOUT_S = 15
MAX_MANIFEST = 1 << 20
MAX_EXE = 256 << 20
MAX_MOD = 64 << 20
CHUNK = 256 << 10
# Tests serve releases from a local http server; real downloads must stay on https
ALLOW_HTTP = False
# The release keys a manifest must be signed by; None = signing.PUBLIC_KEYS (tests sign their own)
TRUSTED_KEYS: tuple[str, ...] | None = None
# The only hosts this app downloads from, like the Mod Menu (updates.github_url): GitHub and where
# it serves release files after a redirect
GITHUB_HOSTS = ("github.com",)
GITHUB_DOMAINS = (".github.com", ".githubusercontent.com")


def _github(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "http" and ALLOW_HTTP:
        return host == "127.0.0.1"
    return parts.scheme == "https" and (host in GITHUB_HOSTS or host.endswith(GITHUB_DOMAINS))


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, PLR0913
        if not _github(newurl):
            raise urllib.error.HTTPError(newurl, code, "refused a redirect away from GitHub", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GitHubRedirects())


def manifest_url(repo: str = REPO) -> str:
    return f"https://github.com/{repo}/releases/latest/download/manifest.json"


def asset_url(tag: str, asset: str, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def releases_page(repo: str = REPO) -> str:
    return f"https://github.com/{repo}/releases/latest"


def _vkey(version: str | None) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version or "")[:4])


def _get(url: str, limit: int, on_bytes: Any = None) -> bytes:
    if not _github(url):
        raise RuntimeError(f"refusing a download that isn't from GitHub: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    with _OPENER.open(req, timeout=TIMEOUT_S) as res:
        if not _github(res.geturl()):
            raise RuntimeError(f"refusing a download that isn't from GitHub: {res.geturl()}")
        chunks, total = [], 0
        while chunk := res.read(CHUNK):
            total += len(chunk)
            if total > limit:
                raise RuntimeError("the download is larger than expected")
            chunks.append(chunk)
            if on_bytes:
                on_bytes(total)
    return b"".join(chunks)


def fetch_manifest(url: str | None = None) -> dict[str, Any]:
    """The release's manifest.json, once its signature by BL4 Fix's release key checks out
    (signing.py, the Mod Menu's copy): the app it lists is only downloaded from a signed release."""
    url = url or manifest_url()
    data = _get(url, MAX_MANIFEST)
    try:
        signature = _get(url + signing.SIGNATURE_SUFFIX, 4096)
    except urllib.error.HTTPError as ex:
        ex.close()  # (its response would be left open: a ResourceWarning in the log)
        if ex.code != 404:  # noqa: PLR2004
            raise
        raise RuntimeError("the newest release isn't signed by BL4 Fix's release key") from ex
    try:
        signing.verify(data, signature, TRUSTED_KEYS)
    except signing.SignatureError as ex:
        raise RuntimeError(str(ex)) from ex
    manifest = json.loads(data.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise RuntimeError("the release's manifest.json is in a format this app doesn't know")
    if not isinstance(manifest.get("mods"), dict) or not isinstance(manifest.get("app"), dict) or not manifest.get("release"):
        raise RuntimeError("the release's manifest.json is incomplete")
    return manifest


def compare(manifest: dict[str, Any]) -> dict[str, Any]:
    """What the release has that this exe doesn't. `stale` is True if any of it is set."""
    app = manifest["app"]
    new_mods, newer_mods = [], []
    for mod_id, info in manifest["mods"].items():
        if not isinstance(info, dict):
            continue
        if mod_id not in catalog.BY_ID:
            new_mods.append(str(info.get("name") or mod_id))
        elif _vkey(str(info.get("version", ""))) > _vkey(core.sdkmod_version(core.payload_mod(mod_id), mod_id)):
            newer_mods.append(catalog.BY_ID[mod_id].name)
    newer_app = _vkey(str(app.get("version", ""))) > _vkey(shared.MOD_VERSION)
    newer_sdk = _vkey(str(manifest.get("sdk", ""))) > _vkey(shared.SDK_VERSION)
    return {
        # Newer mods alone don't make the app stale: it downloads them itself (fetch_mods)
        "stale": bool(newer_app or newer_sdk or new_mods),
        # Every catalog mod's version in the release, for the page's per-mod update lines
        "mods": {
            mod_id: str(info.get("version", ""))
            for mod_id, info in manifest["mods"].items()
            if isinstance(info, dict) and mod_id in catalog.BY_ID
        },
        "release": str(manifest["release"]),
        "version": str(app.get("version", "")),
        "current": shared.MOD_VERSION,
        "new_mods": new_mods,
        "newer_mods": newer_mods,
        "sdk": newer_sdk,
        "size": int(app.get("size") or 0),
    }


def mod_updates(manifest: dict[str, Any], game_dir: Path, mods: set[str] | frozenset[str]) -> list[str]:
    """
    The mods in `mods` whose release version is newer than both the installed copy and the
    newest this app has (its payload or an earlier download): the ones fetch_mods downloads.
    """
    sdk_mods = game_dir / "sdk_mods"
    wanted = []
    for mod_id in sorted(mods):
        info = manifest["mods"].get(mod_id)
        if mod_id not in catalog.BY_ID or not isinstance(info, dict):
            continue
        release = _vkey(str(info.get("version", "")))
        local = max(
            _vkey(core.sdkmod_version(core.mod_source(mod_id), mod_id)),
            _vkey(core.sdkmod_version(sdk_mods / f"{mod_id}.sdkmod", mod_id)),
        )
        if release > local:
            wanted.append(mod_id)
    return wanted


def download_mod(manifest: dict[str, Any], mod_id: str, url: str | None = None) -> Path:
    """
    Downloads one mod of the release into core.mod_cache_dir() once its size, SHA-256 (from the
    signed manifest) and its own pyproject.toml version check out. Returns the file.
    """
    info = manifest["mods"][mod_id]
    size, digest, version = int(info.get("size") or 0), str(info.get("sha256") or ""), str(info.get("version") or "")
    asset = str(info.get("asset") or f"{mod_id}.sdkmod")
    if not size or not digest or not version or "/" in asset or "\\" in asset:
        raise RuntimeError(f"the release doesn't list {mod_id} properly")
    data = _get(url or asset_url(str(manifest["release"]), asset), min(MAX_MOD, size + 1))
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError(f"the {mod_id} download doesn't match the release (size or SHA-256)")
    target = core.cached_mod(mod_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download")
    tmp.write_bytes(data)
    try:
        if core.sdkmod_version(tmp, mod_id) != version:
            raise RuntimeError(f"the {mod_id} download isn't version {version}")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def fetch_mods(game_dir: Path, mods: set[str] | frozenset[str], log=print, manifest: dict[str, Any] | None = None) -> None:  # noqa: ANN001
    """
    Before an install: downloads the release's newer copies of `mods` (mod_updates). Best effort:
    offline, or a download that fails, leaves that mod to the copy this app already has.
    """
    if manifest is None:
        _, manifest = check()
        if manifest is None:
            log("Couldn't reach GitHub for mod updates: installing this app's copies.")
            return
    wanted = mod_updates(manifest, game_dir, mods)
    for i, mod_id in enumerate(wanted, 1):
        name, version = catalog.BY_ID[mod_id].name, manifest["mods"][mod_id].get("version")
        log(f"[{i}/{len(wanted)}] Downloading {name} {version}...")
        try:
            download_mod(manifest, mod_id)
        except Exception as ex:  # noqa: BLE001 - keep going: the install uses the copy it has
            log(f"Couldn't download {name} {version} ({ex}): installing this app's copy.")
    if wanted:
        log(f"Downloaded {len(wanted)} mod update(s) from {manifest['release']}.")


def check(url: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(compare() result, manifest), or (None, None) when offline or the release can't be read."""
    try:
        manifest = fetch_manifest(url)
        return compare(manifest), manifest
    except Exception:  # noqa: BLE001 - offline, GitHub down, a bad manifest: no update offer
        return None, None


def this_exe() -> Path | None:
    """The running exe (None when run from source: a dev checkout updates through git)."""
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None


def download(manifest: dict[str, Any], log=print, url: str | None = None) -> bytes:  # noqa: ANN001
    app = manifest["app"]
    size, digest = int(app.get("size") or 0), str(app.get("sha256") or "")
    if not size or not digest or not app.get("asset"):
        raise RuntimeError("the release doesn't list its setup app")
    step = [0]

    def progress(n: int) -> None:
        tenth = n * 10 // size
        if tenth > step[0]:
            step[0] = tenth
            log(f"Downloading BL4 Fix Setup {app.get('version')}: {min(100, tenth * 10)}%")

    data = _get(url or asset_url(str(manifest["release"]), str(app["asset"])), min(MAX_EXE, size + 1), progress)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("the download doesn't match the release (size or SHA-256): try again")
    if not data.startswith(b"MZ"):
        raise RuntimeError("the download isn't a Windows program")
    return data


def swap_in(exe: Path, data: bytes) -> None:
    """Puts `data` in place of the (running) `exe`; the old one becomes <name>.old."""
    new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
    try:
        new.write_bytes(data)
    except OSError as ex:
        raise RuntimeError(
            f"can't write next to the app ({exe.parent}): download the new version from {releases_page()}",
        ) from ex
    try:
        old.unlink(missing_ok=True)
        exe.rename(old)
    except OSError as ex:
        new.unlink(missing_ok=True)
        raise RuntimeError(f"couldn't move the old app aside ({ex.strerror or ex})") from ex
    try:
        new.rename(exe)
    except OSError as ex:
        old.rename(exe)  # put the old one back: never leave no app at all
        new.unlink(missing_ok=True)
        raise RuntimeError(f"couldn't put the new app in place ({ex.strerror or ex})") from ex


def update(manifest: dict[str, Any], log=print) -> Path:  # noqa: ANN001
    """Downloads and swaps in the release's app. Returns the exe to start. Raises on any failure."""
    exe = this_exe()
    if exe is None:
        raise RuntimeError("running from source: update the checkout instead")
    data = download(manifest, log)
    swap_in(exe, data)
    log(f"Updated BL4 Fix Setup to {manifest['app'].get('version')} ({manifest['release']}). Restarting...")
    return exe


def restart(exe: Path) -> None:
    subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)  # noqa: S603


def finish_update() -> bool:
    """At startup: whether this run follows an update (an .old copy is left), and remove it once free."""
    exe = this_exe()
    if exe is None:
        return False
    old = exe.with_name(exe.name + ".old")
    if not old.exists():
        return False

    def remove() -> None:
        # The old process holds it until it has exited (PyInstaller's bootloader cleans up first)
        for _ in range(60):
            try:
                old.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.5)

    threading.Thread(target=remove, daemon=True).start()
    return True


def open_releases_page() -> None:
    os.startfile(releases_page())  # noqa: S606
