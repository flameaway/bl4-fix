"""
Writes mods/bl4_builds/trees.json for the Build Presets mod (dev only).

    python tools/gen_builds_trees.py --oodle <path to an oo2core_*_win64.dll>

The skill screen colors each class's three trees red, green or blue. That comes from the
`skilltrees_data` NCS records (one per class, OakUISkillTreesDef): each tree has its display name,
its color (left out for red, the enum's default) and segments, one per progress graph (the trunk
and its three branches). We write {graph: {"tree": name, "color": color}} so a build can say which
trees its points are in, in their colors.

Only display data: if a game patch renames a tree, the squares show the old name until this is
run again; nothing else depends on it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ncs_parse  # noqa: E402
from gen_item_cards import DEFAULT_GAME, _entries  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "mods" / "bl4_builds" / "trees.json"
DEFAULT_COLOR = "red"
_REF = re.compile(r"'([^']+)'")


def build(paks: list[Path], oodle: Path) -> dict[str, dict[str, str]]:
    entries, _ = _entries(paks, "skilltrees_data", oodle)
    out: dict[str, dict[str, str]] = {}
    for entry in entries:
        value = entry.get("value")
        if not isinstance(value, dict):
            continue
        for tree in value.get("skilltrees") or []:
            if not isinstance(tree, dict):
                continue
            name = ncs_parse.loc_text(str(tree.get("treename") or ""))
            color = str(tree.get("color") or DEFAULT_COLOR).lower()
            for segment in tree.get("segments") or []:
                match = _REF.search(str(segment.get("progressgraph") if isinstance(segment, dict) else ""))
                if match:
                    out[match.group(1)] = {"tree": name, "color": color}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    trees = build(paks, args.oodle)
    if not trees:
        raise SystemExit("no skill trees found")
    OUT.write_text(json.dumps(trees, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(trees)} graphs in {len({t['tree'] for t in trees.values()})} trees -> {OUT}")


if __name__ == "__main__":
    main()
