"""
Writes mods/bl4_weapondex/rolls.json: how the game rolls each dex item's parts, so the mod can roll
one itself and build its serial (mods/bl4_weapondex/serialgen.py) instead of dropping a pickup.

    python tools/gen_weapondex_rolls.py --oodle <oo2core_*_win64.dll>     (reads the inv NCS)
    python tools/gen_weapondex_rolls.py --inv-json <inv entries dump>    (dev: a cached dump)

gen_weapondex.py also writes it (same inv data as dex.json, so dex.json's source SHA1s cover it).

The rules, as read from the inv NCS and checked against 393 real legendary serials from the saves
(dev/re/serialgen/, mods/bl4_weapondex/CLAUDE.md "Rolling our own serials"):

- Slots: the item type's `parttypes` (nearest type in its `basetype` chain that lists them), in
  serial order. Candidates for a slot: that table's parts in the type and every ancestor.
- Rules: the composition, then its `basecomposition` chain; per slot the most derived rule wins
  whole. `parts` allowlist ([""] = nothing), `partcount` min (1) / max (max(min, 1)),
  `additionalpartchance`.
- Tags: the chain's `basetags` + each pick's `addtags`; a part needs all its `dependencytags` and
  none of its `exclusiontags`. `parttagselectionrules` cap the parts carrying a tag.
- Weights (`weight`) and level gates (`mingamestage`) are values: a data table cell (else the
  constant, its fallback) x an attribute x a post scale (serialgen.product). Attributes and cells
  are read from the game at roll time; a plain constant is stored as a number.

A part index is a number, or text for a part with no serial index (written as a text token).

Output (tags are indices into "tags"):
    {"tags": [...],
     "values": [{"c", "a", "t": [table, row, column], "p"}, ...],
     "tables": {owner type: {"ti": its serial index, "parts": {parttype: [[part index, add, dep, exc,
                                                                          weight, gate], ...]}}},
     "types": {inv: {"ti": serial index, "chain": [owner types, the type first], "slots": [parttypes]}},
     "items": {"<inv>.<comp>": {"comp": index, "tags": [...], "caps": [[tags, max], ...],
                                "rules": {parttype: [min, max, chance, allow]}}}}
On disk (pack()) each table and each type (with its items under "items") is its own JSON string,
so the mod parses only the ones a roll needs (~0.4 ms; the whole file at once held the GIL ~18 ms).
A slot's candidates are its parts in each table of the type's chain, in that order. Trailing
default fields of a part are left out (no tags, weight 1, gate 0). weight / gate / chance: a float
is a constant, an int is an index into "values" (the product of the pieces present: constant,
attribute, table cell, post scale); allow: candidate positions, or null for any.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_weapondex" / "rolls.json"
DEX = REPO / "mods" / "bl4_weapondex" / "dex.json"

_REF = re.compile(r"inv'([^']+)'")
_QUOTED = re.compile(r"'([^']*)'")


def _tags(entries: Any) -> set[str]:
    return {k.lower() for d in entries or [] if isinstance(d, dict) for k in d}


def _merge(a: dict, b: dict | None) -> dict:
    """A later record patches an earlier one; empty values don't remove anything here."""
    out = dict(a)
    for k, v in (b or {}).items():
        if v not in (None, "", [], {}):
            out[k] = v
    return out


class Inv:
    def __init__(self, entries: list[dict]) -> None:
        self.root: dict[str, dict] = {}
        self.parts: dict[str, dict[str, dict]] = defaultdict(dict)
        self.comps: dict[str, dict[str, dict]] = defaultdict(dict)
        self.names: dict[tuple[str, str], str] = {}  # (type, part) -> the part's name as the data spells it
        for e in entries:
            key = e["key"].lower()
            if e.get("value"):
                self.root[key] = _merge(self.root.get(key, {}), e["value"])
            for d in e.get("deps") or []:
                name = d["key"].lower()
                self.names.setdefault((key, name), d["key"])
                bucket = self.comps if d["table"] == "inv_comp" else self.parts
                old = bucket[key].get(name, {"table": d["table"], "value": {}})
                bucket[key][name] = {"table": d["table"], "value": _merge(old["value"], d["value"] if isinstance(d["value"], dict) else {})}

    def index(self, key: str) -> int | None:
        si = self.root.get(key, {}).get("serialindex") or {}
        return int(si["index"]) if si.get("_scope") == "Root" and si.get("index") is not None else None

    def chain(self, key: str) -> list[str]:
        out: list[str] = []
        while key and key in self.root and key not in out:
            out.append(key)
            m = _REF.match(self.root[key].get("basetype") or "")
            key = m.group(1).lower() if m else ""
        return out

    def parttypes(self, key: str) -> list[str]:
        for k in self.chain(key):
            if self.root[k].get("parttypes"):
                return [p.lower() for p in self.root[k]["parttypes"]]
        return []

    def comp_chain(self, key: str, comp: str) -> list[tuple[str, str, dict]]:
        """[(owner type, comp, record)]: the comp first, then its basecompositions."""
        out = []
        owner = next((k for k in self.chain(key) if comp in self.comps[k]), None)
        while owner and len(out) < 16:
            rec = self.comps[owner][comp]["value"]
            out.append((owner, comp, rec))
            m = _REF.match(rec.get("basecomposition") or "")
            if not m:
                break
            t, _, c = m.group(1).partition(".")
            owner, comp = t.lower(), c.lower()
            if comp not in self.comps.get(owner, {}):
                break
        return out

    def rules(self, key: str, comp: str) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for _, _, rec in reversed(self.comp_chain(key, comp)):
            for pair in ((rec.get("parttypeselectionrules") or {}).get("pairs") or {}).values():
                merged[pair["key"].lower()] = pair.get("value") or {}  # the most derived rule wins whole
        return merged

    def base_tags(self, key: str, comp: str) -> set[str]:
        return set().union(*(_tags(rec.get("basetags")) for _, _, rec in self.comp_chain(key, comp)))

    def caps(self, key: str, comp: str) -> list[tuple[set[str], int]]:
        """The nearest comp in the chain with tag rules ([""] there = none)."""
        for _, _, rec in self.comp_chain(key, comp):
            if "parttagselectionrules" in rec:
                return [(_tags(r.get("tags")), int(r.get("max", 99))) for r in rec["parttagselectionrules"] if isinstance(r, dict)]
        return []

    def candidates(self, key: str, parttype: str) -> list[tuple[str, str, dict]]:
        return [(k, n, p["value"]) for k in self.chain(key) for n, p in self.parts[k].items() if p["table"].lower() == parttype]


def _value(spec: Any, default: float) -> Any:
    """A weight / gate / chance: a number when it's a plain constant, else its pieces."""
    if not isinstance(spec, dict):
        try:
            return float(spec)
        except (TypeError, ValueError):
            return default
    out: dict[str, Any] = {}
    if "constant" in spec:
        out["c"] = float(spec["constant"])
    if m := _QUOTED.search(spec.get("attribute") or ""):
        out["a"] = m.group(1)
    table = spec.get("datatablevalue") or {}
    # A cell needs a row and a column (stat2_skill_duration's gate names no column: its constant applies)
    if (m := _QUOTED.search(table.get("datatable") or "")) and m.group(1).lower() != "none" and table.get("rowname") and table.get("columnname"):
        out["t"] = [m.group(1), table["rowname"], table["columnname"]]
    if "postscale" in spec:
        out["p"] = float(spec["postscale"])
    if set(out) <= {"c"}:
        return out.get("c", default)
    return out


def build_rolls(entries: list[dict], item_keys: list[str]) -> dict[str, Any]:
    inv = Inv(entries)
    tag_ids: dict[str, int] = {}

    def tags(names: set[str]) -> list[int]:
        return sorted(tag_ids.setdefault(t, len(tag_ids)) for t in names)

    values: dict[str, int] = {}

    def value(spec: Any, default: float) -> float | int:
        found = _value(spec, default)
        if isinstance(found, dict):
            return values.setdefault(json.dumps(found, sort_keys=True), len(values))
        return float(found)

    tables: dict[str, Any] = {}
    types: dict[str, Any] = {}
    items: dict[str, Any] = {}
    skipped: list[str] = []

    def table(owner: str, pt: str) -> list[str]:
        """Adds an owner type's parts of one part type to "tables"; returns their names."""
        entry = tables.setdefault(owner, {"ti": inv.index(owner), "parts": {}, "names": {}})
        if pt not in entry["parts"]:
            parts, names = [], []
            for name, p in inv.parts[owner].items():
                rec = p["value"]
                si = (rec.get("serialindex") or {}).get("index")
                if p["table"].lower() != pt or entry["ti"] is None:
                    continue
                # A part with no serial index (4 class mod special stats) goes into the serial as
                # text, "<type>.<part>" (ClassMod.statspecial_ricochet_damage), as the game writes it
                index = int(si) if si is not None else f"{inv.root[owner].get('inv', owner)}.{inv.names[(owner, name)]}"
                part = [index, tags(_tags(rec.get("addtags"))), tags(_tags(rec.get("dependencytags"))),
                        tags(_tags(rec.get("exclusiontags"))), value(rec.get("weight"), 1.0), value(rec.get("mingamestage"), 0.0)]
                while len(part) > 1 and repr(part[-1]) == repr([None, [], [], [], 1.0, 0.0][len(part) - 1]):
                    part.pop()  # (repr: an index 1 must not pass for the weight 1.0)
                parts.append(part)
                names.append(name)
            entry["parts"][pt], entry["names"][pt] = parts, names
        return entry["names"][pt]

    for item_key in item_keys:
        key, _, comp = item_key.lower().partition(".")
        chain = inv.comp_chain(key, comp)
        ti = inv.index(key)
        comp_index = (chain[0][2].get("serialindex") or {}).get("index") if chain else None
        if ti is None or comp_index is None:
            skipped.append(f"{item_key}: no serial index for its type or composition")
            continue
        owners = [k for k in inv.chain(key) if inv.index(k) is not None]
        if key not in types:
            types[key] = {"ti": ti, "chain": owners, "slots": inv.parttypes(key)}
        names = {pt: [n for owner in owners for n in table(owner, pt)] for pt in types[key]["slots"]}
        rules = {}
        for pt, rule in inv.rules(key, comp).items():
            if pt not in names:
                continue
            count = rule.get("partcount") or {}
            lo = int(count.get("min", 1))
            hi = int(count.get("max", max(lo, 1)))
            chance = value(rule["additionalpartchance"], 0.0) if rule.get("additionalpartchance") else None
            allow = None
            if "parts" in rule:
                wanted = {p["part"].lower() for p in rule["parts"] or [] if isinstance(p, dict) and p.get("part")}
                allow = [i for i, n in enumerate(names[pt]) if n in wanted]
            rules[pt] = [lo, hi, chance, allow]
        items[item_key] = {
            "comp": int(comp_index),
            "tags": tags(inv.base_tags(key, comp)),
            "caps": [[tags(t), m] for t, m in inv.caps(key, comp)],
            "rules": rules,
        }
    for line in skipped:
        print("  skipped", line)
    for entry in tables.values():
        del entry["names"]
    return {"tags": list(tag_ids), "values": [json.loads(v) for v in values], "tables": tables, "types": types, "items": items}


def pack(rolls: dict[str, Any]) -> str:
    """rolls.json's text: tables and types (each with its items) as JSON strings, parsed on demand."""
    compact = {"separators": (",", ":")}
    items: dict[str, dict[str, Any]] = {}
    for key, item in rolls["items"].items():
        items.setdefault(key.partition(".")[0], {})[key] = item
    return json.dumps({
        "tags": rolls["tags"],
        "values": rolls["values"],
        "tables": {k: json.dumps(v, **compact) for k, v in rolls["tables"].items()},
        "types": {k: json.dumps({**v, "items": items.get(k, {})}, **compact) for k, v in rolls["types"].items()},
    }, **compact) + "\n"  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, help="path to an oo2core_*_win64.dll (reads the inv NCS)")
    parser.add_argument("--inv-json", type=Path, help="a cached dump of the inv NCS entries instead")
    parser.add_argument("--game", type=Path, default=None, help="Borderlands 4 install folder")
    args = parser.parse_args()
    if args.inv_json:
        entries = json.loads(args.inv_json.read_text(encoding="utf-8"))
    elif args.oodle:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gen_item_cards as G  # noqa: PLC0415

        paks = sorted(((args.game or G.DEFAULT_GAME) / "OakGame" / "Content" / "Paks").glob("*.pak"))
        entries, _ = G._entries(paks, "inv", args.oodle)  # noqa: SLF001
    else:
        parser.error("give --oodle or --inv-json")
    keys = list(json.loads(DEX.read_text(encoding="utf-8"))["items"])
    rolls = build_rolls(entries, keys)
    OUT.write_text(pack(rolls), encoding="utf-8")
    print(f"{len(rolls['items'])} items, {len(rolls['types'])} item types -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
