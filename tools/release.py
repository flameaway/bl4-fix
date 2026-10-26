"""
Builds a GitHub release for the in-game updater (dev only).

    python tools/release.py [--tag v2026.10.24] [--allow-same-version] [--offline] [--no-exe]

Writes dist/release/:
- one .sdkmod per mod in installer/catalog.py (byte-for-byte reproducible, see installer/build.py)
- BL4FixSetup.exe, rebuilt first by installer/build.py (unless --no-exe), so the setup app
  installs exactly these files
- manifest.json, which the Mod Menu reads (format: mods/bl4_mod_menu/updates.py)
- notes.md, the release text (what changed, from each mod's CHANGELOG.md if it has one)

Before writing, it compares every mod with the latest published release (unless --offline): a mod
whose files changed but whose pyproject.toml version didn't is an error, because players would
never be offered it. Bump the version, or pass --allow-same-version for a re-release.

Then publish: on GitHub, Releases > Draft a new release, tag = the tag printed here, attach every
file in dist/release/, publish (not as a pre-release: the updater follows "latest"). Or with the
GitHub CLI:

    gh release create <tag> dist/release/* --title <tag> --notes-file dist/release/notes.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import tomllib
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "dist" / "release"
sys.path.insert(0, str(REPO_ROOT / "installer"))
sys.path.insert(0, str(REPO_ROOT / "mods" / "bl4_direct"))

import build  # noqa: E402  # installer/build.py
import catalog  # noqa: E402
import shared  # noqa: E402

UPDATES_PY = REPO_ROOT / "mods" / "bl4_mod_menu" / "updates.py"
MANIFEST_FORMAT = 1


def update_repo() -> str:
    match = re.search(r'^REPO = "([^"]*)"', UPDATES_PY.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def project(src: Path) -> dict:
    return tomllib.loads((src / "pyproject.toml").read_text(encoding="utf-8"))


def mod_version(src: Path) -> str:
    data = project(src)
    return str(data.get("tool", {}).get("sdkmod", {}).get("version") or data["project"]["version"])


def latest_notes(src: Path) -> str:
    """The first section of the mod's CHANGELOG.md (up to the second '## ' heading), if any."""
    path = src / "CHANGELOG.md"
    if not path.is_file():
        return ""
    sections = re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)
    if len(sections) < 2:  # noqa: PLR2004
        return ""
    body = sections[1].split("\n", 1)[1] if "\n" in sections[1] else ""
    return body.strip()


def previous_manifest(repo: str) -> dict | None:
    if not repo:
        return None
    url = f"https://github.com/{repo}/releases/latest/download/manifest.json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "bl4-release"}), timeout=20) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except Exception as ex:  # noqa: BLE001 - first release, or offline
        print(f"no previous release to compare with ({ex})")
        return None


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:4])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", default="v" + dt.date.today().strftime("%Y.%m.%d"))
    parser.add_argument("--allow-same-version", action="store_true")
    parser.add_argument("--offline", action="store_true", help="don't compare with the published release")
    parser.add_argument("--no-exe", action="store_true", help="use the existing dist/BL4FixSetup.exe")
    args = parser.parse_args()

    repo = update_repo()
    if not repo:
        print("warning: REPO in mods/bl4_mod_menu/updates.py is empty, so the Mod Menu won't check for updates")
    if not args.no_exe:
        build.main()
    # The release asset name: the Mod Menu's updater takes it from the manifest ("asset"), so the
    # rename from BL4DirectSetup.exe works for older versions too
    exe = REPO_ROOT / "dist" / "BL4FixSetup.exe"
    if not exe.is_file():
        raise SystemExit("dist/BL4FixSetup.exe is missing: run installer/build.py")

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    previous = None if args.offline else previous_manifest(repo)
    old_mods = (previous or {}).get("mods", {})
    problems: list[str] = []
    mods: dict[str, dict] = {}
    notes_md = [f"# {args.tag}", ""]
    for info in catalog.MODS:
        src = REPO_ROOT / info.source / info.id
        out = OUT / f"{info.id}.sdkmod"
        extra = {"LICENSE": REPO_ROOT / "ported" / "LICENSE"} if info.source == "ported" else None
        build.build_sdkmod(src, out, extra)
        data = out.read_bytes()
        version = mod_version(src)
        digest = hashlib.sha256(data).hexdigest()
        old = old_mods.get(info.id)
        if old:
            if vkey(version) < vkey(str(old.get("version", ""))):
                problems.append(f"{info.id}: {version} is older than the published {old['version']}")
            elif version == old.get("version") and digest != old.get("sha256") and not args.allow_same_version:
                problems.append(f"{info.id}: files changed but the version is still {version} (bump pyproject.toml)")
        notes = latest_notes(src)
        mods[info.id] = {
            "name": info.name,
            "version": version,
            "asset": out.name,
            "sha256": digest,
            "size": len(data),
            "blurb": info.blurb,
            "notes": notes,
            "required": info.required,
            "license": "GPL-3.0" if info.source == "ported" else "",
        }
        changed = not old or old.get("version") != version
        if changed:
            notes_md.append(f"- **{info.name}** {version}" + (f" (was {old['version']})" if old else " (new)")
                            + (f": {notes}" if notes else ""))  # fmt: skip
    if problems:
        print("\n".join(problems))
        shutil.rmtree(OUT, ignore_errors=True)
        return 1

    shutil.copyfile(exe, OUT / exe.name)
    app = exe.read_bytes()
    manifest = {
        "format": MANIFEST_FORMAT,
        "release": args.tag,
        "repo": repo,
        "sdk": shared.SDK_VERSION,
        "mods": mods,
        "app": {"version": shared.MOD_VERSION, "asset": exe.name, "sha256": hashlib.sha256(app).hexdigest(), "size": len(app)},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    if len(notes_md) == 2:  # noqa: PLR2004
        notes_md.append("No mod versions changed.")
    notes_md += ["", "Install or update: run BL4FixSetup.exe, or press Update all on MODS in game."]
    (OUT / "notes.md").write_text("\n".join(notes_md) + "\n", encoding="utf-8")

    print(f"\n{len(mods)} mods + setup app + manifest in {OUT}")
    print("\n".join(notes_md[2:]))
    print(f"\nPublish: gh release create {args.tag} dist/release/* --title {args.tag} --notes-file dist/release/notes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
