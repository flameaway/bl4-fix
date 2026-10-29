"""
Writes a mod repository index (manifest.json) for the BL4 Mod Menu's Discover page, from .sdkmod
files. For any modder, not only BL4 Fix (whose releases use tools/release.py). Guide:
docs/mod-repos.md.

    python tools/make_repo.py dist/*.sdkmod --tag v1.2.0 --name "My Mods" --out dist/manifest.json

Then attach the .sdkmod files and manifest.json to the GitHub release <tag> (the newest release),
and players add your repository as owner/name. For an index hosted elsewhere (GitHub Pages, any
https site), pass --base-url: each mod gets a full download link instead of a release asset name.

Each mod's name, version, description and author come from its pyproject.toml. Optional extras
go in the same file:

    [tool.bl4_mod_menu]
    blurb = "One line for the Discover list."
    icon = "https://.../icon.png"                # PNG or JPEG, square, 128-256 px
    images = ["https://.../shot1.jpg"]            # up to 6, 16:9 looks best, at most 4096 px
    homepage = "https://www.nexusmods.com/..."
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path

FORMAT = 1


def read_mod(path: Path) -> tuple[str, dict]:
    mod_id = path.stem
    data = path.read_bytes()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        roots = {n.split("/", 1)[0] for n in names if n}
        if roots != {mod_id} or f"{mod_id}/__init__.py" not in names:
            raise SystemExit(f"{path.name}: an .sdkmod must hold one folder named {mod_id} with an __init__.py")
        try:
            toml = tomllib.loads(zf.read(f"{mod_id}/pyproject.toml").decode("utf-8"))
        except KeyError:
            raise SystemExit(f"{path.name}: no {mod_id}/pyproject.toml (the version comes from it)") from None
    project = toml.get("project", {})
    sdkmod = toml.get("tool", {}).get("sdkmod", {})
    extra = toml.get("tool", {}).get("bl4_mod_menu", {})
    version = sdkmod.get("version") or project.get("version")
    if not version:
        raise SystemExit(f"{path.name}: pyproject.toml has no version")
    authors = ", ".join(a.get("name", "") for a in project.get("authors", []) if isinstance(a, dict) and a.get("name"))
    entry = {
        "name": sdkmod.get("name") or project.get("name") or mod_id,
        "version": str(version),
        "asset": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "author": authors,
        "description": project.get("description", ""),
        "games": sdkmod.get("supported_games") or ["BL4"],
    }
    for key in ("blurb", "icon", "homepage", "notes"):
        if extra.get(key):
            entry[key] = str(extra[key])
    if isinstance(extra.get("images"), list):
        entry["images"] = [str(i) for i in extra["images"][:6]]
    return mod_id, entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", help=".sdkmod files (globs work)")
    parser.add_argument("--tag", required=True, help="the GitHub release these files are attached to")
    parser.add_argument("--name", default="", help="the repository's name on the Repositories page")
    parser.add_argument("--description", default="")
    parser.add_argument("--homepage", default="")
    parser.add_argument("--base-url", default="", help="for an index hosted outside GitHub releases: where the files are")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    paths = sorted({Path(p) for pattern in args.files for p in (glob.glob(pattern) or [pattern])})
    mods = {}
    for path in paths:
        if path.suffix.lower() != ".sdkmod":
            continue
        mod_id, entry = read_mod(path)
        if args.base_url:
            entry["url"] = args.base_url.rstrip("/") + "/" + entry.pop("asset")
        mods[mod_id] = entry
        print(f"  {mod_id} {entry['version']} ({entry['size']} bytes)")
    if not mods:
        print("no .sdkmod files given", file=sys.stderr)
        return 1
    manifest = {"format": FORMAT, "release": args.tag, "name": args.name, "description": args.description,
                "homepage": args.homepage, "mods": mods}  # fmt: skip
    Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(mods)} mod(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
