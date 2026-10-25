"""
Writes mods/bl4_minimap/maps.json for the Minimap mod (dev only).

    python tools/gen_minimap.py --oodle <path to an oo2core_*_win64.dll>

The big map (ECHO menu) draws each world with a "map actor": a blueprint holding the map model, a
textured plane for small maps or one textured mesh per region for the open world. Its model is the
world at 1/100 scale, around the same origin. The `gbx_discovery_viewable_map_def` NCS records name
the actor per world (`worldname` -> `mapactor`); records can be deltas over a `parent`. A world can
have a second, zoomed-in "detailed view" (`bisdetailedview`, e.g. the city inside World_P); the
minimap uses the main one.

Also records the SHA1s of the data files used ("source_data").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_minimap" / "maps.json"


def _asset(value: str) -> str:
    """`Asset'/Game/X.Y_C'` -> `/Game/X.Y_C`."""
    return value.split("'", 1)[1].rstrip("'") if "'" in value else value


def build(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for entry in entries:  # later chunks patch earlier ones
        if isinstance(entry.get("value"), dict):
            records.setdefault(entry["key"].lower(), {}).update(entry["value"])

    def field(key: str, name: str, depth: int = 0) -> Any:
        record = records.get(key, {})
        if name in record or depth > 8:
            return record.get(name)
        parent = record.get("parent")
        return field(parent.split("'")[1].lower(), name, depth + 1) if parent else None

    maps: dict[str, dict[str, Any]] = {}
    for key in records:
        world, actor = field(key, "worldname"), field(key, "mapactor")
        if not world or not actor or field(key, "bisdetailedview") == "true":
            continue
        maps[world.lower()] = {"def": key, "world": world, "actor": _asset(actor)}
    return dict(sorted(maps.items()))


def main() -> None:
    import gen_item_cards as G  # noqa: PLC0415  # needs the item cards mod's pak.py on the path

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=G.DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))

    entries, fingerprints = G._entries(paks, "gbx_discovery_viewable_map_def", args.oodle)  # noqa: SLF001
    result = {"maps": build(entries), "source_data": dict(sorted(fingerprints.items()))}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for world, info in result["maps"].items():
        print(f"  {world}: {info['actor']}")
    print(f"{len(result['maps'])} maps -> {OUT}")


if __name__ == "__main__":
    main()
