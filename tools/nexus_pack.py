"""
Packs one mod for a manual install from Nexus Mods (dev only).

    python tools/nexus_pack.py bl4_weapondex [--no-pictures]

Writes dist/nexus/<mod>/:
- <Name>-<version>.zip: sdk_mods/<mod>.sdkmod, to extract into the game folder. The .sdkmod is the
  same build as the GitHub release's (installer/build.py), so the Mod Menu's updater treats it
  like an installed one; a warning is printed if its bytes differ from the published file of the
  same version (an unpublished change, or the CRLF trap: build from the main tree's bytes).
- <Name>-Pictures-<version>.zip, for mods with pictures (catalog `assets`, e.g. Kairosdex's boss
  and item pictures, from assets/ in the repo like the setup app's):
  OakGame/Content/uiresources/..., to extract into the game folder. An optional file on Nexus.
- README.txt: install steps, requirements and the first-launch note, for the Nexus description.

The SDK (oak2-mod-manager) is not bundled (nor a mod a NOTES entry `needs`): the readme links them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "dist" / "nexus"
sys.path.insert(0, str(REPO_ROOT / "installer"))

import build  # noqa: E402  # installer/build.py
import catalog  # noqa: E402

GITHUB = "https://github.com/flameaway/bl4-direct"
SDK_PAGE = "https://github.com/bl-sdk/oak2-mod-manager/releases"
MANIFEST_URL = f"{GITHUB}/releases/latest/download/manifest.json"
UI_PREFIX = "OakGame/Content/uiresources"

# Per mod: what it needs besides the SDK, and what happens on its first launch
NOTES: dict[str, dict[str, str]] = {
    "bl4_weapondex": {
        "works_with": (
            "Works on its own, next to other mods' menu pages: its screen runs through the BL4 UI\n"
            "Link, one small shared .pak (Paks/~mods/BL4Fix_UILink_4251_P.pak) that replaces no game\n"
            "page. Settings work in any SDK mod menu (or type mods in the console)."
        ),
        "use": "F8 in game, the Kairosdex entry in the pause menu, or type dex in the console.",
        "first": (
            "The Kairosdex screen (F8, the pause menu entry) and the new-boss / new-legendary HUD\n"
            "banners are set up during the first launch and show from the second one (the game\n"
            "reads its .pak files only at startup). Restart the game once after installing."
        ),
        "pictures": "Boss portraits and item pictures. Without them the dex shows silhouettes.",
    },
}


def published_sha256(mod_id: str, version: str) -> str | None:
    """The latest GitHub release's hash for this mod at this version, None if it isn't there."""
    try:
        with urllib.request.urlopen(MANIFEST_URL, timeout=15) as resp:  # noqa: S310
            entry = json.load(resp)["mods"].get(mod_id) or {}
    except Exception as ex:  # noqa: BLE001
        print(f"warning: couldn't read the published manifest ({ex}); not compared")
        return None
    return entry.get("sha256") if entry.get("version") == version else None


def pictures_zip(info: catalog.ModInfo, out: Path) -> int:
    """build.build_assets' pictures, under the game folder's path instead of uiresources/."""
    tmp = out.with_suffix(".tmp")
    count = build.build_assets(info, tmp)
    with zipfile.ZipFile(tmp) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as dst:
        for item in src.infolist():
            zinfo = zipfile.ZipInfo(f"{UI_PREFIX}/{item.filename}", item.date_time)
            zinfo.external_attr = item.external_attr
            dst.writestr(zinfo, src.read(item))
    tmp.unlink()
    return count


def readme(info: catalog.ModInfo, version: str, description: str, has_pictures: bool) -> str:
    notes = NOTES.get(info.id, {})
    lines = [
        f"{info.name} {version}",
        "",
        description,
        "",
        "REQUIREMENTS",
        f"- The Borderlands 4 Python SDK (oak2-mod-manager): {SDK_PAGE}",
    ]
    if needs := notes.get("needs"):
        lines.append(f"- {needs}")
        lines.append(f"  Download it from {GITHUB}/releases/latest (or install everything with BL4FixSetup.exe there).")
    if works_with := notes.get("works_with"):
        lines += ["", works_with]
    lines += [
        "",
        "INSTALL",
        f"1. Extract {info.name}-{version}.zip into the Borderlands 4 game folder",
        f"   (it puts sdk_mods/{info.id}.sdkmod in place).",
    ]
    if has_pictures:
        lines += [
            f"2. Optional: extract {info.name}-Pictures-{version}.zip into the same folder.",
            f"   {notes.get('pictures', '')}".rstrip(),
        ]
    if first := notes.get("first"):
        lines += ["", "FIRST LAUNCH", first]
    if use := notes.get("use"):
        lines += ["", "USE", use]
    leftovers = [f"sdk_mods/{info.id}.sdkmod"]
    leftovers += [f"{UI_PREFIX}/{folder}/" for folder in info.ui_dirs]
    leftovers += [f"OakGame/Content/Paks/~mods/{pak}" for pak in info.paks]
    lines += [
        "",
        "UPDATES",
        "Check this page for new versions (with BL4 Mod Menu installed, it offers them in game).",
        "",
        "UNINSTALL (with the game closed)",
        "Delete:",
        *(f"- {path}" for path in leftovers),
        "Settings stay in sdk_mods/settings/.",
        "",
        f"Source and issues: {GITHUB}",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mod", help="catalog id, e.g. bl4_weapondex")
    parser.add_argument("--no-pictures", action="store_true", help="skip the pictures zip")
    args = parser.parse_args()

    info = next((m for m in catalog.MODS if m.id == args.mod), None)
    if info is None:
        raise SystemExit(f"{args.mod} isn't in installer/catalog.py")
    src = REPO_ROOT / info.source / info.id
    project = tomllib.loads((src / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    name = info.name.replace(" ", "")

    out = OUT / info.id
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    sdkmod = out / f"{info.id}.sdkmod"
    extra = {"LICENSE": REPO_ROOT / "ported" / "LICENSE"} if info.source == "ported" else None
    build.build_sdkmod(src, sdkmod, extra)
    digest = hashlib.sha256(sdkmod.read_bytes()).hexdigest()
    published = published_sha256(info.id, version)
    if published is None:
        print(f"note: {version} isn't the published version of {info.id}: publish it first, or the updater won't know it")
    elif published != digest:
        print(f"warning: {info.id}.sdkmod differs from the published {version} (unbumped change or CRLF bytes)")
    else:
        print(f"{info.id}.sdkmod matches the published {version}")

    main_zip = out / f"{name}-{version}.zip"
    with zipfile.ZipFile(main_zip, "w", zipfile.ZIP_STORED) as zf:  # the .sdkmod is compressed already
        zinfo = zipfile.ZipInfo(f"sdk_mods/{info.id}.sdkmod", build.ZIP_DATE)
        zinfo.external_attr = 0o644 << 16
        zf.writestr(zinfo, sdkmod.read_bytes())
    sdkmod.unlink()
    print(f"wrote {main_zip.relative_to(REPO_ROOT)} ({main_zip.stat().st_size // 1024} KB)")

    has_pictures = bool(info.assets) and not args.no_pictures
    if has_pictures:
        pics = out / f"{name}-Pictures-{version}.zip"
        count = pictures_zip(info, pics)
        print(f"wrote {pics.relative_to(REPO_ROOT)} ({count} pictures, {pics.stat().st_size // (1024 * 1024)} MB)")

    (out / "README.txt").write_text(readme(info, version, project.get("description", info.blurb), has_pictures), encoding="utf-8")
    print(f"wrote {(out / 'README.txt').relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
