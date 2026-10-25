"""
Builds mods/bl4_reroll/parts.json (dev only, needs Oodle) from the installed game's data:

    python tools/gen_reroll_parts.py --oodle <path to an oo2core_*_win64.dll>

Item serials list parts by index. Class mods and enhancements keep their bottom stat lines in a
shared table (234 for class mods, 247 for enhancements) and write them as {234:[a b c]}; the item's
own type table (254 = Siren class mod, 286 = COV enhancement, ...) holds its rarity, body, skills
and manufacturer perks. This collects, from the `inv` records:

- per shared table, every stat part (stat_group1/2/3) with the tags the game uses to keep two
  stats from being the same thing, its minimum item level, and its name from `ui_stat`
- per shared table, its firmware parts (they share the list with the stats but aren't rerolled)
- per item type, which of its rarities (`inv_comp`) pin a stat (legendary class mods always roll
  their special stat), so a reroll leaves those alone
- per class mod type, its skill parts (`passive_points`): the skill tree node and points each
  gives, as "<graph>,<node>" like the item card's skill entries, so the mod can tell which of
  several class mods with the same stats a card shows

Also writes the SHA1s of the data files it read, so the mod can tell when a game patch changed them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_reroll" / "parts.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_item_cards as cards  # noqa: E402
import ncs_parse  # noqa: E402

# Shared tables we reroll in, by the record that owns them
BASES = {"classmod": "classmod", "enhancement": "enhancement"}
BASE_TYPES = {"inv'classmod'": "classmod", "inv'enhancement'": "enhancement"}
STAT_GROUPS = ("stat_group1", "stat_group2", "stat_group3")
RARITY = re.compile(r"comp_0(\d)_(common|uncommon|rare|epic|legendary)")
_REF = re.compile(r"^\w+'(.*)'$")
_TAGS = re.compile(r"\{[^}]*\}|\[/?\w+\]")
_TRAILER = re.compile(r"\s+is (?:increased|decreased|reduced) by$")


def tag_names(value: object) -> list[str]:
    """[{"tag": "tag"}, ...] -> ["tag", ...]."""
    if not isinstance(value, list):
        return []
    return sorted({k.lower() for item in value if isinstance(item, dict) for k in item})


def min_stage(value: dict) -> int:
    stage = value.get("mingamestage")
    if isinstance(stage, dict) and "constant" in stage:
        return int(float(stage["constant"]))
    return 0


def deref(value: str) -> str:
    match = _REF.match(value)
    return match.group(1) if match else value


def merged(entries: list[dict]) -> dict[str, dict]:
    """Records by lowercase key, with the deps of every record sharing that key (DLC chunks add some)."""
    out: dict[str, dict] = {}
    for item in entries:
        key = item["key"].lower()
        rec = out.setdefault(key, {"value": None, "deps": []})
        if isinstance(item.get("value"), dict) and rec["value"] is None:
            rec["value"] = item["value"]
        rec["deps"] += item.get("deps") or []
    return out


def serial_index(value: dict) -> int | None:
    index = (value.get("serialindex") or {}).get("index")
    return int(index) if index is not None else None


def stat_name(value: dict, ui_stats: dict[str, dict]) -> str:
    for aspect in value.get("aspects") or []:
        for ref in aspect.get("uistatstoinclude") or []:
            stat = ui_stats.get(deref(ref).lower())
            text = ((stat or {}).get("statvalue") or {}).get("formattext")
            if text:
                # "{damage} [secondary]Critical Hit Damage[/secondary]", or for enhancements
                # "[secondary]Gun Magazine Size[/secondary] is increased by [secondary]{mod}[/secondary]"
                text = " ".join(_TAGS.sub("", ncs_parse.loc_text(text)).split())
                return _TRAILER.sub("", text)
    return ""


def build(paks: list[Path], oodle: Path) -> tuple[dict, dict[str, str]]:
    inv_entries, prints = cards._entries(paks, "inv", oodle)
    stat_entries, stat_prints = cards._entries(paks, "ui_stat", oodle)
    prints.update(stat_prints)
    ui_stats = {e["key"].lower(): e["value"] for e in stat_entries if isinstance(e.get("value"), dict)}
    records = merged(inv_entries)

    bases: dict[str, dict] = {}
    base_index: dict[str, int] = {}
    for kind, key in BASES.items():
        rec = records[key]
        index = serial_index(rec["value"])
        base_index[kind] = index
        stats: dict[str, dict] = {}
        firmware: list[int] = []
        for dep in rec["deps"]:
            value = dep["value"]
            part = serial_index(value)
            if part is None:
                continue
            if dep["table"] == "firmware":
                firmware.append(part)
            elif dep["table"] in STAT_GROUPS:
                stats[str(part)] = {
                    "key": dep["key"],
                    "group": dep["table"],
                    "name": stat_name(value, ui_stats) or dep["key"],
                    "add": tag_names(value.get("addtags")),
                    "excl": tag_names(value.get("exclusiontags")),
                    # Only on items whose rarity asks for it (legendary specials)
                    "special": bool(value.get("dependencytags")),
                    "min_level": min_stage(value),
                }
        bases[str(index)] = {"kind": kind, "stats": dict(sorted(stats.items(), key=lambda kv: int(kv[0]))),
                             "firmware": sorted(firmware)}  # fmt: skip

    types: dict[str, dict] = {}
    for key, rec in records.items():
        value = rec["value"]
        if not value or (value.get("serialindex") or {}).get("_scope") != "Root":
            continue
        kind = BASE_TYPES.get(str(value.get("basetype", "")).lower())
        if kind is None:
            continue
        comps: dict[str, dict] = {}
        skills: dict[str, list] = {}
        for dep in rec["deps"]:
            if dep["table"] == "passive_points" and (part := serial_index(dep["value"])) is not None:
                for aspect in dep["value"].get("aspects") or []:
                    for passive in aspect.get("passives") or []:
                        node = f"{deref(str(passive.get('progressgraph', '')))},{passive.get('nodename', '')}"
                        skills.setdefault(str(part), []).append([node, int(aspect.get("points") or 0)])
                continue
            if dep["table"] != "inv_comp" or (part := serial_index(dep["value"])) is None:
                continue
            match = RARITY.search(dep["key"].lower())
            pinned: dict[str, list[str]] = {}
            rules = ((dep["value"].get("parttypeselectionrules") or {}).get("pairs") or {}).values()
            for rule in rules:
                group = str(rule.get("key", "")).lower()
                chosen = [p["part"].lower() for p in ((rule.get("value") or {}).get("parts") or []) if "part" in p]
                if group in STAT_GROUPS and chosen:
                    pinned[group] = chosen
            comps[str(part)] = {"key": dep["key"], "rarity": match.group(2) if match else "", "pinned": pinned}
        label = ncs_parse.loc_text(str(value.get("itembasename", ""))) or key
        types[str(serial_index(value))] = {
            "key": key,
            "kind": kind,
            "base": base_index[kind],
            "label": label,
            "manufacturer": deref(str(value.get("manufacturer", ""))),
            "comps": dict(sorted(comps.items(), key=lambda kv: int(kv[0]))),
            "skills": dict(sorted(skills.items(), key=lambda kv: int(kv[0]))),
        }

    data = {"bases": bases, "types": dict(sorted(types.items(), key=lambda kv: int(kv[0])))}
    return data, dict(sorted(prints.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=cards.DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    data, prints = build(paks, args.oodle)
    data["source_data"] = prints
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    for index, base in data["bases"].items():
        print(f"table {index} ({base['kind']}): {len(base['stats'])} stats, {len(base['firmware'])} firmware")
    print(f"item types: {len(data['types'])}")


if __name__ == "__main__":
    main()
