"""
Mod repositories: other modders' mod sources, added once and then browsed, installed and updated
from the Mod Menu (Discover, Repositories), like a jailbreak package manager's sources.

A repository is one of:
- a GitHub repository, "owner/name" (or its github.com link). Its newest release's manifest.json
  is read, the same file tools/release.py writes for BL4 Fix. A repository without one still works:
  every .sdkmod attached to its newest release is offered (GitHub's release API, the tag as version).
- any https link to a JSON index in the same format (download links relative to the index).

    {"format": 1, "name": "My Mods", "description": "...", "homepage": "https://...",
     "mods": {"<module id>": {"name", "version", "asset" or "url", "sha256", "size",
                              "author", "blurb", "description", "notes", "icon", "images": [...],
                              "homepage", "games": ["BL4"]}}}

docs/mod-repos.md is the guide for modders; tools/make_repo.py writes the index.

BL4 Fix's own release (updates.REPO) is the built-in first repository: updates.py checks it, and
its mods the player doesn't have show on Discover next to everyone else's.

Installing: download (size cap, SHA-256 when given, a zip with one <id>/ folder holding
__init__.py), write sdk_mods/<id>.sdkmod (a new file: nothing has it open) and load it at once
(hotload.py, on the game thread). Updating a loaded mod: stage the new file in
sdk_mods/.bl4_updates/ (the update helper moves it in after the game closes) and reload the mod
from there. Removing: the mod turns off now, its file goes when the game closes.

A mod belongs to the repository it was installed from (settings/bl4_mod_menu_repos.json), so an
added repository can't replace another's mod or BL4 Fix's. Repositories run code on the player's PC
like any mod: the page says so before one is added.

Pictures (icons, screenshots) download on demand into a loose folder next to the page
(uiresources/bl4modmenu/img/cache), PNG or JPEG only, size-capped, and the cache is trimmed at start.

Plain Python, worker threads only, except drain() (the game thread runs the loads it queued).
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import itertools
import json
import os
import queue
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from . import hotload, updates
except ImportError:  # offline tests import the folder's modules directly
    import hotload  # type: ignore[no-redef]
    import updates  # type: ignore[no-redef]

try:
    from unrealsdk import logging
except ImportError:  # offline tests
    logging = None  # type: ignore[assignment]

SOURCES_FILE = "bl4_mod_menu_repos.json"  # in sdk_mods/settings
OFFICIAL_NAME = "BL4 Fix"
MAX_SOURCES = 32
MAX_MODS = 300  # per repository
MAX_INDEX = 2 << 20
API_URL = "https://api.github.com/repos/{}/releases/latest"
IMAGE_DIR = "OakGame/Content/uiresources/bl4modmenu/img/cache"
IMAGE_URL = "coui://uiresources/bl4modmenu/img/cache/"
MAX_IMAGE = 3 << 20
MAX_IMAGE_SIDE = 4096
MAX_IMAGES = 6  # screenshots per mod
IMAGE_CACHE_MAX = 96 << 20
IMAGE_REFRESH_S = 7 * 86400
# A worker with nothing to do for this long ends (one starts again on the next job)
IDLE_S = 20.0
GITHUB = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$", re.I)
SHORT = re.compile(r"^([\w.-]+)/([\w.-]+)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _log(text: str, *, error: bool = False) -> None:
    if logging is not None:
        (logging.error if error else logging.info)(f"[Mod Menu] {text}")


class R:
    """Repository state. Workers write it under _lock; snapshot_json() reads it."""

    revision = 0  # any change (snapshot_json rebuilds)
    # Changes /state should carry at once (states, lists, messages), not each downloaded chunk or
    # picture: web.tick rebuilds its snapshot when this moves
    events = 0
    game_dir: Path | None = None
    loaded = False  # the sources file was read
    # Saved: what the player added, in order. {"key", "url", "added"}
    sources: list[dict[str, Any]] = []
    # Saved: the player removed BL4 Fix's built-in repository (its mods leave Discover; installed
    # ones still update through updates.py)
    official_hidden = False
    # key -> {"status": checking/ok/error, "message", "name", "description", "homepage", "release", "mode"}
    info: dict[str, dict[str, Any]] = {}
    # key -> {mod id: entry}, entries cleaned by _entry()
    catalog: dict[str, dict[str, dict[str, Any]]] = {}
    # Saved: mod id -> {"repo", "version", "sha256"} for mods installed from a repository
    installed: dict[str, dict[str, Any]] = {}
    # Mod id -> {"state": downloading/loaded/staged/reloaded/failed/removed, "message", "progress"}
    jobs: dict[str, dict[str, Any]] = {}
    # Computed by _recompute (workers): what Discover and Updates list
    discover: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    # Remote picture url -> coui url ("" = couldn't be fetched)
    images: dict[str, str] = {}
    # A repository checked and waiting for the player's OK: {"key", "url", "name", "count", "description"}
    pending: dict[str, Any] | None = None
    message: dict[str, Any] = {"id": 0, "text": "", "error": False}
    refreshing = False
    # (what it was built from, its revision, json) of the last snapshot_json
    cached: tuple[Any, int, bytes] = (None, 0, b"")


_lock = threading.RLock()
# Snapshot revisions start from the clock, so a page holding one from an older server (hot reload, a
# new game) can't match a new one by chance (extensions.py does the same)
_revs = itertools.count(int(time.time() * 1000))
# Work for the game thread: (kind, mod id, path or None, done(message, error))
_todo: deque[tuple[str, str, Path | None, Callable[[str, bool], None]]] = deque()


def _set(**changes: Any) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(R, key, value)
        R.revision += 1
        R.events += 1


def _say(text: str, *, error: bool = False) -> None:
    with _lock:
        R.message = {"id": R.message["id"] + 1, "text": text, "error": error}
        R.revision += 1
        R.events += 1
    _log(text, error=error)


def _job(mod_id: str, state: str, message: str = "", progress: tuple[int, int] | None = None) -> None:
    with _lock:
        prev = R.jobs.get(mod_id, {}).get("state")
        R.jobs[mod_id] = {"state": state, "message": message, "progress": list(progress or (0, 0))}
        R.revision += 1
        if prev != state:
            R.events += 1  # not for each downloaded chunk


# region Workers


class _Worker:
    """One long-lived thread per kind of job, ended after IDLE_S with nothing to do."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.jobs: queue.Queue[Callable[[], None]] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.guard = threading.Lock()

    def put(self, job: Callable[[], None]) -> None:
        self.jobs.put(job)
        with self.guard:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                job = self.jobs.get(timeout=IDLE_S)
            except queue.Empty:
                with self.guard:
                    if self.jobs.empty():
                        self.thread = None
                        return
                continue
            try:
                job()
            except Exception as ex:  # noqa: BLE001 - a job reports its own errors; never kill the worker
                _log(f"{self.name}: {ex}", error=True)


_net = _Worker("bl4_mod_menu.repos")
_pics = _Worker("bl4_mod_menu.pictures")


# endregion
# region Saved state


def settings_file(game_dir: Path) -> Path:
    return updates.sdk_mods(game_dir) / "settings" / SOURCES_FILE


def _load(game_dir: Path) -> None:
    path = settings_file(game_dir)
    data: dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as ex:
        # Keep the broken file for the player, start empty (the next save writes a good one)
        try:
            path.replace(path.with_name(f"{path.stem}.bad-{int(time.time())}.json"))
        except OSError:
            pass
        _log(f"{path.name} couldn't be read ({ex}); set aside", error=True)
        data = {}
    sources = []
    for item in data.get("sources") or []:
        if isinstance(item, dict) and (norm := normalize(str(item.get("url") or item.get("key") or ""))):
            if all(s["key"] != norm for s in sources) and norm != official_key():
                sources.append({"key": norm, "url": str(item.get("url") or norm), "added": item.get("added", 0)})
    installed = {
        str(k): {"repo": str(v.get("repo", "")), "version": str(v.get("version", "")), "sha256": str(v.get("sha256", ""))}
        for k, v in (data.get("installed") or {}).items()
        if isinstance(v, dict) and hotload.MOD_ID.match(str(k))
    }
    _set(sources=sources[:MAX_SOURCES], installed=installed, official_hidden=data.get("official_hidden") is True, loaded=True)


def _save() -> None:
    game_dir = R.game_dir
    if game_dir is None:
        return
    with _lock:
        data = {"format": 1, "sources": [dict(s) for s in R.sources], "installed": {k: dict(v) for k, v in R.installed.items()}}
        if R.official_hidden:
            data["official_hidden"] = True
    updates._write(settings_file(game_dir), json.dumps(data, indent=4).encode("utf-8"))


# endregion
# region Repositories


def official_key() -> str:
    return updates.U.repo or updates.REPO


def normalize(text: str) -> str | None:
    """The key a repository is stored under: "owner/name" for GitHub, else the https url."""
    text = text.strip().strip("\"'<>").strip()
    if not text or len(text) > 500 or any(c.isspace() for c in text):
        return None
    if m := GITHUB.match(text):
        return f"{m.group(1)}/{m.group(2)}"
    if (m := SHORT.match(text)) and not text.lower().startswith(("http", "www.")):
        return f"{m.group(1)}/{m.group(2)}"
    if text.lower().startswith("https://") or (updates.ALLOW_HTTP and text.lower().startswith("http://")):
        return text
    return None


def is_github(key: str) -> bool:
    return SHORT.match(key) is not None and not key.lower().startswith("http")


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit] if isinstance(value, (str, int, float)) else ""


def _https(value: Any, base: str = "") -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        return ""
    url = urllib.parse.urljoin(base, value) if base else value
    ok = url.lower().startswith("https://") or (updates.ALLOW_HTTP and url.lower().startswith("http://"))
    return url if ok else ""


def _entry(mod_id: str, raw: Any, download: str, *, base: str = "", version_from_tag: str = "") -> dict[str, Any] | None:
    """A repository's mod, cleaned: only known fields, capped lengths, https links (pictures may be
    relative to base, the index's url)."""
    if not isinstance(raw, dict) or not hotload.MOD_ID.match(mod_id) or mod_id in hotload.FRAMEWORK:
        return None
    games = raw.get("games") or raw.get("supported_games")
    if isinstance(games, list) and games and not any(str(g).upper() in ("BL4", "OAK2") for g in games):
        return None
    sha = str(raw.get("sha256") or "").lower()
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    pictures = raw.get("images") if isinstance(raw.get("images"), list) else []
    images = [u for u in (_https(i, base) for i in pictures[:MAX_IMAGES]) if u]
    return {
        "id": mod_id,
        "name": _text(raw.get("name"), 80) or mod_id,
        "version": _text(raw.get("version"), 32) or version_from_tag,
        "author": _text(raw.get("author"), 80),
        "blurb": _text(raw.get("blurb"), 300),
        "description": _text(raw.get("description"), 4000),
        "notes": _text(raw.get("notes"), 2000),
        "homepage": _https(raw.get("homepage")),
        "icon": _https(raw.get("icon"), base),
        "images": images,
        "sha256": sha if SHA256.match(sha) else "",
        "size": size if 0 < size <= updates.MAX_ASSET else 0,
        "download": download,
        "tag_version": bool(version_from_tag),
    }


def _from_manifest(manifest: Any, download_base: Callable[[str], str], index_url: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(manifest, dict) or manifest.get("format") != updates.MANIFEST_FORMAT or not isinstance(manifest.get("mods"), dict):
        raise RuntimeError("this isn't a mod repository (no manifest in a format this Mod Menu knows)")
    mods: dict[str, dict[str, Any]] = {}
    for mod_id, raw in list(manifest["mods"].items())[:MAX_MODS]:
        if not isinstance(raw, dict):
            continue
        url = _https(raw.get("url")) or (download_base(str(raw["asset"])) if isinstance(raw.get("asset"), str) and "/" not in raw["asset"] and "\\" not in raw["asset"] else "")
        if url and (entry := _entry(str(mod_id), raw, url, base=index_url)) is not None:
            mods[entry["id"]] = entry
    info = {
        "name": _text(manifest.get("name"), 80),
        "description": _text(manifest.get("description"), 1000),
        "homepage": _https(manifest.get("homepage")),
        "release": _text(manifest.get("release"), 60),
        "mode": "manifest",
    }
    return info, mods


def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": updates.USER_AGENT, "Cache-Control": "no-cache", **(headers or {})})
    with urllib.request.urlopen(request, timeout=updates.TIMEOUT_S) as response:  # noqa: S310 - https checked
        if not (response.geturl().startswith("https://") or updates.ALLOW_HTTP):
            raise RuntimeError(f"refused a non-https link: {response.geturl()}")
        data = response.read(MAX_INDEX + 1)
    if len(data) > MAX_INDEX:
        raise RuntimeError("the repository's index is too large")
    return json.loads(data.decode("utf-8"))


def _from_github_api(key: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """A GitHub repository without a manifest: every .sdkmod on its newest release."""
    release = _get_json(api_url(key), {"Accept": "application/vnd.github+json"})
    if not isinstance(release, dict):
        raise RuntimeError("GitHub answered something unexpected")
    tag = _text(release.get("tag_name"), 60)
    mods: dict[str, dict[str, Any]] = {}
    for asset in (release.get("assets") or [])[:MAX_MODS]:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".sdkmod"):
            continue
        digest = str(asset.get("digest") or "")
        raw = {
            "name": name[: -len(".sdkmod")],
            "size": asset.get("size"),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
            "notes": release.get("body") or "",
        }
        if (entry := _entry(name[: -len(".sdkmod")], raw, _https(asset.get("browser_download_url")), version_from_tag=tag)) and entry["download"]:
            mods[entry["id"]] = entry
    if not mods:
        raise RuntimeError(f"{key}'s newest release has no .sdkmod files")
    info = {"name": key, "description": _text(release.get("name"), 200), "homepage": f"https://github.com/{key}",
            "release": tag, "mode": "github"}  # fmt: skip
    return info, mods


def api_url(key: str) -> str:
    return API_URL.format(key)


def fetch(key: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(repository info, its mods). Blocking: workers only."""
    if is_github(key):
        url = updates.manifest_url(key)
        try:
            manifest = _get_json(url)
        except urllib.error.HTTPError as ex:
            if ex.code != 404:  # noqa: PLR2004
                raise
            return _from_github_api(key)
        tag = str(manifest.get("release") or "") if isinstance(manifest, dict) else ""
        info, mods = _from_manifest(manifest, lambda asset: updates.asset_url(key, tag, asset), url)
        info["homepage"] = info["homepage"] or f"https://github.com/{key}"
        info["name"] = info["name"] or key
        return info, mods
    manifest = _get_json(key)
    return _from_manifest(manifest, lambda asset: urllib.parse.urljoin(key, asset), key)


def _official() -> tuple[dict[str, Any], dict[str, dict[str, Any]]] | None:
    """BL4 Fix's release as a repository, from what updates.py already fetched (no second request)."""
    manifest = updates.U.manifest
    if not isinstance(manifest, dict):
        return None
    key = official_key()
    tag = str(manifest.get("release") or "")
    try:
        info, mods = _from_manifest(manifest, lambda asset: updates.asset_url(key, tag, asset), updates.manifest_url(key))
    except RuntimeError:
        return None
    info["name"] = OFFICIAL_NAME
    info["homepage"] = updates.releases_page(key)
    return info, mods


def refresh_async(keys: list[str] | None = None, *, announce: bool = False) -> bool:
    """
    Checks the added repositories (or just these) on the worker. False if a check is running.
    announce: the player asked (Check all): say what the check found when it's done, including
    BL4 Fix's own release (updates.py checks that one; start its check first).
    """
    with _lock:
        if R.refreshing:
            return False
        R.refreshing = True
        R.revision += 1
        R.events += 1
    _net.put(lambda: _refresh(keys, announce=announce))
    return True


# Check all waits this long at most for updates.py's check of BL4 Fix's release, to report it too
ANNOUNCE_WAIT_S = 20.0


def _announce() -> None:
    end = time.monotonic() + ANNOUNCE_WAIT_S
    while updates.U.busy and time.monotonic() < end:
        time.sleep(0.25)
    _recompute()  # with BL4 Fix's release as just read
    with _lock:
        checked = len(R.sources) + (0 if R.official_hidden else 1)
        failed = [s["key"] for s in R.sources if R.info.get(s["key"], {}).get("status") == "error"]
        new, waiting = len(R.discover), len(R.updates)
    parts = [f"{new} new mod{'s' if new != 1 else ''} on Discover"]
    if waiting:
        parts.append(f"{waiting} update{'s' if waiting != 1 else ''} on Updates")
    text = f"Refreshed {checked} repositor{'ies' if checked != 1 else 'y'}: {', '.join(parts)}."
    if failed:
        text += f" Couldn't read {', '.join(failed)} (see its row)."
    _say(text, error=bool(failed))


def _refresh(keys: list[str] | None, *, announce: bool = False) -> None:
    try:
        targets = [s["key"] for s in R.sources if keys is None or s["key"] in keys]
        for key in targets:
            with _lock:
                R.info[key] = {**R.info.get(key, {}), "status": "checking", "message": "Checking..."}
                R.revision += 1
                R.events += 1
            try:
                info, mods = fetch(key)
                with _lock:
                    R.info[key] = {**info, "status": "ok", "message": f"{len(mods)} mod{'s' if len(mods) != 1 else ''}"}
                    R.catalog[key] = mods
                    R.revision += 1
                    R.events += 1
            except Exception as ex:  # noqa: BLE001 - offline, gone, not a repository: say so
                with _lock:
                    R.info[key] = {**R.info.get(key, {}), "status": "error", "message": _reason(ex)}
                    R.revision += 1
                    R.events += 1
                _log(f"repository {key}: {_reason(ex)}")
        _recompute()
        if announce:
            _announce()
    finally:
        _set(refreshing=False)


def _reason(ex: BaseException) -> str:
    if isinstance(ex, urllib.error.HTTPError):
        return {404: "not found (no release or index there)", 403: "GitHub refused (rate limit?); try again later"}.get(ex.code, f"HTTP {ex.code}")
    if isinstance(ex, urllib.error.URLError):
        return f"couldn't connect ({ex.reason})"
    if isinstance(ex, ValueError):
        return "the index isn't valid JSON"
    return str(ex)


def add_async(text: str, replace: str | None = None) -> None:
    """
    Checks a repository the player gave; on success it waits in R.pending for their OK.
    replace: an added repository's key this one takes the place of (the player edited its link).
    """
    key = normalize(text)
    if key is None:
        _say("That isn't a repository link: use owner/name, a github.com link, or an https link to an index.", error=True)
        return
    if key == official_key():
        if replace is not None:
            _say(f"That's {OFFICIAL_NAME}'s repository, which is built in.", error=True)
        elif R.official_hidden:
            show_official()
        else:
            _say(f"{OFFICIAL_NAME} is built in already.", error=True)
        return
    if replace is not None and key == replace:
        set_url(key, text.strip())
        return
    if any(s["key"] == key for s in R.sources):
        _say(f"{key} is added already.", error=True)
        return
    if replace is not None and source_url(replace) is None:
        replace = None  # removed meanwhile: add it as a new one
    if replace is None and len(R.sources) >= MAX_SOURCES:
        _say(f"That's {MAX_SOURCES} repositories already: remove one first.", error=True)
        return
    _set(pending={"key": key, "url": text.strip(), "status": "checking", "name": key, "count": 0, "description": "",
                  "replace": replace})  # fmt: skip

    def run() -> None:
        try:
            info, mods = fetch(key)
        except Exception as ex:  # noqa: BLE001
            _set(pending=None)
            _say(f"Couldn't add {key}: {_reason(ex)}", error=True)
            return
        with _lock:
            if R.pending is None or R.pending["key"] != key:
                return  # cancelled
            R.pending = {"key": key, "url": text.strip(), "status": "ready", "name": info.get("name") or key,
                         "count": len(mods), "description": info.get("description", ""), "mods": [m["name"] for m in mods.values()][:12],
                         "replace": replace, "replace_name": _source_name(replace) if replace else ""}  # fmt: skip
            R.info[key] = {**info, "status": "ok", "message": f"{len(mods)} mod{'s' if len(mods) != 1 else ''}"}
            R.catalog[key] = mods
            R.revision += 1
            R.events += 1

    _net.put(run)


def add_from_clipboard_async() -> None:
    def run() -> None:
        text = clipboard_text()
        if not text.strip():
            _say("The clipboard is empty: copy a repository link first (owner/name or https://...).", error=True)
            return
        add_async(text.strip().splitlines()[0])

    _net.put(run)


def confirm_add() -> bool:
    """Adds the checked repository (or puts it in place of the one edited). True if it did."""
    with _lock:
        pending = R.pending
        if pending is None or pending.get("status") != "ready":
            return False
        new = {"key": pending["key"], "url": pending["url"], "added": int(time.time())}
        old = pending.get("replace")
        at = next((i for i, s in enumerate(R.sources) if s["key"] == old), None) if old else None
        if at is None:
            R.sources.append(new)
        else:
            R.sources[at] = {**new, "added": R.sources[at].get("added", new["added"])}
            R.catalog.pop(old, None)
            R.info.pop(old, None)
            # The same modder's repository at a new link: what was installed from it updates from there
            for record in R.installed.values():
                if record["repo"] == old:
                    record["repo"] = new["key"]
        R.pending = None
        R.revision += 1
        R.events += 1
    _net.put(lambda: (_save(), _recompute()))
    _say(f"Saved {pending['name']} in place of {old}." if at is not None else f"Added {pending['name']}: its mods are on Discover.")
    return True


def cancel_add() -> None:
    _set(pending=None)


def source_url(key: str) -> str | None:
    """The link an added repository was given as (None: not added)."""
    with _lock:
        return next((s["url"] for s in R.sources if s["key"] == key), None)


def set_url(key: str, url: str) -> None:
    """The player edited a repository's link into another spelling of the same one."""
    with _lock:
        for source in R.sources:
            if source["key"] == key:
                source["url"] = url
        R.revision += 1
        R.events += 1
    _net.put(_save)
    _say(f"Saved: that's still {key}.")


def show_official() -> None:
    _set(official_hidden=False)
    _net.put(lambda: (_save(), _recompute()))
    _say(f"{OFFICIAL_NAME}'s mods are back on Discover.")


def remove_source(key: str) -> None:
    if key == official_key():
        # Built in: hidden, not forgotten (updates.py still keeps BL4 Fix's installed mods current)
        if not R.official_hidden:
            _set(official_hidden=True)
            _net.put(lambda: (_save(), _recompute()))
            _say(f"Removed {OFFICIAL_NAME} from Discover. Its installed mods still update on the Updates page.")
        return
    with _lock:
        before = len(R.sources)
        R.sources = [s for s in R.sources if s["key"] != key]
        if len(R.sources) == before:
            return
        R.catalog.pop(key, None)
        R.info.pop(key, None)
        R.revision += 1
        R.events += 1
    _net.put(lambda: (_save(), _recompute()))
    _say(f"Removed {key}. Mods you installed from it stay, without updates.")


# endregion
# region What's installed, what's new


def _present(game_dir: Path, mod_id: str) -> bool:
    return updates._present(game_dir, mod_id)


def _installed_version(game_dir: Path, mod_id: str) -> str | None:
    path = updates.sdk_mods(game_dir) / f"{mod_id}.sdkmod"
    if path.is_file():
        return updates._zip_version(path, mod_id)
    folder = updates.sdk_mods(game_dir) / mod_id
    if folder.is_dir():
        return hotload.read_info(folder).get("version") or None
    return None


def owner(mod_id: str) -> str | None:
    """The repository a mod's updates come from: where it was installed from, else BL4 Fix if its
    release has it (updates.py handles those), else the first added repository listing it."""
    with _lock:
        record = R.installed.get(mod_id)
        if record and any(s["key"] == record["repo"] for s in R.sources):
            return record["repo"]
        manifest = updates.U.manifest
        if isinstance(manifest, dict) and mod_id in (manifest.get("mods") or {}):
            return official_key()
        if record:
            return None  # its repository was removed
        for source in R.sources:
            if mod_id in R.catalog.get(source["key"], {}):
                return source["key"]
    return None


def _source_name(key: str) -> str:
    if key == official_key():
        return OFFICIAL_NAME
    return str(R.info.get(key, {}).get("name") or key)


def _recompute() -> None:
    """Works out Discover (mods nobody installed) and the repositories' updates. File reads: worker."""
    game_dir = R.game_dir
    if game_dir is None:
        return
    official = _official()
    with _lock:
        sources = [(official_key(), official[1])] if official and not R.official_hidden else []
        if official:
            R.info[official_key()] = {**official[0], "status": "ok", "message": f"{len(official[1])} mods"}
            R.catalog[official_key()] = official[1]
        sources += [(s["key"], R.catalog.get(s["key"], {})) for s in R.sources]
        installed = dict(R.installed)
    discover: list[dict[str, Any]] = []
    listed: set[str] = set()
    pending_updates: list[dict[str, Any]] = []
    for key, mods in sources:
        for mod_id, entry in mods.items():
            if mod_id in listed:
                continue
            listed.add(mod_id)
            if not _present(game_dir, mod_id):
                discover.append({**entry, "repo": key, "repo_name": _source_name(key)})
                continue
            if key == official_key() or owner(mod_id) != key:
                continue  # BL4 Fix's own mods update through updates.py; others' only from their owner
            record = installed.get(mod_id)
            have = record["version"] if record and record["repo"] == key and record["version"] else _installed_version(game_dir, mod_id)
            if entry["tag_version"] and record and record["version"] == entry["version"]:
                continue
            if updates.newer(entry["version"], have) and (have is None or updates.version_key(entry["version"]) != updates.version_key(have)):
                pending_updates.append({**entry, "repo": key, "repo_name": _source_name(key), "installed": have or "?"})
    _set(discover=discover, updates=pending_updates)


def mods_installed_from_repos() -> list[dict[str, Any]]:
    with _lock:
        return [{"id": k, **v, "repo_name": _source_name(v["repo"])} for k, v in sorted(R.installed.items())]


# endregion
# region Install, update, remove


def _find(mod_id: str) -> tuple[str, dict[str, Any]] | None:
    """The repository entry to install or update this mod from."""
    with _lock:
        for item in R.discover + R.updates:
            if item["id"] == mod_id:
                return item["repo"], item
    return None


def _verify(data: bytes, mod_id: str, version: str | None) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if f"{mod_id}/__init__.py" not in names or any(not n.startswith(f"{mod_id}/") for n in names):
                raise RuntimeError(f"the download isn't the {mod_id} mod (it must hold one {mod_id}/ folder)")
            if zf.testzip() is not None:
                raise RuntimeError("the download is damaged")
            if version and f"{mod_id}/pyproject.toml" in names:
                toml = tomllib.loads(zf.read(f"{mod_id}/pyproject.toml").decode("utf-8"))
                found = toml.get("tool", {}).get("sdkmod", {}).get("version") or toml.get("project", {}).get("version")
                if found and str(found) != version:
                    raise RuntimeError(f"the download is version {found}, the repository says {version}")
    except (zipfile.BadZipFile, ValueError) as ex:
        raise RuntimeError(f"the download isn't a valid .sdkmod ({ex})") from ex


def _download(mod_id: str, entry: dict[str, Any]) -> bytes:
    size = int(entry.get("size") or 0)
    limit = min(updates.MAX_ASSET, size + 1024) if size else updates.MAX_ASSET

    def progress(n: int) -> None:
        _job(mod_id, "downloading", "Downloading...", (n, size))

    data = updates._get(entry["download"], limit, progress)
    if size and len(data) != size:
        raise RuntimeError(f"got {len(data)} bytes, the repository says {size}")
    if entry.get("sha256") and hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise RuntimeError("checksum mismatch: the file isn't the one the repository lists")
    _verify(data, mod_id, None if entry.get("tag_version") else entry.get("version"))
    return data


def _record(mod_id: str, key: str, entry: dict[str, Any], data: bytes) -> None:
    if key == official_key():
        return  # BL4 Fix's own mods: updates.py knows them
    with _lock:
        R.installed[mod_id] = {"repo": key, "version": entry.get("version", ""), "sha256": hashlib.sha256(data).hexdigest()}
        R.revision += 1
        R.events += 1
    _save()


def install_async(mod_id: str) -> bool:
    """Downloads a mod from Discover, puts it in sdk_mods and loads it now."""
    game_dir = R.game_dir
    found = _find(mod_id)
    if game_dir is None or found is None:
        _say("That mod isn't in your repositories any more.", error=True)
        return False
    key, entry = found
    if R.jobs.get(mod_id, {}).get("state") == "downloading":
        return False
    _job(mod_id, "downloading", "Downloading...", (0, int(entry.get("size") or 0)))

    def run() -> None:
        try:
            data = _download(mod_id, entry)
            target = updates.sdk_mods(game_dir) / f"{mod_id}.sdkmod"
            # A file the game loaded (now or at start, even if that failed) must not be written over
            removal = updates.staging(game_dir) / f"{mod_id}.remove"
            if removal.exists():
                removal.unlink()  # removed earlier this session: install takes it back
            if target.exists() and mod_id in sys.modules:
                path: Path | None = updates.staging(game_dir) / f"{mod_id}.sdkmod"
                updates._write(path, data)
                updates.start_helper(game_dir)
                kind = "reload"
            else:
                updates._write(target, data)
                path, kind = None, "load"
            _enable_setting(game_dir, mod_id)
            _record(mod_id, key, entry, data)
            if key == official_key():
                # Off the Updates page's "Not installed" list too
                updates._set(new=[n for n in updates.U.new if n["id"] != mod_id])
        except Exception as ex:  # noqa: BLE001
            _job(mod_id, "failed", f"Couldn't install: {_reason(ex)}")
            _say(f"Couldn't install {entry['name']}: {_reason(ex)}", error=True)
            return
        _job(mod_id, "loading", "Loading...")
        _queue(kind, mod_id, path, lambda text, error: _after_load(mod_id, entry, text, error, installed=True))

    _net.put(run)
    return True


def _enable_setting(game_dir: Path, mod_id: str) -> None:
    """A mod installed from here starts switched on, like one the setup app installs."""
    updates._enable_setting(game_dir, mod_id)


def update_async(mod_ids: list[str]) -> int:
    """Downloads updates from the repositories, stages them, and reloads each mod that can."""
    game_dir = R.game_dir
    if game_dir is None:
        return 0
    started = 0
    for mod_id in mod_ids:
        with _lock:
            entry = next((u for u in R.updates if u["id"] == mod_id), None)
        if entry is None or R.jobs.get(mod_id, {}).get("state") == "downloading":
            continue
        if hotload.H.loaded_from.get(mod_id) == str(updates.staging(game_dir) / f"{mod_id}.sdkmod"):
            _job(mod_id, "staged", "Already updated once this session: restart the game first.")
            continue
        _job(mod_id, "downloading", "Downloading...", (0, int(entry.get("size") or 0)))
        started += 1

        def run(mod_id: str = mod_id, entry: dict[str, Any] = entry) -> None:
            try:
                data = _download(mod_id, entry)
                path = updates.staging(game_dir) / f"{mod_id}.sdkmod"
                updates._write(path, data)
                updates.start_helper(game_dir)
                _record(mod_id, entry["repo"], entry, data)
            except Exception as ex:  # noqa: BLE001
                _job(mod_id, "failed", f"Couldn't update: {_reason(ex)}")
                _say(f"Couldn't update {entry['name']}: {_reason(ex)}", error=True)
                return
            _job(mod_id, "loading", "Reloading...")
            _queue("reload", mod_id, path, lambda text, error: _after_load(mod_id, entry, text, error, installed=False))

        _net.put(run)
    return started


def _after_load(mod_id: str, entry: dict[str, Any], text: str, error: bool, *, installed: bool) -> None:
    if error and not installed:
        # Staged all the same: the helper moves it in when the game closes
        _job(mod_id, "staged", f"{text} Installs when the game closes.")
        _say(f"{entry['name']} updates when the game starts again: {text}")
    elif error:
        _job(mod_id, "failed", text)
        _say(text, error=True)
    else:
        _job(mod_id, "reloaded" if not installed else "loaded", text)
        _say(text)
    _net.put(_recompute)


def remove_mod(mod_id: str) -> str:
    """Game thread: turns the mod off now and deletes its file when the game closes."""
    game_dir = R.game_dir
    if game_dir is None:
        raise RuntimeError("not ready")
    if mod_id in hotload.REQUIRED or mod_id in hotload.FRAMEWORK:
        raise RuntimeError(f"{mod_id} is needed by the mod menu: remove it with BL4 Fix Setup.")
    if not (updates.sdk_mods(game_dir) / f"{mod_id}.sdkmod").is_file():
        raise RuntimeError(f"{mod_id} isn't an installed .sdkmod (a folder copy: delete it yourself).")
    names = hotload.unload(mod_id) if mod_id in sys.modules else []
    staging = updates.staging(game_dir)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"{mod_id}.sdkmod").unlink(missing_ok=True)
    (staging / f"{mod_id}.remove").write_bytes(b"")
    updates.start_helper(game_dir)
    with _lock:
        R.installed.pop(mod_id, None)
        R.revision += 1
        R.events += 1
    _job(mod_id, "removed", "Removed: its file goes when the game closes.")
    _net.put(lambda: (_save(), _recompute()))
    return f"Removed {', '.join(names) or mod_id}: its file is deleted when the game closes."


def _queue(kind: str, mod_id: str, path: Path | None, done: Callable[[str, bool], None]) -> None:
    _todo.append((kind, mod_id, path, done))


def pending_loads() -> bool:
    return bool(_todo)


def drain() -> None:
    """Game thread (web.tick): runs the loads the workers queued."""
    game_dir = R.game_dir
    while _todo and game_dir is not None:
        kind, mod_id, path, done = _todo.popleft()
        try:
            if kind == "load":
                text = hotload.load(game_dir, mod_id)
            else:
                text = hotload.reload(game_dir, mod_id, path)
            done(text, False)
        except Exception as ex:  # noqa: BLE001
            done(str(ex), True)


# endregion
# region Pictures


def _image_name(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]  # noqa: S324 - a file name, not security


def _image_size(data: bytes) -> tuple[str, int, int] | None:
    """(extension, width, height) of a PNG or JPEG, or None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 24 and data[12:16] == b"IHDR":
        return "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            length = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2):
                return "jpg", int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
            i += 2 + length
    return None


def want_images(urls: list[str]) -> None:
    """The page shows these pictures: fetch the ones not cached yet (one at a time, on a worker)."""
    game_dir = R.game_dir
    if game_dir is None:
        return
    for url in urls[:64]:
        if not isinstance(url, str) or not _https(url):
            continue
        with _lock:
            if url in R.images:
                continue
            R.images[url] = None  # type: ignore[assignment]  # asked for
        _pics.put(lambda url=url: _fetch_image(game_dir, url))


def _fetch_image(game_dir: Path, url: str) -> None:
    folder = game_dir / IMAGE_DIR
    name = _image_name(url)
    for ext in ("png", "jpg"):
        path = folder / f"{name}.{ext}"
        try:
            info = path.stat()
        except OSError:
            continue
        if time.time() - info.st_mtime < IMAGE_REFRESH_S:
            _set_image(url, f"{IMAGE_URL}{path.name}?v={int(info.st_mtime)}")
            return
    try:
        data = updates._get(url, MAX_IMAGE)
        kind = _image_size(data)
        if kind is None:
            raise RuntimeError("not a PNG or JPEG")
        if not (0 < kind[1] <= MAX_IMAGE_SIDE and 0 < kind[2] <= MAX_IMAGE_SIDE):
            raise RuntimeError(f"{kind[1]}x{kind[2]} is too large")
        path = folder / f"{name}.{kind[0]}"
        updates._write(path, data)
        _set_image(url, f"{IMAGE_URL}{path.name}?v={int(path.stat().st_mtime)}")
    except Exception as ex:  # noqa: BLE001
        _set_image(url, "")
        _log(f"picture {url}: {_reason(ex)}")
    time.sleep(0.05)  # a gentle pace: pictures are never urgent


def _set_image(url: str, local: str) -> None:
    with _lock:
        R.images[url] = local
        R.revision += 1  # data only: the page's own /repos poll picks it up


def trim_images(game_dir: Path) -> None:
    """Keeps the picture cache under IMAGE_CACHE_MAX, oldest out first. Worker, at start."""
    folder = game_dir / IMAGE_DIR
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in folder.iterdir() if p.is_file()]
    except OSError:
        return
    total = sum(f[1] for f in files)
    for _, size, path in sorted(files):
        if total <= IMAGE_CACHE_MAX:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


# endregion
# region Clipboard


def clipboard_text() -> str:
    """The clipboard's text (worker thread; own DLL objects, shared windll prototypes stay as they are)."""
    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32.OpenClipboard.argtypes = (ctypes.c_void_p,)
    user32.OpenClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = (ctypes.c_uint,)
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_int
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    for _ in range(10):  # another program may hold it for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)[:2000]
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# endregion
# region The page


def open_link(url: str) -> None:
    """Opens a mod's or repository's web page in the browser (https only)."""
    if not _https(url) or not url.lower().startswith("https://"):
        return
    threading.Thread(target=lambda: os.startfile(url), name="bl4_mod_menu.link", daemon=True).start()  # noqa: S606


def start(game_dir: Path, *, check: bool) -> None:
    """At enable: read the saved repositories, trim the picture cache, check them if asked."""
    R.game_dir = game_dir

    def run() -> None:
        _load(game_dir)
        trim_images(game_dir)
        hotload.take_baseline(game_dir)
        hotload.publish_icons(game_dir)
        _recompute()

    _net.put(run)
    if check:
        _net.put(lambda: refresh_async() if R.sources else None)


def official_checked() -> None:
    """updates.py fetched BL4 Fix's release again: list its mods anew."""
    _net.put(_recompute)


def summary() -> dict[str, Any]:
    """The small part /state carries (the badge, the left column's counts)."""
    with _lock:
        waiting = sum(1 for u in R.updates if R.jobs.get(u["id"], {}).get("state") not in ("staged", "reloaded", "downloading", "loading"))
        found = len(hotload.H.found)
        return {"rev": R.events + hotload.H.revision, "updates": waiting, "discover": len(R.discover),
                "found": found, "sources": len(R.sources) + 1, "message": R.message}  # fmt: skip


def snapshot_json() -> tuple[int, bytes]:
    """(revision, json) of everything the Discover, Repositories and Updates pages show. Built on the
    server thread, only when the revision moved."""
    with _lock:
        built_from = (R.revision, hotload.H.revision)
        if R.cached[0] == built_from:
            return R.cached[1], R.cached[2]
        rev = next(_revs)
        official = official_key()
        sources = [{"key": official, "name": OFFICIAL_NAME, "official": True, "hidden": R.official_hidden,
                    **{k: R.info.get(official, {}).get(k, "") for k in ("status", "message", "homepage", "release")}}]  # fmt: skip
        for s in R.sources:
            info = R.info.get(s["key"], {})
            sources.append({"key": s["key"], "url": s["url"], "official": False, "name": info.get("name") or s["key"],
                            **{k: info.get(k, "") for k in ("status", "message", "homepage", "release", "description")}})  # fmt: skip
        state = {
            "rev": rev,
            "sources": sources,
            "discover": R.discover,
            "updates": R.updates,
            "installed": [{"id": k, **v, "repo_name": _source_name(v["repo"])} for k, v in sorted(R.installed.items())],
            "jobs": R.jobs,
            "images": {k: v for k, v in R.images.items() if v is not None},
            "pending": R.pending,
            "refreshing": R.refreshing,
            "message": R.message,
            **hotload.snapshot(),
        }
        R.cached = (built_from, rev, json.dumps(state, separators=(",", ":"), default=str).encode("utf-8"))
        return R.cached[1], R.cached[2]


# endregion
