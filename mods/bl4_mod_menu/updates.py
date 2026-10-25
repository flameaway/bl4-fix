"""
Mod updates from GitHub: a check at launch, then "Update all" on the Mod Menu's Updates page.

Each GitHub release of the repo carries every mod's .sdkmod plus a manifest.json made by
tools/release.py:

    {"format": 1, "release": "<tag>", "sdk": "0.3",
     "mods": {"<id>": {"name", "version", "asset", "sha256", "size", "blurb", "notes"}},
     "app": {"version", "asset", "sha256", "size"}}

The check downloads https://github.com/<REPO>/releases/latest/download/manifest.json (a plain
redirect to the newest release's file, so no API rate limit) and compares each mod with the
version in the pyproject.toml inside the installed sdk_mods/<id>.sdkmod. Mods running from a folder
(a dev junction, a manual copy) are left alone.

Updating never touches a loaded mod: a running game keeps reading its .sdkmod files (zipimport
caches their directory, mods read their assets from them), so replacing one under it could give
mismatched or broken reads, the same way a replaced pak does. Downloads are verified (size, SHA-256,
the mod's folder and version inside) and staged in sdk_mods/.bl4_updates/ (dot folders aren't
loaded as mods). A hidden helper waits until the game has closed, moves them into sdk_mods, and
starts the game again if the player chose Restart. The setup app's copy in the game folder
(sdk_mods/.bl4_direct/) is updated the same way, so a later repair can't put old mods back.

Plain Python only (network and file I/O, never unreal calls): everything runs on worker threads
and the game thread only reads snapshot().
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

try:
    from unrealsdk import logging
except ImportError:  # offline tests
    logging = None  # type: ignore[assignment]

# The GitHub repository the releases come from, "owner/name". Empty: no update checks at all.
REPO = "flameaway/bl4-direct"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1
USER_AGENT = "BL4-Mod-Menu-Updater"
TIMEOUT_S = 20
MAX_MANIFEST = 1 << 20
MAX_ASSET = 64 << 20
CHUNK = 64 << 10
STAGING = ".bl4_updates"  # in sdk_mods
APP_DIR = ".bl4_direct"  # in sdk_mods: the setup app's copy (installer core.TOOLS_DIR_REL)
APP_EXE = "BL4DirectSetup.exe"
APP_VERSION_FILE = "version.txt"
RELAUNCH_FLAG = "relaunch"
STEAM_APP_ID = "1285190"  # bl4_direct.shared.STEAM_APP_ID
SDK_MARKER_REL = Path("OakGame/Binaries/Win64/Plugins/unrealsdk.toml")
# Tests serve releases from a local http server; real downloads must stay on https
ALLOW_HTTP = False


def _log(text: str, *, error: bool = False) -> None:
    if logging is not None:
        (logging.error if error else logging.info)(f"[Mod Menu] {text}")


def manifest_url(repo: str) -> str:
    return f"https://github.com/{repo}/releases/latest/download/{MANIFEST_NAME}"


def asset_url(repo: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


def releases_page(repo: str) -> str:
    return f"https://github.com/{repo}/releases/latest" if repo else ""


def version_key(version: str) -> tuple[int, ...]:
    """'0.10.2' -> (0, 10, 2); anything without numbers sorts first."""
    return tuple(int(n) for n in re.findall(r"\d+", version or "")[:4])


def newer(latest: str, installed: str | None) -> bool:
    return installed is None or version_key(latest) > version_key(installed)


class U:
    """Updater state. Worker threads write it under _lock; snapshot() reads it."""

    status = "idle"  # off idle checking current available downloading ready error
    message = ""
    release = ""
    items: list[dict[str, Any]] = []  # mods with an update: id, name, installed, latest, notes, state
    new: list[dict[str, Any]] = []  # mods in the release that aren't installed: id, name, latest, blurb
    progress = (0, 0)
    revision = 0
    manifest: dict[str, Any] | None = None
    game_dir: Path | None = None
    repo = REPO
    busy = False
    helper_started = False
    checked_at = 0.0


_lock = threading.Lock()


def _set(**changes: Any) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(U, key, value)
        U.revision += 1


def can_restart(game_dir: Path | None) -> bool:
    """Whether the helper can start the game again (Steam installs: through Steam)."""
    return game_dir is not None and "steamapps" in str(game_dir).lower()


def snapshot() -> dict[str, Any]:
    """What the page shows. Cheap: built from the state as it is."""
    with _lock:
        return {
            "rev": U.revision,
            "status": U.status,
            "message": U.message,
            "release": U.release,
            "items": list(U.items),
            "new": list(U.new),
            "progress": list(U.progress),
            "count": sum(1 for i in U.items if i["state"] == "update"),
            "can_restart": can_restart(U.game_dir),
            "page": releases_page(U.repo),
        }


# region Reading what's installed


def sdk_mods(game_dir: Path) -> Path:
    return game_dir / "sdk_mods"


def staging(game_dir: Path) -> Path:
    return sdk_mods(game_dir) / STAGING


def _zip_version(path: Path, mod_id: str) -> str | None:
    """The version in <id>/pyproject.toml inside an .sdkmod, or None if it can't be read."""
    try:
        with zipfile.ZipFile(path) as zf:
            data = tomllib.loads(zf.read(f"{mod_id}/pyproject.toml").decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    version = data.get("tool", {}).get("sdkmod", {}).get("version") or data.get("project", {}).get("version")
    return str(version) if version else None


def _running_from_folder(mod_id: str) -> bool:
    """The game loaded this mod from a folder (a dev junction or a manual copy), not an .sdkmod."""
    module = sys.modules.get(mod_id)
    path = getattr(module, "__file__", None) or ""
    return bool(path) and ".sdkmod" not in path.lower()


def installed_versions(game_dir: Path, ids: list[str]) -> dict[str, str | None]:
    """{id: version} for mods installed as .sdkmod files we may update. Folder copies are skipped."""
    found: dict[str, str | None] = {}
    for mod_id in ids:
        path = sdk_mods(game_dir) / f"{mod_id}.sdkmod"
        if not path.is_file() or (sdk_mods(game_dir) / mod_id).is_dir() or _running_from_folder(mod_id):
            continue
        found[mod_id] = _zip_version(path, mod_id)
    return found


def _present(game_dir: Path, mod_id: str) -> bool:
    """Installed in any form (an .sdkmod, a folder, or loaded from elsewhere)."""
    return (
        (sdk_mods(game_dir) / f"{mod_id}.sdkmod").exists()
        or (sdk_mods(game_dir) / mod_id).is_dir()
        or mod_id in sys.modules
    )


def installed_sdk(game_dir: Path) -> str | None:
    try:
        text = (game_dir / SDK_MARKER_REL).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'\[mod_manager\][^\[]*display_version\s*=\s*"([^"\s]+)', text)
    return match.group(1) if match else None


def _app_version(game_dir: Path) -> str | None:
    try:
        return (sdk_mods(game_dir) / APP_DIR / APP_VERSION_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# endregion
# region Network


def _get(url: str, limit: int, progress: Any = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - https only
        if not (response.geturl().startswith("https://") or ALLOW_HTTP):
            raise RuntimeError(f"refused a non-https download: {response.geturl()}")
        data = bytearray()
        while chunk := response.read(CHUNK):
            data += chunk
            if len(data) > limit:
                raise RuntimeError(f"{url} is larger than expected")
            if progress is not None:
                progress(len(data))
    return bytes(data)


def fetch_manifest(repo: str) -> dict[str, Any]:
    manifest = json.loads(_get(manifest_url(repo), MAX_MANIFEST).decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise RuntimeError("the release's manifest.json is in a format this Mod Menu doesn't know; update it with BL4 Mods Setup")
    if not isinstance(manifest.get("mods"), dict) or not manifest.get("release"):
        raise RuntimeError("the release's manifest.json is incomplete")
    return manifest


# endregion
# region Check


def compare(game_dir: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(updates, new mods) for this game folder and release."""
    mods: dict[str, dict[str, Any]] = manifest["mods"]
    versions = installed_versions(game_dir, list(mods))
    staged_dir = staging(game_dir)
    items, new = [], []
    for mod_id, info in mods.items():
        latest = str(info.get("version", ""))
        name = str(info.get("name") or mod_id)
        if mod_id in versions:
            if not newer(latest, versions[mod_id]):
                continue
            staged = _zip_version(staged_dir / f"{mod_id}.sdkmod", mod_id) == latest
            items.append({
                "id": mod_id, "name": name, "installed": versions[mod_id] or "?", "latest": latest,
                "notes": str(info.get("notes") or ""), "state": "staged" if staged else "update",
            })  # fmt: skip
        elif not _present(game_dir, mod_id):
            staged = _zip_version(staged_dir / f"{mod_id}.sdkmod", mod_id) == latest
            new.append({
                "id": mod_id, "name": name, "latest": latest, "blurb": str(info.get("blurb") or ""),
                "state": "staged" if staged else "new",
            })  # fmt: skip
    items.sort(key=lambda i: i["name"].lower())
    new.sort(key=lambda i: i["name"].lower())
    return items, new


def _sdk_problem(game_dir: Path, manifest: dict[str, Any]) -> str:
    wanted = str(manifest.get("sdk") or "")
    have = installed_sdk(game_dir)
    if wanted and have and version_key(have) < version_key(wanted):
        return (
            f"This release needs Python SDK {wanted} (you have {have}): get the new BL4 Mods Setup from"
            f" {releases_page(U.repo)} and run it."
        )
    return ""


def check(game_dir: Path, repo: str | None = None) -> None:
    """Fetches the manifest and works out what can be updated. Blocking: call from a worker."""
    repo = U.repo if repo is None else repo
    if not repo:
        _set(status="off", message="Update checks aren't set up in this build.", game_dir=game_dir)
        return
    _set(status="checking", message="Checking GitHub for updates...", game_dir=game_dir, repo=repo)
    try:
        manifest = fetch_manifest(repo)
        items, new = compare(game_dir, manifest)
    except Exception as ex:  # noqa: BLE001 - offline, GitHub down, a bad release: say so, nothing else
        _set(status="error", message=f"Couldn't check for updates: {ex}", checked_at=time.monotonic())
        _log(f"update check failed: {ex}")
        return
    problem = _sdk_problem(game_dir, manifest)
    waiting = [i for i in items if i["state"] == "update"]
    staged = any(i["state"] == "staged" for i in items + new)
    if problem:
        status, message = "error", problem
    elif waiting:
        status, message = "available", f"{len(waiting)} mod update(s) available ({manifest['release']})."
    elif staged:
        status, message = "ready", "Updates are downloaded: they install when the game closes."
    else:
        status, message = "current", "All mods are up to date."
    _set(status=status, message=message, items=items, new=new, release=str(manifest["release"]),
         manifest=manifest, checked_at=time.monotonic())  # fmt: skip
    _log(message)
    if staged:
        start_helper(game_dir)


def check_async(game_dir: Path) -> bool:
    """Starts a check on a worker thread unless one is running. Returns whether it started."""
    with _lock:
        if U.busy:
            return False
        U.busy = True

    def run() -> None:
        try:
            check(game_dir)
        finally:
            _set(busy=False)

    threading.Thread(target=run, name="bl4_mod_menu.updates", daemon=True).start()
    return True


# endregion
# region Download and stage


def _verify_mod(data: bytes, mod_id: str, latest: str) -> None:
    import io  # noqa: PLC0415

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if f"{mod_id}/__init__.py" not in names or any(not n.startswith(f"{mod_id}/") for n in names):
                raise RuntimeError(f"{mod_id}: the download isn't a {mod_id} mod")
            if zf.testzip() is not None:
                raise RuntimeError(f"{mod_id}: the download is damaged")
            data_toml = tomllib.loads(zf.read(f"{mod_id}/pyproject.toml").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as ex:
        raise RuntimeError(f"{mod_id}: the download isn't a valid mod ({ex})") from ex
    version = data_toml.get("tool", {}).get("sdkmod", {}).get("version") or data_toml.get("project", {}).get("version")
    if str(version) != latest:
        raise RuntimeError(f"{mod_id}: the download is version {version}, expected {latest}")


def _download(repo: str, tag: str, info: dict[str, Any], on_bytes: Any) -> bytes:
    asset = str(info["asset"])
    if "/" in asset or "\\" in asset:
        raise RuntimeError(f"bad asset name {asset!r}")
    size = int(info.get("size") or 0)
    data = _get(asset_url(repo, tag, asset), min(MAX_ASSET, size + 1024) if size else MAX_ASSET, on_bytes)
    if size and len(data) != size:
        raise RuntimeError(f"{asset}: got {len(data)} bytes, expected {size}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != str(info.get("sha256", "")).lower():
        raise RuntimeError(f"{asset}: checksum mismatch")
    return data


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _enable_setting(game_dir: Path, mod_id: str) -> None:
    """A newly installed mod starts switched on (like the setup app does)."""
    path = sdk_mods(game_dir) / "settings" / f"{mod_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["enabled"] = True
    _write(path, json.dumps(data, indent=4).encode("utf-8"))


def apply(game_dir: Path, install_new: list[str] | None = None) -> None:
    """
    Downloads every pending update (plus the new mods asked for), verifies and stages them, and
    starts the helper that installs them once the game closes. Blocking: call from a worker.
    """
    manifest = U.manifest
    if manifest is None:
        raise RuntimeError("check for updates first")
    repo, tag = U.repo, str(manifest["release"])
    wanted = [i["id"] for i in U.items if i["state"] == "update"]
    wanted += [i["id"] for i in U.new if i["state"] == "new" and i["id"] in set(install_new or [])]
    if problem := _sdk_problem(game_dir, manifest):
        raise RuntimeError(problem)
    if not wanted:
        _set(status="ready" if any(i["state"] == "staged" for i in U.items + U.new) else "current")
        return

    infos = [(mod_id, manifest["mods"][mod_id]) for mod_id in wanted]
    app = manifest.get("app")
    want_app = isinstance(app, dict) and (sdk_mods(game_dir) / APP_DIR / APP_EXE).is_file() and newer(
        str(app.get("version", "")), _app_version(game_dir)
    )
    total = sum(int(info.get("size") or 0) for _, info in infos) + (int(app.get("size") or 0) if want_app else 0)  # type: ignore[union-attr]
    done = 0
    _set(status="downloading", message=f"Downloading {len(infos)} mod(s)...", progress=(0, total))

    # All or nothing: mods of one release go together (the Mod Menu and the mods with pages in it),
    # so a failure takes back whatever this run staged
    staged_dir = staging(game_dir)
    written: list[Path] = []
    try:
        downloads: list[tuple[str, bytes]] = []
        for mod_id, info in infos:
            base = done
            data = _download(repo, tag, info, lambda n, base=base: _set(progress=(base + n, total)))
            _verify_mod(data, mod_id, str(info["version"]))
            downloads.append((mod_id, data))
            done += len(data)
        app_data = b""
        if want_app:
            base = done
            app_data = _download(repo, tag, app, lambda n: _set(progress=(base + n, total)))  # type: ignore[arg-type]
            if not app_data.startswith(b"MZ"):
                raise RuntimeError("the setup app download isn't a Windows program")
        for mod_id, data in downloads:
            written.append(staged_dir / f"{mod_id}.sdkmod")
            _write(written[-1], data)
        if app_data:
            written += [staged_dir / APP_EXE, staged_dir / APP_VERSION_FILE]
            _write(staged_dir / APP_EXE, app_data)
            _write(staged_dir / APP_VERSION_FILE, str(app["version"]).encode("utf-8"))  # type: ignore[index]
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for mod_id, _ in downloads:
        if any(n["id"] == mod_id for n in U.new):
            _enable_setting(game_dir, mod_id)

    items = [{**i, "state": "staged"} if i["id"] in wanted else i for i in U.items]
    new = [{**i, "state": "staged"} if i["id"] in wanted else i for i in U.new]
    restart = "Restart the game" if can_restart(game_dir) else "Quit and start the game again"
    _set(status="ready", items=items, new=new, progress=(total, total),
         message=f"Downloaded {len(infos)} mod(s). {restart} to finish; they install when the game closes.")  # fmt: skip
    _log(U.message)
    start_helper(game_dir)


def apply_async(game_dir: Path, install_new: list[str] | None = None) -> bool:
    with _lock:
        if U.busy:
            return False
        U.busy = True

    def run() -> None:
        try:
            apply(game_dir, install_new)
        except Exception as ex:  # noqa: BLE001
            _set(status="error", message=f"Update failed: {ex}. Nothing was changed.")
            _log(f"update failed: {ex}", error=True)
        finally:
            _set(busy=False)

    threading.Thread(target=run, name="bl4_mod_menu.updates", daemon=True).start()
    return True


# endregion
# region Installing after the game closes


def _ps(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def helper_script(game_dir: Path, process: str = "Borderlands4") -> str:
    """The PowerShell that waits for the game to close, then moves the staged files into place."""
    staged, mods_dir = staging(game_dir), sdk_mods(game_dir)
    app_dir = mods_dir / APP_DIR
    launch = f"steam://rungameid/{STEAM_APP_ID}"
    return (
        # Every copy of the game, not just this process (two instances, a dev tool)
        f"while (Get-Process -Name {process} -ErrorAction SilentlyContinue) {{ Start-Sleep 2 }}; "
        f"$s = {_ps(staged)}; $m = {_ps(mods_dir)}; $a = {_ps(app_dir)}; "
        "for ($i = 0; $i -lt 30; $i++) { try { "
        "Get-ChildItem -LiteralPath $s -Filter *.sdkmod -File | ForEach-Object { "
        "Move-Item -LiteralPath $_.FullName -Destination (Join-Path $m $_.Name) -Force -ErrorAction Stop }; "
        f"if (Test-Path -LiteralPath (Join-Path $s '{APP_EXE}')) {{ "
        "New-Item -ItemType Directory -Force -Path $a | Out-Null; "
        f"Move-Item -LiteralPath (Join-Path $s '{APP_EXE}') -Destination (Join-Path $a '{APP_EXE}') -Force -ErrorAction Stop; "
        f"Move-Item -LiteralPath (Join-Path $s '{APP_VERSION_FILE}') -Destination (Join-Path $a '{APP_VERSION_FILE}') -Force -ErrorAction SilentlyContinue }}; "
        "break } catch { Start-Sleep 1 } }; "
        f"$r = Test-Path -LiteralPath (Join-Path $s '{RELAUNCH_FLAG}'); "
        "if (-not (Get-ChildItem -LiteralPath $s -Filter *.sdkmod -File -ErrorAction SilentlyContinue)) { "
        "Remove-Item -LiteralPath $s -Recurse -Force -ErrorAction SilentlyContinue }; "
        f"if ($r) {{ Start-Process '{launch}' }}"
    )


def start_helper(game_dir: Path) -> bool:
    """Starts the helper once per game session. Returns whether it runs."""
    if U.helper_started:
        return True
    try:
        # The game runs in a job that doesn't allow breaking away, but the helper still outlives a
        # normal game exit (verified by the respec and item cards mods, which do the same)
        subprocess.Popen(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", helper_script(game_dir)],  # noqa: S607
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as ex:
        _log(f"couldn't start the update helper: {ex}", error=True)
        return False
    U.helper_started = True
    return True


def resume(game_dir: Path) -> None:
    """At launch: updates staged by an earlier session that didn't install yet go in at this exit."""
    if any(staging(game_dir).glob("*.sdkmod")):
        _set(game_dir=game_dir)
        start_helper(game_dir)


def mark_restart(game_dir: Path) -> bool:
    """Asks the helper to start the game again once it installed the updates."""
    if not can_restart(game_dir) or not start_helper(game_dir):
        return False
    staged = staging(game_dir)
    staged.mkdir(parents=True, exist_ok=True)
    (staged / RELAUNCH_FLAG).write_bytes(b"")
    return True


# endregion
