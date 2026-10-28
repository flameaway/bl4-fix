"""
Copies the mods' pictures (catalog `assets`, e.g. Kairosdex's boss and item pictures) from this
machine's game folder into assets/ in the repo, where installer/build.py takes them from.

    python tools/sync_pictures.py [--game <Borderlands 4 folder>] [--check]

They're made in game (Kairosdex's `dex bake all` / `dex bake items`: every boss and item rendered,
a long job), so they're kept in git instead of making every builder bake them. Run this after a
bake, then commit assets/. Each folder and its small/ copies: index.json plus the pictures it lists,
nothing else (raw/ and trials/ are bake scratch); pictures no longer listed are removed.
--check only reports what would change (build.py runs it to warn about unsynced bakes).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "assets"
DEFAULT_GAME = Path(os.environ.get("BL4_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4"))
sys.path.insert(0, str(REPO / "installer"))
import catalog  # noqa: E402


def _index(folder: Path) -> dict[str, int]:
    try:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return index if isinstance(index, dict) else {}


def sync(game: Path, check: bool = False) -> list[str]:
    """What differs between the game folder's pictures and assets/ (copied over unless check)."""
    ui = game / "OakGame/Content/uiresources"
    changes: list[str] = []
    for info in catalog.MODS:
        for rel in info.assets:
            for sub in (rel, f"{rel}/small"):
                src, dst = ui / sub, ASSETS / sub
                theirs, ours = _index(src), _index(dst)
                if not theirs:
                    continue  # nothing baked on this machine: keep what the repo has
                if missing := [key for key in theirs if not (src / f"{key}.png").is_file()]:
                    raise SystemExit(f"{len(missing)} pictures listed in {src / 'index.json'} are missing, e.g. {missing[0]}")
                changed = [key for key, version in theirs.items() if ours.get(key) != version or not (dst / f"{key}.png").is_file()]
                gone = [key for key in ours if key not in theirs]
                if changed or gone:
                    changes.append(f"{sub}: {len(changed)} new or changed, {len(gone)} removed")
                if check or not (changed or gone):
                    continue
                dst.mkdir(parents=True, exist_ok=True)
                for key in changed:
                    (dst / f"{key}.png").write_bytes((src / f"{key}.png").read_bytes())
                for key in gone:
                    (dst / f"{key}.png").unlink(missing_ok=True)
                tmp = dst / "index.json.tmp"
                tmp.write_bytes((src / "index.json").read_bytes())
                tmp.replace(dst / "index.json")
    return changes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="the Borderlands 4 folder")
    parser.add_argument("--check", action="store_true", help="only report what would change")
    args = parser.parse_args()
    changes = sync(args.game, args.check)
    for line in changes:
        print(line)
    if not changes:
        print("assets/ is up to date")
    elif not args.check:
        print(f"updated {ASSETS}: commit it")


if __name__ == "__main__":
    main()
