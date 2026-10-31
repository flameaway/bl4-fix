"""
Builds a GitHub release for the in-game updater (dev only).

    python tools/release.py [--tag v2026.10.24] [--allow-same-version] [--offline] [--no-exe]

Writes dist/release/:
- one .sdkmod per mod in installer/catalog.py (byte-for-byte reproducible, see installer/build.py)
- BL4FixSetup.exe, rebuilt first by installer/build.py (unless --no-exe), so the setup app
  installs exactly these files
- <mod>.assets.zip for mods with pictures (catalog `assets`, e.g. Kairosdex's): the mod downloads
  it when the game folder lacks pictures (bl4_weapondex/fetch.py), listed in the manifest as
  mods.<id>.pictures
- manifest.json, which the Mod Menu reads (format: mods/bl4_mod_menu/updates.py), and
  manifest.json.sig: its signature by BL4 Fix's release key (tools/release_key.py). Mod Menus from
  0.2.3 and the setup app refuse a release without a valid one, so this only runs on the PC (and
  Windows user) holding the key, or with --backup-key <the offline backup key file>
- BL4Fix-Manual.zip, the manual install for players who'd rather not run the setup app
  (tools/manual_zip.py: SDK + every .sdkmod + the startup page files), listed in the manifest as
  `manual` (so its SHA-256 is covered by the signature too)
- SHA256SUMS.txt: every file above in sha256sum's format, for players checking a download by hand
- notes.md, the release text (what changed, from each mod's CHANGELOG.md if it has one), with the
  setup app's SHA-256 and its VirusTotal link (uploaded there first when VT_API_KEY is set)

Before writing, it compares every mod with the latest published release (unless --offline): a mod
whose files changed but whose pyproject.toml version didn't is an error, because players would
never be offered it. Bump the version, or pass --allow-same-version for a re-release. A mod whose
files differ from the published ones only in line endings is fine (installer/build.py file_bytes
normalizes them since 2026-10-30, so the first release after that differs everywhere).

Once published, .github/workflows/verify-release.yml rebuilds the .sdkmods, the pictures zips and
the manual zip from the tag on GitHub's machines (tools/verify_release.py), checks they're
byte-identical to the release's and attests them (`gh attestation verify <file> --repo
flameaway/bl4-fix`). So build from a clean tree: committed files only (see the release notes in
memory / CLAUDE.md), with Python 3.11.4 (its zlib 1.2.13; another zlib can deflate differently).

Then publish: on GitHub, Releases > Draft a new release, tag = the tag printed here, attach every
file in dist/release/, publish (not as a pre-release: the updater follows "latest"). Or with the
GitHub CLI:

    gh release create <tag> dist/release/* --title <tag> --notes-file dist/release/notes.md

The repository's releases are immutable (since 2026-10-30): once published, files can't be added,
replaced or removed and the tag can't move (a ruleset protects v* tags). Attach everything before
publishing (gh does: it uploads into a draft first); a mistake needs a new tag.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tomllib
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "dist" / "release"
sys.path.insert(0, str(REPO_ROOT / "installer"))
sys.path.insert(0, str(REPO_ROOT / "mods" / "bl4_direct"))

import build  # noqa: E402  # installer/build.py
import release_key  # noqa: E402  # tools/release_key.py (this folder)
import catalog  # noqa: E402
import shared  # noqa: E402
import manual_zip  # noqa: E402  # tools/manual_zip.py (this folder)

UPDATES_PY = REPO_ROOT / "mods" / "bl4_mod_menu" / "updates.py"
MANIFEST_FORMAT = 1
SUMS_NAME = "SHA256SUMS.txt"
# The zlib the GitHub Actions rebuild uses (Python 3.11.4 there): deflate output can differ by version
ZLIB_PINNED = "1.2.13"


def same_but_line_endings(old: bytes, new: bytes) -> bool:
    """Whether two .sdkmods hold the same files apart from CRLF vs LF."""
    try:
        with zipfile.ZipFile(io.BytesIO(old)) as a, zipfile.ZipFile(io.BytesIO(new)) as b:
            if sorted(a.namelist()) != sorted(b.namelist()):
                return False
            return all(a.read(n).replace(b"\r\n", b"\n") == b.read(n).replace(b"\r\n", b"\n") for n in a.namelist())
    except zipfile.BadZipFile:
        return False


def published_asset(repo: str, tag: str, asset: str) -> bytes | None:
    url = f"https://github.com/{repo}/releases/download/{tag}/{asset}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "bl4-release"}), timeout=60) as r:  # noqa: S310
            return r.read()
    except Exception as ex:  # noqa: BLE001
        print(f"couldn't download the published {asset} ({ex})")
        return None


def write_sums(folder: Path) -> Path:
    """SHA256SUMS.txt over every file in the release folder (sha256sum -c format)."""
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()} *{path.name}"
        for path in sorted(folder.iterdir(), key=lambda p: p.name)
        if path.is_file() and path.name not in (SUMS_NAME, "notes.md")
    ]
    out = folder / SUMS_NAME
    out.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return out


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


def virustotal_upload(exe: Path) -> bool:
    """Uploads the setup app to VirusTotal for a scan when VT_API_KEY is set (a free account's key),
    so the release notes' link shows a result. False when not uploaded."""
    key = os.environ.get("VT_API_KEY")
    if not key:
        print("VT_API_KEY not set: the setup app isn't uploaded to VirusTotal")
        return False
    headers = {"x-apikey": key, "User-Agent": "bl4-release"}
    try:
        # Files over 32 MB (the setup app is ~100 MB) go to a one-time upload link
        request = urllib.request.Request("https://www.virustotal.com/api/v3/files/upload_url", headers=headers)
        with urllib.request.urlopen(request, timeout=30) as r:  # noqa: S310
            url = json.loads(r.read())["data"]
        boundary = uuid.uuid4().hex
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{exe.name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n")  # fmt: skip
        body = head.encode() + exe.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(url, data=body, headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=600) as r:  # noqa: S310
            r.read()
    except Exception as ex:  # noqa: BLE001 - the release doesn't depend on it
        print(f"VirusTotal upload failed ({ex}): upload {exe.name} there by hand")
        return False
    print("uploaded the setup app to VirusTotal (its scan takes a few minutes)")
    return True


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:4])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", default="v" + dt.date.today().strftime("%Y.%m.%d"))
    parser.add_argument("--allow-same-version", action="store_true")
    parser.add_argument("--offline", action="store_true", help="don't compare with the published release")
    parser.add_argument("--no-exe", action="store_true", help="use the existing dist/BL4FixSetup.exe")
    parser.add_argument("--backup-key", type=Path, help="sign with the offline backup key file instead of this PC's key")
    args = parser.parse_args()

    repo = update_repo()
    if not repo:
        print("warning: REPO in mods/bl4_mod_menu/updates.py is empty, so the Mod Menu won't check for updates")
    if zlib.ZLIB_RUNTIME_VERSION != ZLIB_PINNED:
        print(f"warning: zlib {zlib.ZLIB_RUNTIME_VERSION}, not {ZLIB_PINNED} (Python 3.11.4): GitHub's rebuild may not match")
    if not args.no_exe:
        build.main()
    # The release asset name: the Mod Menu's updater takes it from the manifest ("asset"), so the
    # rename from BL4DirectSetup.exe works for older versions too
    exe = REPO_ROOT / "dist" / "BL4FixSetup.exe"
    if not exe.is_file():
        raise SystemExit("dist/BL4FixSetup.exe is missing: run installer/build.py")

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    build.check_picture_lists()
    previous = None if args.offline else previous_manifest(repo)
    old_mods = (previous or {}).get("mods", {})
    problems: list[str] = []
    mods: dict[str, dict] = {}
    notes_md = [f"# {args.tag}", ""]
    for info in catalog.MODS:
        src = REPO_ROOT / info.source / info.id
        out = OUT / f"{info.id}.sdkmod"
        build.build_sdkmod(src, out, catalog.license_files(info, REPO_ROOT))
        data = out.read_bytes()
        version = mod_version(src)
        digest = hashlib.sha256(data).hexdigest()
        old = old_mods.get(info.id)
        if old:
            if vkey(version) < vkey(str(old.get("version", ""))):
                problems.append(f"{info.id}: {version} is older than the published {old['version']}")
            elif version == old.get("version") and digest != old.get("sha256") and not args.allow_same_version:
                published = published_asset(repo, str(previous["release"]), str(old.get("asset") or out.name))
                if published is not None and same_but_line_endings(published, data):
                    print(f"{info.id}: same files as the published {version} apart from line endings")
                else:
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
            "license": catalog.license_id(info),
        }
        if info.assets:
            pictures = OUT / f"{info.id}.assets.zip"
            count = build.build_assets(info, pictures)
            blob = pictures.read_bytes()
            mods[info.id]["pictures"] = {
                "asset": pictures.name,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size": len(blob),
                "count": count,
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
    manual = manual_zip.build(OUT / manual_zip.zip_name(args.tag), OUT, build.fetch_sdk(), args.tag)
    manual_data = manual.read_bytes()
    manifest = {
        "format": MANIFEST_FORMAT,
        "release": args.tag,
        "repo": repo,
        "sdk": shared.SDK_VERSION,
        "mods": mods,
        "app": {"version": shared.MOD_VERSION, "asset": exe.name, "sha256": hashlib.sha256(app).hexdigest(), "size": len(app)},
        # The no-exe install (README "Install without the setup app"); nothing reads it automatically
        "manual": {"asset": manual.name, "sha256": hashlib.sha256(manual_data).hexdigest(), "size": len(manual_data)},
    }
    (OUT / "manifest.json").write_bytes((json.dumps(manifest, indent=1) + "\n").encode("utf-8"))
    try:
        # The signature covers these exact bytes; players' Mod Menus refuse the release without it
        release_key.sign_file(OUT / "manifest.json", args.backup_key)
    except (RuntimeError, OSError) as ex:
        print(f"couldn't sign manifest.json: {ex}")
        shutil.rmtree(OUT, ignore_errors=True)
        return 1
    if len(notes_md) == 2:  # noqa: PLR2004
        notes_md.append("No mod versions changed.")
    app_sha = manifest["app"]["sha256"]
    scanned = virustotal_upload(exe)
    write_sums(OUT)
    notes_md += ["", "Install or update: run BL4FixSetup.exe, or press Update all on MODS in game.",
                 f"No exe: extract `{manual.name}` into the game folder (steps in its BL4Fix-README.txt and the README).", "",
                 f"**Check your download:** `{SUMS_NAME}` lists every file's SHA-256; the mod files and `{manual.name}` are"
                 " rebuilt from this tag by GitHub Actions and attested (`gh attestation verify <file> --repo flameaway/bl4-fix`).",
                 f"BL4FixSetup.exe SHA-256 `{app_sha}` (PowerShell: `Get-FileHash BL4FixSetup.exe`).",
                 f"VirusTotal: https://www.virustotal.com/gui/file/{app_sha}"
                 + ("" if scanned else " (if it says not found, upload the exe there yourself)"),
                 "Mod files are checked automatically: manifest.json is signed by BL4 Fix's release key and lists each file's SHA-256."]  # fmt: skip
    (OUT / "notes.md").write_text("\n".join(notes_md) + "\n", encoding="utf-8")

    print(f"\n{len(mods)} mods + setup app + manifest in {OUT}")
    print("\n".join(notes_md[2:]))
    print(f"\nPublish: gh release create {args.tag} dist/release/* --title {args.tag} --notes-file dist/release/notes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
