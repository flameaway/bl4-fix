"""
Builds mods/bl4_reroll/legendaries.json (dev only, needs Oodle): how the game rolls each legendary
and pearl, so Reroll can roll one again the way a drop would.

    python tools/gen_reroll_legendaries.py --oodle <path to an oo2core_*_win64.dll>
    python tools/gen_reroll_legendaries.py --inv-json <inv entries dump>    (dev: a cached dump)

The rules are Kairosdex's (tools/gen_weapondex_rolls.py, checked against 393 real legendary drops,
see mods/bl4_weapondex/CLAUDE.md "Rolling our own serials"): per slot of the item type, how many
parts, which ones (allowlists, tags each part needs or excludes, caps), their weights and their
level gates. Reroll keeps its own copy so it works without Kairosdex installed.

The items are Kairosdex's (dex.json: every legendary and pearl a player can get, 299 + 19; the
generic `comp_05_legendary` templates and the Anchor turret aren't items). Run gen_weapondex.py
first if the dex changed.

Output: rolls.json's packed format (tables / types as JSON strings, parsed on first use) plus
    "index":  {"<item type serial index>:<composition serial index>": "<inv>.<comp>"}
    "names":  {"<inv>.<comp>": [name, rarity, type, maker]}
    "source_data": SHA1s of the inv files read (the mod steps aside when a patch changes them)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_reroll" / "legendaries.json"
DEX = REPO / "mods" / "bl4_weapondex" / "dex.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_weapondex_rolls as rolls_gen  # noqa: E402


def build(entries: list[dict], prints: dict[str, str]) -> dict:
    dex = json.loads(DEX.read_text(encoding="utf-8"))["items"]
    rolls = rolls_gen.build_rolls(entries, list(dex))
    out = json.loads(rolls_gen.pack(rolls))
    index: dict[str, str] = {}
    for inv, text in out["types"].items():
        t = json.loads(text)
        for key, item in t["items"].items():
            index[f"{t['ti']}:{item['comp']}"] = key
    out["index"] = dict(sorted(index.items()))
    out["names"] = {
        key: [item.get("name", ""), item.get("rarity", "Legendary"), item.get("type", ""), item.get("maker", "")]
        for key, item in dex.items()
        if key in rolls["items"]
    }
    out["source_data"] = dict(sorted(prints.items()))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, help="path to an oo2core_*_win64.dll (reads the inv NCS)")
    parser.add_argument("--inv-json", type=Path, help="a cached dump of the inv NCS entries instead")
    parser.add_argument("--game", type=Path, default=None, help="Borderlands 4 install folder")
    args = parser.parse_args()
    import gen_item_cards as cards  # noqa: PLC0415

    if args.inv_json:
        entries = json.loads(args.inv_json.read_text(encoding="utf-8"))
        # A dump has no file hashes: take the dex's (made from the same inv files)
        dex_prints = json.loads(DEX.read_text(encoding="utf-8")).get("source_data") or {}
        prints = {k: v for k, v in dex_prints.items() if "/Nexus-Data-inv" in k and "inv_name_part" not in k}
    elif args.oodle:
        paks = sorted(((args.game or cards.DEFAULT_GAME) / "OakGame" / "Content" / "Paks").glob("*.pak"))
        entries, prints = cards._entries(paks, "inv", args.oodle)  # noqa: SLF001
    else:
        parser.error("give --oodle or --inv-json")
    data = build(entries, prints)
    OUT.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    pearls = sum(1 for n in data["names"].values() if n[1].lower().startswith("pearl"))
    print(f"{len(data['index'])} items ({pearls} pearls), {len(data['types'])} item types -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
