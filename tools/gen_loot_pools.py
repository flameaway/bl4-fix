"""
Writes dev/bl4_loot_spawner/pools.json for the dev-only loot spawner mod (dev only).

    python tools/gen_loot_pools.py --oodle <path to an oo2core_*_win64.dll>

From the game's NCS data: every item pool name, plus legendary display names mapped to the pool
that spawns that exact item where one exists (the `_shiny` pools: the same item with a cosmetic).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_drops  # noqa: E402
import gen_item_cards as G  # noqa: E402

OUT = G.REPO / "dev" / "bl4_loot_spawner" / "pools.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=G.DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))

    cache: dict[str, tuple[list[dict], dict[str, str]]] = {}

    def load(name: str) -> tuple[list[dict], dict[str, str]]:
        if name not in cache:
            cache[name] = G._entries(paks, name, args.oodle)  # noqa: SLF001
        return cache[name]

    pools = sorted({entry["key"] for entry in load("itempool")[0]}, key=str.lower)
    by_lower = {p.lower(): p for p in pools}

    drops, _machines, _prints = gen_drops.build_drops(load)
    legendaries: dict[str, str] = {}
    for name, entries in drops.items():
        for entry in entries:
            # "ord_ps.comp_05_legendary_rocketreload" -> "itempool_ord_ps_05_legendary_rocketreload_shiny"
            match = re.match(r"([a-z0-9_]+)\.comp_05_legendary_(\w+)$", entry["item"])
            if match:
                pool = by_lower.get(f"itempool_{match.group(1)}_05_legendary_{match.group(2)}_shiny")
                if pool:
                    legendaries.setdefault(name, pool)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"pools": pools, "legendaries": legendaries}, indent=1) + "\n", encoding="utf-8")
    print(f"{len(pools)} pools, {len(legendaries)} legendaries with their own pool -> {OUT}")


if __name__ == "__main__":
    main()
