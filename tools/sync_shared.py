"""
Copies the shared packages in shared/ into the mods that carry them (dev only).

    python tools/sync_shared.py           copy (only files that differ)
    python tools/sync_shared.py --check   exit 1 if any mod's copy differs

- shared/uilink/  the BL4 UI Link (page scripts without the Mod Menu, one shared pak)
- shared/health/  "needs an update" statuses and the notice (whichever of our mods runs)

Each mod carries its own copy (`<mod>/<package>/`) so it works alone; at run time the newest copy
loaded does the work for all of them. Never edit a mod's copy.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# package -> the mods that carry it
PACKAGES = {
    "uilink": ["mods/bl4_builds", "mods/bl4_tidy_pause", "mods/bl4_reroll", "mods/bl4_uvh_teleport",
               "mods/bl4_damage_meter", "mods/bl4_weapondex", "mods/bl4_mod_menu", "mods/bl4_item_cards",
               "mods/bl4_respec", "mods/bl4_instant_tabs"],
    "health": ["mods/bl4_builds", "mods/bl4_tidy_pause", "mods/bl4_mod_menu", "mods/bl4_reroll",
               "mods/bl4_uvh_teleport", "mods/bl4_damage_meter", "mods/bl4_weapondex",
               "mods/bl4_backpack_size", "mods/bl4_direct", "mods/bl4_echo_anywhere", "mods/bl4_instant_travel",
               "mods/bl4_item_cards", "mods/bl4_minimap", "mods/bl4_third_person", "mods/bl4_unstuck",
               "mods/bl4_respec", "mods/bl4_instant_tabs"],
}
SKIP = {"__pycache__"}


def files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file() and not SKIP & set(p.parts))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only report copies that differ")
    args = parser.parse_args()
    stale = 0
    for package, mods in PACKAGES.items():
        source = REPO / "shared" / package
        wanted = files(source)
        for mod in mods:
            target = REPO / mod / package
            have = files(target) if target.is_dir() else []
            for rel in wanted:
                if (target / rel).is_file() and filecmp.cmp(source / rel, target / rel, shallow=False):
                    continue
                stale += 1
                if args.check:
                    print(f"{mod}/{package}/{rel.as_posix()} differs from shared/{package}")
                    continue
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / rel, target / rel)
                print(f"{mod}/{package}/{rel.as_posix()}")
            for rel in set(have) - set(wanted):
                stale += 1
                if args.check:
                    print(f"{mod}/{package}/{rel.as_posix()} isn't in shared/{package}")
                    continue
                (target / rel).unlink()
                print(f"{mod}/{package}/{rel.as_posix()} removed")
    if args.check:
        return 1 if stale else 0
    print(f"{stale} file(s) copied" if stale else "every copy is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
