"""
Puts the dex's pictures back when the game folder lacks some (2026-10-29).

The boss and item pictures (uiresources/bl4weapondex/{bosses,items} + small/, ~46 MB) aren't in the
.sdkmod: the setup app unpacks them (installer/core._install_assets). Every other way in left the
dex without pictures: the in-game updater, Discover, a Nexus zip, or an uninstall that removed the
folder before a reinstall of the .sdkmods (what happened on the dev machine).

pictures.json (written by tools/sync_pictures.py from assets/, checked by installer/build.py) lists
every picture the mod expects and its version. Once per session, on a worker thread, `restore`
compares it with the game folder; if any picture is missing (or older, for the full ones), it
downloads the pictures zip of the latest GitHub release (manifest.json: mods.bl4_weapondex.
pictures, made by tools/release.py; size + SHA-256 checked) and merges it in the setup app's way:
a picture the player has in a newer version is kept. The page re-reads the indexes every 3 s, so
they show without a restart. settings/weapondex_pictures.json records the zip applied last and
what it left missing: if that's all that's missing now (the release lacks those pictures), it
isn't downloaded again, so that costs one download, not one per launch. A later wipe of pictures
it did bring downloads again.

Plain Python, no game access. Worker thread only.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.request
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

# The release the in-game updater follows (bl4_mod_menu/updates.py REPO): Kairosdex runs without
# the Mod Menu, so it has its own copy
REPO = "flameaway/bl4-fix"
MOD_ID = "bl4_weapondex"
USER_AGENT = "BL4-Kairosdex"
TIMEOUT_S = 30
MAX_MANIFEST = 2 * 1024 * 1024
MAX_ZIP = 200 * 1024 * 1024
UI_REL = Path("OakGame/Content/uiresources")
STATE_FILE = "weapondex_pictures.json"


def expected() -> dict[str, dict[str, int]]:
    """{folder under uiresources: {picture: version}}, from pictures.json."""
    return json.loads((resources.files(__package__) / "pictures.json").read_text(encoding="utf-8"))


def _index(folder: Path) -> dict[str, int]:
    try:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return index if isinstance(index, dict) else {}


def missing(game_dir: Path, want: dict[str, dict[str, int]]) -> set[str]:
    """
    The expected pictures ("folder/picture") the game folder lacks: not listed, no file, or (full
    pictures only) an older version. A small copy's version is its file's date
    (picture.write_small_index), so only its presence counts.
    """
    lacking = set()
    for folder, pictures in want.items():
        target = game_dir / UI_REL / folder
        have = _index(target)
        try:
            files = {p.name for p in target.iterdir()}
        except OSError:
            files = set()
        small = folder.endswith("/small")
        for key, version in pictures.items():
            if key not in have or f"{key}.png" not in files or (not small and have[key] < version):
                lacking.add(f"{folder}/{key}")
    return lacking


def _get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - https only
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is larger than {limit} bytes")
    return data


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _read_state(settings_dir: Path) -> dict[str, Any]:
    try:
        state = json.loads((settings_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def merge(game_dir: Path, archive: zipfile.ZipFile) -> int:
    """
    Unpacks the pictures zip like installer/core._install_assets: full folders before their small/
    copies (a small copy is never older than its picture, or the backfill remakes it), a picture
    the player has in a newer version kept, indexes merged. Returns how many pictures it wrote.
    """
    written = 0
    names = archive.namelist()
    folders = sorted((n.rsplit("/", 1)[0] for n in names if n.endswith("/index.json")), key=lambda f: (f.count("/"), f))
    for folder in folders:
        ours = json.loads(archive.read(f"{folder}/index.json"))
        target = game_dir / UI_REL / folder
        target.mkdir(parents=True, exist_ok=True)
        theirs = _index(target)
        merged = dict(theirs)
        for key, version in ours.items():
            png = target / f"{key}.png"
            if theirs.get(key, 0) > version and png.is_file():
                continue
            content = archive.read(f"{folder}/{key}.png")
            try:
                same = png.read_bytes() == content
            except OSError:
                same = False
            if not same:
                _write(png, content)
                written += 1
            merged[key] = version
            time.sleep(0.001)  # a few ms of Python per picture at most: let the game thread in
        _write(target / "index.json", json.dumps(merged, indent=4).encode("utf-8"))
    return written


def restore(game_dir: Path, settings_dir: Path, log: Any, manifest_url: str | None = None) -> int:
    """
    Downloads and merges the release's pictures if the game folder lacks any. Returns how many
    pictures it wrote (0: nothing missing, or nothing to get). Raises on network / file errors.
    """
    want = expected()
    lacking = missing(game_dir, want)
    if not lacking:
        return 0
    url = manifest_url or f"https://github.com/{REPO}/releases/latest/download/manifest.json"
    manifest = json.loads(_get(url, MAX_MANIFEST).decode("utf-8"))
    entry = (manifest.get("mods") or {}).get(MOD_ID, {}).get("pictures")
    if not isinstance(entry, dict) or not entry.get("asset") or not entry.get("sha256"):
        log(f"{len(lacking)} pictures are missing and the latest release has none to download")
        return 0
    state = _read_state(settings_dir)
    if state.get("sha256") == entry["sha256"] and lacking <= set(state.get("left") or ()):
        return 0  # this release's pictures are in; the rest isn't in it
    log(f"{len(lacking)} pictures are missing: downloading them ({int(entry.get('size', 0)) // (1024 * 1024)} MB)")
    asset_url = url.rsplit("/", 1)[0] + "/" + entry["asset"]
    if manifest_url is None and manifest.get("release"):
        asset_url = f"https://github.com/{REPO}/releases/download/{manifest['release']}/{entry['asset']}"
    data = _get(asset_url, MAX_ZIP)
    if entry.get("size") and len(data) != int(entry["size"]):
        raise ValueError(f"pictures download is {len(data)} bytes, expected {entry['size']}")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("pictures download doesn't match its SHA-256")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        written = merge(game_dir, archive)
    settings_dir.mkdir(parents=True, exist_ok=True)
    left = sorted(missing(game_dir, want))
    state = {"sha256": entry["sha256"], "release": manifest.get("release", ""), "at": int(time.time()), "left": left}
    _write(settings_dir / STATE_FILE, json.dumps(state, indent=4).encode("utf-8"))
    log(f"put back {written} pictures from release {manifest.get('release', '?')}"
        + (f" ({len(left)} not in it)" if left else ""))  # fmt: skip
    return written
