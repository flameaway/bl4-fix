"""
Rebuilds a published release from source and checks it byte for byte (GitHub Actions:
.github/workflows/verify-release.yml; also runs on a dev PC).

    gh release download <tag> --dir <dir>
    python tools/verify_release.py --tag <tag> --release-dir <dir> [--list <file>]

Run it on a checkout of the tag. It checks:
- manifest.json.sig: signed by BL4 Fix's release key (mods/bl4_mod_menu/signing.py, Windows' bcrypt)
- every .sdkmod and pictures zip the manifest lists: rebuilt here (installer/build.py) equal to the
  manifest's SHA-256 and to the release's file
- the manual zip (manifest `manual`, tools/manual_zip.py): rebuilt from the rebuilt .sdkmods and
  the pinned SDK, equal to the release's
- SHA256SUMS.txt: every line matches its file

Exit 0 when all match. --list writes the verified files' paths (one per line) for the attestation
step. The setup exe isn't rebuilt (its window uses game files that only a PC with the game has):
its SHA-256 is checked against the signed manifest and SHA256SUMS.txt only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for extra in (REPO_ROOT / "installer", REPO_ROOT / "mods" / "bl4_direct", REPO_ROOT / "mods" / "bl4_mod_menu"):
    sys.path.insert(0, str(extra))

import build  # noqa: E402
import catalog  # noqa: E402
import manual_zip  # noqa: E402  # tools/ (this folder)
import signing  # noqa: E402  # mods/bl4_mod_menu/signing.py


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--list", type=Path, help="write the verified files' paths here")
    args = parser.parse_args()
    rel = args.release_dir
    problems: list[str] = []
    verified: list[Path] = []

    def compare(name: str, built: bytes, listed: str | None) -> None:
        path = rel / name
        if not path.is_file():
            problems.append(f"{name}: not in the release")
            return
        ours, theirs = sha(built), sha(path.read_bytes())
        if listed is not None and listed != theirs:
            problems.append(f"{name}: the manifest says {listed[:12]}, the release file is {theirs[:12]}")
        elif ours != theirs:
            problems.append(f"{name}: rebuilt {ours[:12]} != released {theirs[:12]}")
        else:
            verified.append(path)
            print(f"OK  {name}  {ours}")

    data = (rel / "manifest.json").read_bytes()
    try:
        signing.verify(data, (rel / ("manifest.json" + signing.SIGNATURE_SUFFIX)).read_bytes())
        print("OK  manifest.json signed by BL4 Fix's release key")
    except (OSError, signing.SignatureError) as ex:
        problems.append(f"manifest.json signature: {ex}")
    manifest = json.loads(data)
    if manifest.get("release") != args.tag:
        problems.append(f"manifest.json is for {manifest.get('release')}, not {args.tag}")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for mod_id, entry in manifest["mods"].items():
            info = catalog.BY_ID.get(mod_id)
            if info is None:
                problems.append(f"{mod_id}: in the manifest but not in installer/catalog.py at this tag")
                continue
            sdkmod = out / f"{mod_id}.sdkmod"
            build.build_sdkmod(REPO_ROOT / info.source / info.id, sdkmod, catalog.license_files(info, REPO_ROOT))
            compare(entry["asset"], sdkmod.read_bytes(), entry.get("sha256"))
            if pictures := entry.get("pictures"):
                assets = out / pictures["asset"]
                build.build_assets(info, assets)
                compare(pictures["asset"], assets.read_bytes(), pictures.get("sha256"))
        if manual := manifest.get("manual"):
            built = manual_zip.build(out / manual["asset"], out, build.fetch_sdk(), args.tag)
            compare(manual["asset"], built.read_bytes(), manual.get("sha256"))
        else:
            problems.append("the manifest lists no manual zip (a release from before 2026-10-30?)")

    app = manifest.get("app") or {}
    exe = rel / str(app.get("asset", ""))
    if exe.is_file() and sha(exe.read_bytes()) == app.get("sha256"):
        print(f"OK  {exe.name} matches the signed manifest (not rebuilt: it needs the game's files)")
    else:
        problems.append(f"{exe.name}: missing or not the manifest's SHA-256")

    sums = rel / "SHA256SUMS.txt"
    if sums.is_file():
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, _, name = line.partition(" *")
            path = rel / name
            if not path.is_file() or sha(path.read_bytes()) != digest:
                problems.append(f"SHA256SUMS.txt: {name} doesn't match")
        print("OK  SHA256SUMS.txt" if not any(p.startswith("SHA256SUMS") for p in problems) else "SHA256SUMS.txt has mismatches")
    else:
        problems.append("SHA256SUMS.txt: not in the release")

    if args.list:
        args.list.write_text("".join(f"{p.as_posix()}\n" for p in verified), encoding="utf-8")
    if problems:
        print("\nNOT VERIFIED:\n" + "\n".join(f"- {p}" for p in problems))
        return 1
    print(f"\nAll {len(verified)} rebuilt files match the release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
