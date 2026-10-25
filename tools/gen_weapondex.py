"""
Writes mods/bl4_weapondex/dex.json for the Weapondex mod (dev only).

    python tools/gen_weapondex.py --oodle <path to an oo2core_*_win64.dll>

Everything comes from the game's NCS data (see gen_drops.py for how drop tables and chances work):

- Bosses: the rows of the `*DedicatedDrops` tables. One boss usually has several rows (the
  story fight, a Big Encore replay, the "True" Big Encore), named like `Grotto_Cat`,
  `Grotto_Cat_Replay`, `Grotto_Cat_Replay_TRUE`, so rows are grouped by that base name and by the
  name the boss shows. Each boss lists the actor defs (`Char_...`) of its rows: a dying enemy is
  recognised by its def, which is also its object name in game.
- Where: the map's boss fight markers (`gbx_discovery_location_meta_data` of type
  `DiscoveryLocation_Activity_BossFight`) name the fight and its map. Open world markers are
  named after their region (`UILocation_BossFight_GRA06` is in the Fadefields). Bosses without a
  marker fall back to their table (a DLC's own table) or the region in their row or def name.
- Drops: each row's ItemPoolList. A specific item counts, and so does a named pool of specific
  items, like the class mod pools (one legendary class mod per class: you get your class's).
- Items: every legendary and pearlescent composition with a name, its type and manufacturer
  from its inventory key (`jak_sg` = Jakobs shotgun, `classmod_dark_siren` = Siren class mod),
  whether it's also a world drop and whether a mission reward gives it.

Also records the SHA1s of the data files used ("source_data"); the mod says so when they change.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_drops  # noqa: E402
import ncs_parse  # noqa: E402
from gen_drops import Loader, _quoted, _walk_strings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_weapondex" / "dex.json"
STATS_OUT = REPO / "mods" / "bl4_weapondex" / "stats.json"

# The base game's open world regions, by the codes their map markers and defs use
REGION_CODES = {"gra": "grasslands", "grass": "grasslands", "grasslands": "grasslands", "mtn": "mountains",
                "mou": "mountains", "mount": "mountains", "mountain": "mountains", "mountains": "mountains",
                "sha": "shatteredlands", "shat": "shatteredlands", "shatterlands": "shatteredlands",
                "shatteredlands": "shatteredlands", "city": "city"}  # fmt: skip
BASE_REGIONS = ("grasslands", "mountains", "shatteredlands", "city")

# Inventory key -> what the item is. The game's own words where it has them (display_data keys).
WEAPON_TYPES = {"ps": "weapontype_pistol", "sm": "weapontype_smg", "ar": "weapontype_assaultrifle",
                "sg": "weapontype_shotgun", "sr": "weapontype_sniper", "hw": "weapontype_heavy"}  # fmt: skip
GEAR_TYPES = {"shield": "Shield", "grenade_gadget": "Grenade", "repair_kit": "Repkit",
              "turret_gadget": "Turret", "enhancement": "Enhancement"}  # fmt: skip
# The order types sort in, and which tab of filters each falls under
TYPE_ORDER = ["Pistol", "SMG", "Assault Rifle", "Shotgun", "Sniper Rifle", "Heavy", "Shield", "Grenade",
              "Repkit", "Enhancement", "Class Mod"]  # fmt: skip
MAKERS = {"bor": "borg", "borg": "borg", "dad": "daedalus", "jak": "jakobs", "mal": "maliwan", "ord": "order",
          "ted": "tediore", "tor": "torgue", "vla": "vladof", "atl": "atlas", "cov": "cov", "hyp": "hyperion"}  # fmt: skip

# Bosses fought together under a fight name the data doesn't link to them: row name prefix (lowercase,
# no underscores) -> the fight marker's name
FIGHT_ALIASES = {"hovercart": "Rippa Roadbirds", "creepexperiment": "Vile Ted & The Experiments",
                 "testzero": "The Reject Prototypes", "ordonitemeathead": "Sturmdrang", "ordonitesplice": "Sturmdrang", "raid2uber": "Subjugator & Thol",
                 "lockdowntedioreframe": "Jayson and his Frame", "citytimekeeperguardian": "The Timekeeper",
                 "murderer": "Biff Fistful"}  # fmt: skip
# What kind of fight a boss is, from words in its row and def names (first match wins). Only the
# unambiguous ones: story and side bosses don't say so reliably.
KINDS = (("bossrift", "Rift"), ("raid", "Raid"), ("mineboss", "Auger Mine"), ("bunkerboss", "Order Bunker"),
         ("drillboss", "Drill Site"), ("drillsite", "Drill Site"), ("fracking", "Drill Site"),
         ("hovercart", "Drill Site"), ("meatheadrider", "Drill Site"), ("meatheadjockey", "Drill Site"),
         ("grassguard", "Vault Guardian"), ("mountguard", "Vault Guardian"), ("shatterguard", "Vault Guardian"),
         ("tkguard", "Vault Guardian"))  # fmt: skip

_ROW_SUFFIX = re.compile(r"(_?trueboss|_?true|_replay|_mission|_runnable)$", re.IGNORECASE)
_ENCORE_ROW = re.compile(r"true", re.IGNORECASE)
_WORDS = re.compile(r"[a-z0-9]+")
# Pools that roll random gear rather than particular items
_COLUMN_SUFFIX = re.compile(r"_\d+_[0-9A-Fa-f]{32}$")
_GENERIC_POOL = re.compile(r"_all$|money|cash|health|ammo|currency|rare_guns|^none$", re.IGNORECASE)


def _words(text: str) -> list[str]:
    return _WORDS.findall(text.lower().replace("'", ""))


def _slug(text: str) -> str:
    return "-".join(_words(text)) or "boss"


def _contains(haystack: list[str], needle: list[str]) -> bool:
    """needle appears in haystack as consecutive whole words."""
    n = len(needle)
    return n > 0 and any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _comps_and_parts(inv_entries: list[dict]) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    """
    ({"<inv>.<comp>": composition record}, {inv: {part: part record}}), all keys lowercased. An item
    type's records can sit in several files (base game, patches, DLC): later ones patch earlier ones.
    """
    comps: dict[str, dict] = {}
    parts: dict[str, dict[str, dict]] = defaultdict(dict)
    for item in inv_entries:
        inv_key = item["key"].lower()
        for dep in item.get("deps") or []:
            value = dep.get("value")
            if not isinstance(value, dict):
                continue
            key = dep["key"].lower()
            if dep.get("table") == "inv_comp":
                comps[f"{inv_key}.{key}"] = {**comps.get(f"{inv_key}.{key}", {}), **value}
            else:
                parts[inv_key][key] = {**parts[inv_key].get(key, {}), **value}
    return comps, parts


def _markup(text: str) -> str:
    return " ".join(ncs_parse.loc_text(text).split())


def _item_texts(comp: dict, parts: dict[str, dict], ui_stats: dict[str, dict], static: Any = None) -> dict[str, Any]:
    """
    The texts a card shows for an item whatever it rolls: its red (flavor) text and its unique
    effect ("desc"). They're UIStatAspects on the composition or on its unique parts (the parts its
    selection rules always pick: a barrel, a shield's unique part, ...).

    Effect texts can hold numbers ("dealing {dmg} Damage"). Ones `static(attribute)` knows (constants,
    data table cells) are filled in here; the rest go in "args" ({name: number spec}) for a roll to
    fill (roll.py), and the page drops what's still unfilled.
    """
    rules = (comp.get("parttypeselectionrules") or {}).get("pairs") or {}
    sources = [comp]
    for pair in rules.values():
        options = [p.get("part", "").lower() for p in ((pair.get("value") or {}).get("parts") or []) if isinstance(p, dict)]
        # Only a slot with one choice is the item's own; a pick from many is a random roll
        if len(options) == 1 and options[0] in parts:
            sources.append(parts[options[0]])
    red: list[str] = []
    effects: list[str] = []
    args: dict[str, Any] = {}
    variants: list[dict[str, Any]] = []
    for source in sources:
        for aspect in source.get("aspects") or []:
            if not isinstance(aspect, dict) or "UIStatAspect" not in str(aspect.get("structtype")):
                continue
            for ref in aspect.get("uistatstoinclude") or []:
                stat = ui_stats.get((_quoted(ref) or "").lower()) or {}
                value = stat.get("statvalue") or {}
                if "StringDisplayValue" not in str(value.get("structtype")) or not value.get("formattext"):
                    continue
                text = _markup(value["formattext"])
                for pair in ((value.get("argsmap") or {}).get("pairs") or {}).values():
                    arg = pair.get("value") or {}
                    if "NumericDisplayValue" not in str(arg.get("structtype")) or "{" + pair["key"] + "}" not in text:
                        continue
                    spec = _number_spec(arg)
                    known = static(spec["attr"]) if static and spec["attr"] else None
                    if known is not None:
                        text = text.replace("{" + pair["key"] + "}", _format_number(known, spec))
                    else:
                        args[pair["key"]] = spec
                group = (_quoted(stat.get("displaygroup")) or "").lower()
                cond = stat.get("displaycondition")
                if cond and group != "redtext":
                    # One of several texts the card picks by the rolled parts (Tediore throw types,
                    # licensed parts): shown only when a roll passes its condition
                    when = _display_condition(cond)
                    if text and not any(v["text"] == text for v in variants):
                        variants.append({"text": text, "if": when})
                    continue
                target = red if group == "redtext" else effects
                if text and text not in target:
                    target.append(text)
    out: dict[str, Any] = {}
    if variants:
        out["variants"] = variants
    if red:
        out["red"] = red
    if effects:
        out["effects"] = effects
    if args:
        out["args"] = args
    return out


def _display_condition(cond: dict[str, Any]) -> dict[str, Any]:
    """A ui_stat displaycondition. No compare type means Equal; defaultvalue false: false when the
    attribute isn't there."""
    out: dict[str, Any] = {
        "attr": (_quoted(cond.get("attributedef")) or "").lower(),
        "cmp": cond.get("comparetype", "Equal"),
        "value": float(cond.get("comparevalue") or 0),
    }
    if cond.get("defaultvalue") == "false":
        out["missing"] = False
    return out


# Attribute contexts that read the item itself (its stats container, parts or augments): such a
# value differs per roll. Anything else depends on the player/level only.
_ITEM_CONTEXTS = ("WeaponAttributeContextResolver", "InventoryStatsContainerContextResolver", "InventoryAugmentAttributeContextResolver")
_ITEM_VALUES = ("PropertyValueResolver", "InventoryStatsContainerValueResolver", "WeaponPartValueResolver", "InventoryAugmentAttributeValueResolver")


def _item_bound(record: dict[str, Any]) -> bool:
    context = str((record.get("context") or {}).get("structtype", ""))
    value = record.get("value")
    kind = str(value.get("structtype", "")) if isinstance(value, dict) else ""
    return any(c in context for c in _ITEM_CONTEXTS) or any(v in kind for v in _ITEM_VALUES)


def _format_number(value: float, spec: dict[str, Any]) -> str:
    """A number the way the card shows it (same rules as bl4_weapondex/stats.py)."""
    if spec.get("percent"):
        value = float(round(value * 100))
    precision = 0 if spec.get("percent") else spec.get("precision", 0)
    if spec.get("round") == "RoundToInt" or precision == 0:
        value = float(math.floor(value + 0.5))
        precision = 0
    text = f"{value:,.{precision}f}" + ("%" if spec.get("percent") else "")
    if spec.get("plus") and value > 0:
        text = "+" + text
    return spec["format"].replace("$VALUE$", text) if spec.get("format") else text


class _Groups:
    """Union-find over row indexes."""

    def __init__(self, count: int) -> None:
        self.parent = list(range(count))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def build_dex(load: Loader) -> dict[str, Any]:
    loot = gen_drops.Loot(load)
    texts = loot.texts

    def text(key: str) -> str:
        return texts.get(key.lower(), "")

    # region Items

    names = loot.item_names(lambda comp: comp.startswith(("comp_05", "comp_06")))
    world = loot.world_items()
    rewarded: set[str] = set()
    for entry in loot.data.entries("rewards"):
        for s in _walk_strings(entry.get("value")):
            if s.startswith("inv'") and (item := _quoted(s)):
                rewarded.add(item.lower())
    makers = {k: ncs_parse.loc_text(v["displayname"]) for k, v in loot.data.records("Manufacturer").items() if "displayname" in v}
    comps, parts = _comps_and_parts(loot.data.entries("inv"))
    ui_stats = loot.data.records("ui_stat")

    def static_value(attr: str) -> float | None:
        """An attribute that's the same on every roll: a constant or a data table cell (else None)."""
        value = (loot.attributes.get(attr) or {}).get("value")
        kind = str(value.get("structtype", "")) if isinstance(value, dict) else ""
        if "GbxDataTableValueResolver" in kind:
            cell = value.get("datatablevalue") or {}
        elif "GbxConstantAttributeValueResolver" in kind:
            init = value.get("attributeinit") or {}
            cell = init.get("datatablevalue") or {}
            if (_quoted(cell.get("datatable")) or "none").lower() == "none":
                try:
                    return float(init.get("constant", 0))
                except (TypeError, ValueError):
                    return None
        else:
            return None
        table = (_quoted(cell.get("datatable")) or "").lower()
        if not table or table == "none" or not cell.get("rowname"):
            return None
        # Columns are named like Value_2_70_<GUID>; the row holds them as value_2
        column = _COLUMN_SUFFIX.sub("", cell.get("columnname") or "").lower()
        for row in (loot.tables.get(table) or {}).get("data") or []:
            if row.get("row_name") == cell["rowname"]:
                found = (row.get("row_value") or {}).get(column)
                try:
                    return float(found) if found is not None else None
                except (TypeError, ValueError):
                    return None
        return None

    # Item type -> its serial index (a serial's first number): with the composition's, it tells which
    # legendary a serial is (the "obtained" check reads the saves' serials, bl4_weapondex/owned.py)
    type_index: dict[str, int] = {}
    for entry in loot.data.entries("inv"):
        value = entry.get("value")
        found = (value.get("serialindex") or {}).get("index") if isinstance(value, dict) else None
        if found is not None:
            type_index.setdefault(entry["key"].lower(), int(found))

    def describe(item: str) -> dict[str, Any] | None:
        inv, _, comp = item.partition(".")
        if not names.get(item):
            return None
        if inv.endswith("turret_gadget"):
            return None  # the one legendary turret (Anchor) isn't something players get
        out: dict[str, Any] = {"name": " / ".join(sorted(names[item]))}
        if inv.startswith("classmod_"):
            cls = inv.removeprefix("classmod_").replace("_", "")
            out["type"] = "Class Mod"
            out["class"] = text(cls) or cls.title()
            if hero := text(f"{cls}narrativename"):
                out["hero"] = hero
        else:
            maker, _, kind = inv.partition("_")
            if kind in WEAPON_TYPES:
                out["type"] = text(WEAPON_TYPES[kind]) or kind.upper()
            else:
                out["type"] = GEAR_TYPES.get(kind, kind.replace("_", " ").title())
            if maker in MAKERS:
                out["maker"] = makers.get(MAKERS[maker], MAKERS[maker].title())
        record = comps.get(item) or {}
        # The composition's own rarity wins: some comp_05 items are pearls (Abyss)
        rarity = (_quoted(record.get("rarity")) or "").lower()
        out["rarity"] = "Pearlescent" if "pearl" in rarity or comp.startswith("comp_06") else "Legendary"
        # The composition's serial index: a rolled drop's identity points at its composition, so the
        # roll can check it got this item (bl4_weapondex/roll.py)
        index = (record.get("serialindex") or {}).get("index")
        if index is not None:
            out["si"] = int(index)
        if inv in type_index:
            out["ti"] = type_index[inv]
        out.update(_item_texts(record, parts.get(inv, {}), ui_stats, static_value))
        for spec in (out.get("args") or {}).values():
            if not _item_bound(loot.attributes.get(spec["attr"]) or {}):
                # The same for every roll at a level (balance formulas, ...): the game evaluates it
                # against the player (roll.level_fills)
                spec["game"] = True
        if item in world:
            out["world"] = True
        if item in rewarded:
            out["reward"] = True
        return out

    items: dict[str, dict[str, Any]] = {}
    for item in names:
        if (info := describe(item)) is not None:
            items[item] = info

    pools = loot.data.records("itempool")

    def pool_items(pool: str) -> list[str]:
        """The specific items a named pool rolls from, if that's all it holds."""
        if _GENERIC_POOL.search(pool) or "shiny" in pool.lower():
            return []
        record = pools.get(pool.lower()) or {}
        found = [i.lower() for s in _walk_strings(record.get("items")) if s.startswith("inv'") and (i := _quoted(s))]
        return found if found and all(i in items for i in found) else []

    # endregion
    # region Boss fight markers

    def place(world_map: str) -> tuple[str, str]:
        """(area, region) for a map: Fortress_Grasslands_P -> (Fortress Indomita, The Fadefields)."""
        base = world_map.removesuffix("_P").lower()
        key = next((k for k in (f"uiregiondisplaydata_{base}", *sorted(texts)) if k.startswith("uiregiondisplaydata_")
                    and (k == f"uiregiondisplaydata_{base}" or k.endswith("_" + base)) and text(k)), None)  # fmt: skip
        if key is None:
            # Fortress_Grasslands -> grasslands_fortress, UpperCity -> city_upper, Mandolin1 -> mandolin
            plain = base.replace("_", "").rstrip("0123456789")
            tries = ["_".join(reversed(base.split("_"))), plain]
            tries += [f"{plain[i:]}_{plain[:i]}" for i in range(1, len(plain))]
            key = next((f"uiregiondisplaydata_{t}" for t in tries if text(f"uiregiondisplaydata_{t}")), None)
        if key is None:
            pretty = gen_drops._pretty_map(world_map)  # noqa: SLF001
            return pretty, pretty
        code = key.removeprefix("uiregiondisplaydata_")
        top = code.split("_")[0]
        for word in base.split("_"):
            if REGION_CODES.get(word) in BASE_REGIONS:
                top = REGION_CODES[word]
        return texts[key], text(f"uiregiondisplaydata_{top}") or texts[key]

    fights: list[dict[str, Any]] = []
    for key, record in loot.data.records("gbx_discovery_location_meta_data").items():
        if "BossFight" not in str(record.get("locationtype")):
            continue
        display = _quoted(record.get("locationdisplaydata")) or ""
        name = text(display)
        world_map = record.get("worldmapname") or ""
        if not name or not world_map:
            continue
        if world_map.lower() == "world_p":
            code = display.rsplit("_", 1)[-1].rstrip("0123456789").lower()  # UILocation_BossFight_GRA06
            region = text(f"uiregiondisplaydata_{REGION_CODES.get(code, code)}")
            area = region
        else:
            area, region = place(world_map)
        fights.append({"name": name, "words": _words(name), "area": area, "region": region, "location": key})

    # endregion
    # region Bosses

    rows: list[dict[str, Any]] = []
    for table, row_name, actor, pool_list in loot.dedicated_rows():
        name = loot.boss_name(actor) if actor else None
        if not name or not actor:
            continue
        name = " ".join(name.split())
        encore = bool(_ENCORE_ROW.search(row_name)) or bool(gen_drops._ENCORE_WORDS.search(name))  # noqa: SLF001
        base = row_name.lower()
        while (stripped := _ROW_SUFFIX.sub("", base)) != base:
            base = stripped
        base = base.replace("_", "")
        rows.append({
            "table": table.removeprefix("table_").removesuffix("dedicateddrops").strip("_"),
            "row": row_name,
            "base": base,
            "actor": actor,
            "display": loot.display_def(actor),
            "name": gen_drops._ENCORE_WORDS.sub("", name).strip(),  # noqa: SLF001
            "encore": encore,
            "pool_list": pool_list,
        })

    groups = _Groups(len(rows))
    first_by: dict[tuple[str, str], int] = {}
    for i, row in enumerate(rows):
        # The name a Big Encore row shows can be another boss's (a data slip), so those rows only
        # join by row name
        keys = [("base", row["base"])] + ([] if row["encore"] else [("name", row["name"].lower())])
        for key in keys:
            if key in first_by:
                groups.union(i, first_by[key])
            else:
                first_by[key] = i
    # A group of only Big Encore rows (its row name misspelt, e.g. Crazy_Early_TRUE) belongs to the
    # boss of the same name
    roots: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        roots[groups.find(i)].append(i)
    for indexes in roots.values():
        if all(rows[i]["encore"] for i in indexes) and ("name", rows[indexes[0]]["name"].lower()) in first_by:
            groups.union(indexes[0], first_by[("name", rows[indexes[0]]["name"].lower())])
    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, row in enumerate(rows):
        members[groups.find(i)].append(row)

    def boss_place(group: list[dict[str, Any]]) -> tuple[str, str, str | None]:
        """(area, region, fight marker) for a boss."""
        boss_words = [_words(r["name"]) for r in ([r for r in group if not r["encore"]] or group)]
        row_words = [_words(r["row"].replace("_", " ")) for r in group]
        aliases = {_words(fight).__str__() for r in group for prefix, fight in FIGHT_ALIASES.items() if r["base"].startswith(prefix)}
        best: tuple[int, dict[str, Any]] | None = None
        for fight in fights:
            score = 4 if str(fight["words"]) in aliases else 0
            for words in boss_words:
                if words == fight["words"]:
                    score = max(score, 3)
                elif _contains(fight["words"], words) or _contains(words, fight["words"]):
                    score = max(score, 2)
            if not score and any(_contains(words, fight["words"]) for words in row_words):
                score = 1
            if score and (best is None or score > best[0]):
                best = (score, fight)
        if best is not None:
            return best[1]["area"], best[1]["region"], best[1]["location"]
        # No marker: the DLC's own table, else a region named in the row or def
        for row in group:
            if row["table"] and (dlc := text(f"uiregiondisplaydata_{row['table']}")):
                return dlc, dlc, None
        for row in group:
            for word in _WORDS.findall((row["row"] + " " + row["actor"]).lower().replace("_", " ")):
                for code, region in REGION_CODES.items():
                    if code in word and (found := text(f"uiregiondisplaydata_{region}")):
                        return found, found, None
        return "Unknown", "Unknown", None

    bosses: list[dict[str, Any]] = []
    for group in members.values():
        plain = [r for r in group if not r["encore"]] or group
        name = max({r["name"] for r in plain}, key=lambda n: (sum(r["name"] == n for r in plain), -len(n)))
        drops: dict[str, dict[str, float]] = {}
        for row in group:
            entries = (loot.pool_lists.get((row["pool_list"] or "").lower()) or {}).get("itempools") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                refs = list(_walk_strings(entry.get("itempool")))
                found = [i.lower() for s in refs if s.startswith("inv'") and (i := _quoted(s))]
                for s in refs:
                    if s.startswith("itempool'") and (pool := _quoted(s)):
                        found.extend(pool_items(pool))
                found = [i for i in found if i in items]
                low, high = loot.chance(entry.get("probability"), 0), loot.chance(entry.get("probability"), 5)
                if not found or low is None:
                    continue
                for item in found:
                    drop = drops.setdefault(item, {})
                    if row["encore"]:
                        drop["encore"] = max(drop.get("encore", 0.0), round(low, 5))
                    else:
                        drop["uvh0"] = max(drop.get("uvh0", 0.0), round(low, 5))
                        drop["uvh5"] = max(drop.get("uvh5", 0.0), round(high if high is not None else low, 5))
        if not drops:
            continue
        area, region, fight = boss_place(group)
        boss: dict[str, Any] = {
            "name": name,
            "area": area,
            "region": region,
            "chars": sorted({r["actor"].lower() for r in group}),
            "names": sorted({r["display"].lower() for r in group if r["display"]}),
            "encore": any(r["encore"] for r in group),
            "drops": [{"item": item, **chances} for item, chances in drops.items()],
        }
        if fight:
            boss["fight"] = fight
        words = " ".join(r["row"] + " " + r["actor"] for r in group).lower().replace("_", "")
        if kind := next((k for token, k in KINDS if token in words), None):
            boss["kind"] = kind
        bosses.append(boss)

    # A UI name shared by two bosses (an inherited generic one) can't tell them apart
    owners: dict[str, int] = defaultdict(int)
    for boss in bosses:
        for display in boss["names"]:
            owners[display] += 1
    for boss in bosses:
        boss["names"] = [d for d in boss["names"] if owners[d] == 1]

    # Numbered like a dex: base game regions in their order, then everything else, by name
    region_rank = {text(f"uiregiondisplaydata_{r}"): i for i, r in enumerate(BASE_REGIONS)}
    bosses.sort(key=lambda b: (region_rank.get(b["region"], len(region_rank)), b["region"], b["area"], b["name"].lower()))
    used: set[str] = set()
    for number, boss in enumerate(bosses, 1):
        boss["no"] = number
        slug = _slug(boss["name"])
        while slug in used:
            slug += "-2"
        used.add(slug)
        boss["id"] = slug
        for drop in boss["drops"]:
            items[drop["item"]].setdefault("bosses", []).append(slug)
        boss["drops"].sort(key=lambda d: (TYPE_ORDER.index(items[d["item"]]["type"]) if items[d["item"]]["type"] in TYPE_ORDER else 99,
                                          items[d["item"]]["name"].lower()))  # fmt: skip

    # endregion

    regions = list(dict.fromkeys(b["region"] for b in bosses))
    types = [t for t in TYPE_ORDER if any(i["type"] == t for i in items.values())]
    types += sorted({i["type"] for i in items.values()} - set(types))
    return {
        "bosses": bosses,
        "items": dict(sorted(items.items(), key=lambda kv: kv[1]["name"].lower())),
        "regions": regions,
        "types": types,
        "makers": sorted({i["maker"] for i in items.values() if "maker" in i}),
        "source_data": dict(sorted(loot.data.fingerprints.items())),
    }


# region Card stats (stats.json)

# Card line groups worth showing (the rest are type lines, red text and per-part text)
STAT_GROUPS = {"headline", "primary", "secondary", "tertiary", "element"}
_ATTR_REF = re.compile(r"attribute'([A-Za-z0-9_]+)'")
_ATTR_CALL = re.compile(r"attr\(([A-Za-z0-9_]+)\)")
_SPEC_ATTR = re.compile(r'"attr": "([a-z0-9_]+)"')


def _value_spec(value: Any) -> dict[str, Any]:
    """An attribute value reference ({"attribute": ..} / {"constant": ..}) -> {"attr": name} / {"const": x}."""
    if isinstance(value, dict):
        if name := _quoted(value.get("attribute")):
            spec: dict[str, Any] = {"attr": name.lower()}
            if value.get("postscale"):
                spec["scale"] = float(value["postscale"])
            return spec
        if "constant" in value:
            try:
                return {"const": float(value["constant"])}
            except (TypeError, ValueError):
                pass
    return {"const": 0.0}


def _expr_spec(expression: Any) -> dict[str, Any] | None:
    """GbxExpressionValueResolver.expression: a formula string, or {formula, variables}."""
    if isinstance(expression, str):
        return {"formula": expression, "vars": {}}
    if not isinstance(expression, dict) or not isinstance(expression.get("formula"), str):
        return None
    variables: dict[str, Any] = {}
    pairs = ((expression.get("variables") or {}).get("variablevalues") or {}).get("pairs") or {}
    for pair in pairs.values():
        inner = (pair.get("value") or {}).get("value") or {}
        ref = _quoted(inner.get("value")) if inner.get("type") == "Attribute" else None
        if ref:
            variables[pair["key"]] = {"attr": ref.lower()}
        else:
            try:
                variables[pair["key"]] = {"const": float(inner.get("value"))}
            except (TypeError, ValueError):
                variables[pair["key"]] = {"const": 0.0}
    return {"formula": expression["formula"], "vars": variables}


def _condition_spec(condition: Any) -> dict[str, Any] | None:
    if not isinstance(condition, dict):
        return None
    if name := _quoted(condition.get("externalattribute")):
        return {"attr": name.lower()}
    inline = condition.get("inlinestruct") or {}
    if "ExpressionValueResolver" in str(inline.get("structtype")) and (expr := _expr_spec(inline.get("expression"))):
        return {"expr": expr}
    return None


def _attr_spec(record: dict[str, Any], tables: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    How to get an attribute's value from an item's stats: read it, or compute it from others.
    With `tables` (gear attributes), balance formulas are computed too.
    """
    value = record.get("value")
    kind = str(value.get("structtype", "")) if isinstance(value, dict) else ""
    if "GbxConditionalAttributeValueResolver" in kind:
        cases = []
        for case in value.get("conditionalvalues") or []:
            if (cond := _condition_spec(case.get("condition"))) is not None:
                cases.append([cond, _value_spec(case.get("value"))])
        return {"k": "cond", "default": _value_spec(value.get("defaultvalue")), "cases": cases}
    if "GbxExpressionValueResolver" in kind and (expr := _expr_spec(value.get("expression"))):
        return {"k": "expr", **expr}
    if "GbxCondition_WeaponPartValue" in kind:
        # 1 when the rolled part of that slot has this value (Reload 4: a heat weapon); the part
        # values come from the rolled parts (stats.json "parts")
        return {"k": "partis", "attr": f"weapon_part_{str(value.get('type', '')).lower()}_value", "v": float(value.get("value") or 0)}
    if "GbxConstantAttributeValueResolver" in kind:
        return {"k": "const", "v": _value_spec(value.get("value") or value).get("const", 0.0)}
    if "BalanceFormulaValueResolver" in kind and tables is not None:
        # multiplier x (level ^ power + offset); only met on an item's own formulas here (the
        # player's level formulas are evaluated by the game)
        return {"k": "balance", **{key: _piece(tables, value.get(name), empty)[:3]
                                   for key, name, empty in (("m", "multiplier", 1.0), ("l", "level", 1.0), ("p", "power", 1.0), ("o", "offset", 0.0))}}  # fmt: skip
    # Read from the item's stats container (PropertyValueResolver, InventoryStatsContainer...,
    # plain attributes). Anything else we can't compute reads too, and is 0 if it isn't there.
    return {"k": "stored"}


def _number_spec(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"attr": (_quoted(value.get("attributedef")) or "").lower()}
    if value.get("roundingmode"):
        out["round"] = value["roundingmode"]
    if value.get("floatprecision"):
        out["precision"] = int(value["floatprecision"])
    if value.get("bdisplayaspercentage") == "true":
        out["percent"] = True
    if value.get("bdisplayplussign") == "true":
        out["plus"] = True
    if value.get("signstyle") in ("Positive", "Negative"):
        out["sign"] = value["signstyle"]  # the card shows the size, with this sign
    if value.get("buseformattext") == "true" and value.get("formattext"):
        out["format"] = ncs_parse.loc_text(value["formattext"])
    return out


def build_stats(data: Any, item_keys: list[str], extra: set[str] | None = None, variant_attrs: set[str] | None = None,
                gear_attrs: set[str] | None = None) -> dict[str, Any]:
    """
    What the card shows for each item type and how to compute it from an item's stats container:
    {"lines": {inv: [ui_stat]}, "stats": {ui_stat: line}, "attrs": {attribute: spec}}.
    Item type roots list their lines (`uistats`) and inherit their base type's (`basetype`,
    unless `binheritbaseuistats` is false).
    """
    roots: dict[str, dict] = defaultdict(dict)
    for item in data.entries("inv"):
        if isinstance(item.get("value"), dict):
            roots[item["key"].lower()].update(item["value"])
    ui_stats = data.records("ui_stat")
    attributes = data.records("attribute")

    def line_names(inv: str, depth: int = 0) -> list[str]:
        root = roots.get(inv) or {}
        names = [n.lower() for r in root.get("uistats") or [] if (n := _quoted(r))]
        base = (_quoted(root.get("basetype")) or "").lower()
        if base and depth < 8 and root.get("binheritbaseuistats") != "false":
            names = line_names(base, depth + 1) + names
        return list(dict.fromkeys(names))

    lines: dict[str, list[str]] = {}
    stats: dict[str, dict] = {}
    needed: set[str] = set()
    for inv in sorted({k.partition(".")[0] for k in item_keys}):
        shown = []
        for name in line_names(inv):
            stat = ui_stats.get(name) or {}
            group = (_quoted(stat.get("displaygroup")) or "").lower()
            value = stat.get("statvalue") or {}
            if group not in STAT_GROUPS or not value:
                continue
            if name not in stats:
                line: dict[str, Any] = {
                    "label": ncs_parse.loc_text((stat.get("statlabel") or {}).get("formattext", "")),
                    "group": group,
                    "sort": float(stat.get("sortingpriority") or 0),
                }
                if icon := stat.get("staticon"):
                    line["icon"] = icon.removeprefix("Asset'").rstrip("'")
                if "NumericDisplayValue" in str(value.get("structtype")):
                    line["value"] = _number_spec(value)
                else:
                    args = {}
                    for pair in ((value.get("argsmap") or {}).get("pairs") or {}).values():
                        if "NumericDisplayValue" in str((pair.get("value") or {}).get("structtype")):
                            args[pair["key"]] = _number_spec(pair["value"])
                    if not args:
                        continue  # fixed text (part descriptions): nothing to compute
                    line["value"] = {"text": ncs_parse.loc_text(value.get("formattext", "")), "args": args}
                if cond := stat.get("displaycondition"):
                    # (Repair Time: reload type == 4; Shots to Overheat: no magazine)
                    line["if"] = _display_condition(cond)
                stats[name] = line
                needed.update(_SPEC_ATTR.findall(json.dumps(line)))
            shown.append(name)
        lines[inv] = sorted(shown, key=lambda n: stats[n]["sort"])

    tables = data.records("gbx_ue_data_table")

    def table_constant(record: dict[str, Any]) -> float | None:
        """A data table cell or constant attribute (att_playershared_dotinterval: 0.33)."""
        value = record.get("value")
        kind = str(value.get("structtype", "")) if isinstance(value, dict) else ""
        if "GbxDataTableValueResolver" in kind:
            cell = value.get("datatablevalue") or {}
        elif "GbxConstantAttributeValueResolver" in kind:
            init = value.get("attributeinit") or {}
            cell = init.get("datatablevalue") or {}
            if (_quoted(cell.get("datatable")) or "none").lower() == "none":
                try:
                    return float(init.get("constant", 0))
                except (TypeError, ValueError):
                    return None
        else:
            return None
        column = _COLUMN_SUFFIX.sub("", cell.get("columnname") or "").lower()
        for row in (tables.get((_quoted(cell.get("datatable")) or "").lower()) or {}).get("data") or []:
            if row.get("row_name") == cell.get("rowname"):
                try:
                    return float((row.get("row_value") or {}).get(column))
                except (TypeError, ValueError):
                    return None
        return None

    specs: dict[str, dict] = {}
    stack = sorted(needed | (extra or set()))
    while stack:
        name = stack.pop()
        if not name or name in specs:
            continue
        record = attributes.get(name)
        gear_side = name in (gear_attrs or ()) or any(c in str(((record or {}).get("context") or {}).get("structtype", "")) for c in _GEAR_CONTEXTS)
        specs[name] = _attr_spec(record, tables if gear_side else None) if record else {"k": "stored"}
        if specs[name]["k"] == "stored" and record and not _item_bound(record) and (known := table_constant(record)) is not None:
            specs[name] = {"k": "const", "v": known}
        if specs[name]["k"] != "stored":
            text = json.dumps(specs[name])
            stack.extend(_SPEC_ATTR.findall(text))
            stack.extend(n.lower() for n in _ATTR_CALL.findall(text))
            if specs[name]["k"] == "balance":
                stack.extend(spec[1] for key, spec in specs[name].items() if key != "k" and spec[1])

    # Accuracy's weights and maximums aren't in an item's stats: the card takes them per weapon type
    # from the Weapon_Acc_Uistat_Init table (sway max isn't in it: 1)
    rows = {r["row_name"]: r.get("row_value") or {} for r in (tables.get("weapon_acc_uistat_init") or {}).get("data") or []}
    defaults: dict[str, dict[str, float]] = {}
    for inv in lines:
        row = rows.get(ACC_ROWS.get(inv.rpartition("_")[2], ""))
        if row:
            defaults[inv] = {attr: float(row.get(column, fallback)) for attr, (column, fallback) in ACC_COLUMNS.items()}
    # Elemental lines: the status effect defaults per element (DPS scalar, duration)
    elements = {r["row_name"]: {k: float((r.get("row_value") or {}).get(k, {}).get("value", 0)) for k in ("dps", "duration")}
                for r in (tables.get("status_application_defaults") or {}).get("data") or []}  # fmt: skip
    return {"lines": lines, "stats": stats, "attrs": dict(sorted(specs.items())), "defaults": defaults,
            **_flag_parts(data, item_keys, variant_attrs or set()), "elements": elements}  # fmt: skip


def _variant_flags(dex: dict[str, Any], data: Any) -> set[str]:
    """The item-bound attributes the variant texts' conditions read (through their formulas)."""
    attributes = data.records("attribute")
    seen: set[str] = set()
    stack = [v["if"]["attr"] for item in dex["items"].values() for v in item.get("variants") or []]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        text = json.dumps((attributes.get(name) or {}).get("value"))
        stack.extend(n.lower() for n in _ATTR_CALL.findall(text))
        stack.extend(n.lower() for n in _ATTR_REF.findall(text))
    return {n for n in seen if _item_bound(attributes.get(n) or {}) and "InventoryStatsContainer" in json.dumps(attributes.get(n) or {})}


_PART_VALUE = re.compile(r"weapon_part_[a-z_]+_value")


def _flag_parts(data: Any, item_keys: list[str], flags: set[str]) -> dict[str, Any]:
    """
    Every item type's parts by serial index, with what a roll can't read from its stats container:
    the part values (weapon_part_reload_value: 4 on a heat weapon's magazine, from inv_part_value
    aspects), the flags variant texts test (weapon_ted_homing, ...: tags like ted_homing, aspect
    names like ted_is_homing) and the element (element parts: part_fire, ...). A roll reads its part
    indices and works these out.

    A rolled part can also come from an ancestor type's table (element parts live in "weapon"):
    "bases" lists each type's ancestors, and their tables are in "parts" too.
    {"parts": {inv: {index: {"flags", "effects", "el"?}}}, "bases": {inv: [ancestor, ...]}}
    """
    _, parts = _comps_and_parts(data.entries("inv"))
    roots: dict[str, dict] = defaultdict(dict)
    for item in data.entries("inv"):
        if isinstance(item.get("value"), dict):
            roots[item["key"].lower()].update(item["value"])
    words = {f.removeprefix("weapon_") for f in flags}

    def wanted(target: str) -> bool:
        return target in flags or _PART_VALUE.fullmatch(target) is not None

    def table(inv: str) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for key, record in (parts.get(inv) or {}).items():
            index = (record.get("serialindex") or {}).get("index")
            if index is None:
                continue
            tags = [t for t in (next(iter(x)).lower() for x in record.get("addtags") or [] if isinstance(x, dict) and x) if t in words]
            parents = []
            effects: dict[str, float] = {}
            for aspect in record.get("aspects") or []:
                if not isinstance(aspect, dict):
                    continue
                parent = (_quoted(aspect.get("parent")) or "").lower()
                if parent.replace("_is_", "_") in words:
                    parents.append(parent.replace("_is_", "_"))
                for effect in aspect.get("attributeeffects") or []:
                    target = (_quoted(effect.get("attributetomodify")) or "").lower()
                    if wanted(target):
                        try:
                            effects[target] = float((effect.get("modifiervalue") or {}).get("constant", 0))
                        except (TypeError, ValueError):
                            pass
            entry: dict[str, Any] = {}
            if tags or parents:
                entry["flags"] = sorted(set(tags + parents))
            if effects:
                entry["effects"] = effects
            # part_fire / part_shock ... (main element); part_secondary_elem_fire_shock_mal (the
            # second mode's: the one that isn't the main element)
            elements = [e for e in _ELEMENTS if re.search(rf"(^|_){e}(_|$)", key)]
            if elements and ("elem" in key or re.fullmatch(r"part_(" + "|".join(_ELEMENTS) + r")", key)):
                entry["el2" if "secondary" in key else "el"] = [e.title() for e in elements]
            if entry:
                found[str(int(index))] = entry
        return found

    def ancestors(inv: str) -> list[str]:
        chain: list[str] = []
        base = (_quoted((roots.get(inv) or {}).get("basetype")) or "").lower()
        while base and base not in chain and len(chain) < 8:
            chain.append(base)
            base = (_quoted((roots.get(base) or {}).get("basetype")) or "").lower()
        return chain

    out: dict[str, dict[str, dict]] = {}
    bases: dict[str, list[str]] = {}
    for inv in sorted({k.partition(".")[0] for k in item_keys}):
        bases[inv] = [b for b in ancestors(inv) if parts.get(b)]
        for key in [inv, *bases[inv]]:
            if key not in out and (found := table(key)):
                out[key] = found
    return {"parts": out, "bases": {k: v for k, v in bases.items() if v}}


_ELEMENTS = ("fire", "shock", "cryo", "corrosive", "radiation")


# region Part lines on the card


def _part_key(root: dict[str, Any], table: str, name: str, index: Any) -> str:
    """
    How stats.json keys a part within its table: its serial index, or for a part with no serial index
    (4 class mod special stats) the text the serial carries instead, lowercase:
    "classmod.statspecial_ricochet_damage" (stats.part_owners reads -1 for it; roll.py maps it back).
    """
    return str(int(index)) if index is not None else f"{root.get('inv') or table}.{name}".lower()


def build_part_texts(data: Any, item_keys: list[str]) -> dict[str, Any]:
    """
    The card lines a rolled part adds (licensed parts, shield and repkit augments, manufacturer
    perks): {"part_texts": {table serial index: {part serial index: [ui_stat]}},
             "type_texts": {item type: [ui_stat]}, "ui_texts": {ui_stat: {"text", "icon", "args"?, "if"?}}}. Numbers in a text are args
    ({name: number spec}) filled from the roll; "if" is a display condition like variants'.
    """
    _, parts = _comps_and_parts(data.entries("inv"))
    roots: dict[str, dict] = defaultdict(dict)
    for item in data.entries("inv"):
        if isinstance(item.get("value"), dict):
            roots[item["key"].lower()].update(item["value"])
    ui_stats = data.records("ui_stat")
    tables: set[str] = set()
    for inv in {k.partition(".")[0] for k in item_keys}:
        at = inv
        while at and at in roots and at not in tables and len(tables) < 400:
            tables.add(at)
            at = (_quoted(roots[at].get("basetype")) or "").lower()

    texts: dict[str, dict[str, Any]] = {}

    def add(name: str) -> bool:
        """Records a ui_stat's text (once); False if it isn't a card text line."""
        stat = ui_stats.get(name) or {}
        value = stat.get("statvalue") or {}
        # Only the card's list of lines (not its type line, red text or class mod skill points)
        if (_quoted(stat.get("displaygroup")) or "").lower() not in ("secondary", "text"):
            return False
        if "StringDisplayValue" not in str(value.get("structtype")) or not value.get("formattext"):
            return False
        if name not in texts:
            entry: dict[str, Any] = {"text": _markup(value["formattext"])}
            if icon := stat.get("staticon"):
                entry["icon"] = icon.removeprefix("Asset'").rstrip("'")
            args = {}
            for pair in ((value.get("argsmap") or {}).get("pairs") or {}).values():
                arg = (pair.get("value") or {}) if isinstance(pair, dict) else {}
                if "NumericDisplayValue" in str(arg.get("structtype")) and "{" + pair["key"] + "}" in entry["text"]:
                    args[pair["key"]] = _number_spec(arg)
            if args:
                entry["args"] = args
            if cond := stat.get("displaycondition"):
                entry["if"] = _display_condition(cond)
            texts[name] = entry
        return True

    # The item type's own text lines (manufacturer perks: "Ripper - This Gun charges before...")
    type_texts = {}
    for inv in sorted({k.partition(".")[0] for k in item_keys}):
        names = [n for r in (roots.get(inv) or {}).get("uistats") or [] if (n := (_quoted(r) or "").lower()) and add(n)]
        if names:
            type_texts[inv] = names
    out: dict[str, dict[str, list[str]]] = {}
    for table in sorted(tables):
        ti = (roots[table].get("serialindex") or {}).get("index")
        if ti is None:
            continue
        found: dict[str, list[str]] = {}
        for part_name, record in (parts.get(table) or {}).items():
            pi = (record.get("serialindex") or {}).get("index")
            keys = []
            for aspect in record.get("aspects") or []:
                if not isinstance(aspect, dict) or "UIStatAspect" not in str(aspect.get("structtype")):
                    continue
                for ref in aspect.get("uistatstoinclude") or []:
                    name = (_quoted(ref) or "").lower()
                    if add(name):
                        keys.append(name)
            if keys:
                found[_part_key(roots[table], table, part_name, pi)] = keys
        if found:
            out[str(int(ti))] = found
    return {"part_texts": out, "type_texts": type_texts, "ui_texts": texts}


# endregion
# region Class mod skills


def build_classmod_skills(data: Any, item_keys: list[str]) -> tuple[dict[str, list[dict]], dict[str, dict[str, list]]]:
    """
    ({item key: [skill it can roll]}, {class mod type: {part serial index: [graph, node, points]}}).

    A legendary class mod's body part (fixed by the composition) adds slot tags (`red_1_3`,
    `green_left_4_3`, ...); a skill's tier 1 part (`passive_points`) needs one of them, each next
    tier needs the one before, and 7 points are spread over them. So the body's tags are the skills
    it can have, and the tier count is the most points one can get. Skill names and descriptions are
    the skill tree tooltips'. A skill is {"name", "desc", "graph", "node", "max"}.
    """
    import gen_item_cards as cards  # noqa: PLC0415

    comps, parts = _comps_and_parts(data.entries("inv"))
    graphs = data.records("progress_graph")
    tooltips = data.records("uitooltipdata")
    # Tooltips are mostly named after their skill (SkillDef_X: ToolTip_X); the rest by their header
    by_header: dict[str, dict] = {}
    for tooltip in tooltips.values():
        if tooltip.get("header") and tooltip.get("body"):
            by_header.setdefault(ncs_parse.loc_text(tooltip["header"]).lower(), tooltip)

    # The skill screen's own data (skilltrees_data): each node's icon and tooltip, and its tree's
    # color (unset = Red), which the card shows a skill's frame in
    screen: dict[tuple[str, str], dict[str, str]] = {}
    for record in data.records("skilltrees_data").values():
        for tree in record.get("skilltrees") or []:
            color = str(tree.get("color") or "Red").lower()
            for segment in tree.get("segments") or []:
                graph = (_quoted(segment.get("progressgraph")) or "").lower()
                for tier in segment.get("tiers") or []:
                    for node in (tier or {}).get("nodes") or []:
                        if isinstance(node, dict) and node.get("name"):
                            screen[(graph, node["name"])] = {"icon": str(node.get("icon", "")).removeprefix("Asset'").rstrip("'"),
                                                             "tooltip": (_quoted(node.get("tooltip")) or "").lower(), "color": color}  # fmt: skip

    def node_skill(graph: str, node: str) -> tuple[str, str]:
        shown = screen.get((graph.lower(), node)) or {}
        if (tooltip := tooltips.get(shown.get("tooltip", ""))) and tooltip.get("header"):
            return ncs_parse.loc_text(tooltip["header"]), _markup(tooltip["body"]) if tooltip.get("body") else ""
        for pair in cards._graph_nodes(graphs, graph).values():  # noqa: SLF001
            if pair.get("key") != node or not isinstance(pair.get("value"), dict):
                continue
            skill = cards._deref((pair["value"].get("itemdata") or {}).get("skill", ""))  # noqa: SLF001
            tooltip = tooltips.get(("tooltip_" + skill.removeprefix("SkillDef_")).lower()) or {}
            if not tooltip and pair["value"].get("alias"):
                tooltip = by_header.get(str(pair["value"]["alias"]).lower(), {})
            if not tooltip:
                # Passive_Grav_84_SustainedReaction: ToolTip_Grav_Passive_84_SustainedReaction
                tail = "_".join(skill.lower().split("_")[-2:])
                found = [t for k, t in tooltips.items() if k.endswith("_" + tail) and t.get("body")]
                tooltip = found[0] if len(found) == 1 else {}
            name = ncs_parse.loc_text(tooltip["header"]) if tooltip.get("header") else pair["value"].get("alias", skill)
            return name, _markup(tooltip["body"]) if tooltip.get("body") else ""
        return node, ""

    def tags(record: dict[str, Any], field: str) -> list[str]:
        return [next(iter(t)).lower() for t in record.get(field) or [] if isinstance(t, dict) and t]

    skills: dict[str, list[dict]] = {}
    passives: dict[str, dict[str, list]] = {}
    for key in item_keys:
        inv = key.partition(".")[0]
        if not inv.startswith("classmod_") or key not in comps:
            continue
        table = parts.get(inv) or {}
        # part -> [graph, node, points]; tier 1 parts by the slot tag they need; tier n+1 needs tier n's tag
        by_part: dict[str, tuple[str, str, int]] = {}
        needs: dict[str, list[str]] = defaultdict(list)
        for name, record in table.items():
            if "passive_points" not in record:
                continue
            for aspect in record.get("aspects") or []:
                found = (aspect.get("passives") or [None])[0] if isinstance(aspect, dict) else None
                if found:
                    by_part[name] = (_quoted(found.get("progressgraph")) or "", found.get("nodename", ""), int(aspect.get("points") or 1))
            for tag in tags(record, "dependencytags"):
                needs[tag].append(name)
        if inv not in passives:
            passives[inv] = {}
            for name, (graph, node, points) in by_part.items():
                if (index := (table[name].get("serialindex") or {}).get("index")) is not None:
                    passives[inv][str(int(index))] = [graph, node, points]
        rules = ((comps[key].get("parttypeselectionrules") or {}).get("pairs") or {}).values()
        bodies = [p.get("part", "").lower() for r in rules if isinstance(r, dict) and r.get("key") == "class_mod_body"
                  for p in ((r.get("value") or {}).get("parts") or []) if isinstance(p, dict)]  # fmt: skip
        found_skills = []
        for body in bodies:
            for slot in tags(table.get(body) or {}, "addtags"):
                tier = next((p for p in needs.get(slot, []) if p in by_part), None)
                if tier is None:
                    continue
                graph, node, _ = by_part[tier]
                count, seen = 0, set()
                while tier and tier not in seen:
                    seen.add(tier)
                    count += by_part[tier][2]
                    tier = next((p for t in tags(table[tier], "addtags") for p in needs.get(t, []) if p in by_part), None)
                same = next((f for f in found_skills if f["graph"] == graph and f["node"] == node), None)
                if same is not None:
                    same["max"] += count  # two slots can both roll it: their points add up
                    continue
                name, desc = node_skill(graph, node)
                shown = screen.get((graph.lower(), node)) or {}
                found_skills.append({"name": name, "desc": desc, "graph": graph, "node": node, "max": count,
                                     "icon": shown.get("icon", ""), "color": shown.get("color", "red")})  # fmt: skip
        if found_skills:
            skills[key] = found_skills
    return skills, passives


# endregion
# region Gear: values from modifiers

# A gear item's stats container only holds what its parts change, so its card values are worked out
# the way the game does: each attribute starts from its base (an OverrideBaseValue modifier, often a
# player-level formula), then every modifier the item type (and its base types), the composition
# (and its base compositions) and each rolled part adds. Parts mostly name a shared template
# (an `inv_aspect` record in the Resident data, like shield_attr_augment_init) and patch its table
# row ("Amp", "Adaptive"). Checked against the game's own cards (Watts 4 Dinner, Kill Spring).
_MOD_KINDS = {"OverrideBaseValue": "O", "ScaleMultiply": "M", "ScaleAdd": "S", "PreAdd": "P", "PostAdd": "Q"}
# Contexts that mean "the item itself" (on the player they'd read the equipped item instead)
_GEAR_CONTEXTS = (*_ITEM_CONTEXTS, "InventoryContextResolver", "ShieldAttributeContextResolver", "RepairKitContextResolver",
                  "EnhancementContextResolver")  # fmt: skip


def _deep_patch(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        return {**base, **{k: _deep_patch(base.get(k), v) for k, v in patch.items()}}
    return base if patch is None else patch


def _table_cell(tables: dict[str, Any], cell: dict[str, Any]) -> float | list[str] | None:
    """
    A data table cell: its value, None when it names no table, or [table, row, column] when the
    data doesn't hold it. Rows only store what differs from their struct's defaults, which live in
    the game's assets: the mod asks the game for those (GbxDataTableFunctionLibrary).
    """
    table = _quoted(cell.get("datatable")) or "none"
    if table.lower() == "none" or str(cell.get("rowname") or "none").lower() == "none":
        return None  # no table, or a row named None (Waterfall's augment): no value
    column = _COLUMN_SUFFIX.sub("", cell.get("columnname") or "").lower()
    for row in (tables.get(table.lower()) or {}).get("data") or []:
        if row.get("row_name") != cell.get("rowname"):
            continue
        values = {_COLUMN_SUFFIX.sub("", k).lower(): v for k, v in (row.get("row_value") or {}).items()}
        value = values.get(column) if column and column != "none" else next(iter(values.values()), None) if len(values) == 1 else None
        if isinstance(value, dict):
            value = value.get("value")
        try:
            return float(value)
        except (TypeError, ValueError):
            break
    return [table, str(cell.get("rowname") or ""), str(cell.get("columnname") or "")]


def _piece(tables: dict[str, Any], value: Any, empty: float) -> list[Any]:
    """
    A modifier or formula value -> [factor, attribute or "", table cell the game holds or None]:
    table cell (else the constant: it's the fallback, see serialgen.product) x scale (x attribute x
    that cell).
    """
    if not isinstance(value, dict):
        return [empty, "", None, False]
    factor, found, missing = 1.0, False, None
    if "constant" in value:
        factor = float(value["constant"])
        found = True
    if isinstance(value.get("datatablevalue"), dict) and (cell := _table_cell(tables, value["datatablevalue"])) is not None:
        if isinstance(cell, list):
            missing = cell
            factor = 1.0  # the game's cell replaces the constant
        else:
            factor = cell
        found = True
    attr = (_quoted(value.get("attribute")) or "none").lower()
    if attr != "none":
        found = True
    if "postscale" in value:
        factor *= float(value["postscale"])
    # (4th: whether it holds any value at all; a modifier with none doesn't apply)
    return [factor if found else empty, "" if attr == "none" else attr, missing, found]


def build_gear(data: Any, item_keys: list[str]) -> dict[str, Any]:
    """
    {"gear": {inv: {"type": [mod], "comps": {serial index: [mod]}}},
     "gear_parts": {table serial index: {part serial index: [mod]}},
     "gear_game": [attribute evaluated by the game on the player: level formulas],
     "gear_attrs": [item attributes the runtime needs specs for]}
    A mod is [attribute, kind (O/M/S/P/Q), factor, attribute it's multiplied by or "", table cell
    it's multiplied by that only the game knows ([table, row, column]) or None].
    """
    comps, parts = _comps_and_parts(data.entries("inv"))
    roots: dict[str, dict] = defaultdict(dict)
    for item in data.entries("inv"):
        if isinstance(item.get("value"), dict):
            roots[item["key"].lower()].update(item["value"])
    tables = data.records("gbx_ue_data_table")
    attributes = data.records("attribute")
    templates = {k: v for k, v in data.records("Resident").items() if "inv_aspect" in v}

    def chain(inv: str) -> list[str]:
        out = [inv]
        while (base := (_quoted((roots.get(out[-1]) or {}).get("basetype")) or "").lower()) and base not in out and len(out) < 8:
            out.append(base)
        return [k for k in out if k in roots and k != "none"]

    def effects(aspect: dict[str, Any], depth: int = 0) -> list[dict]:
        own = [e for e in aspect.get("attributeeffects") or [] if isinstance(e, dict)]
        parent = (_quoted(aspect.get("parent")) or "").lower()
        if not own and parent in templates and depth < 6:
            own = effects(templates[parent], depth + 1)
        if isinstance(aspect.get("attributeeffecttemplate"), dict):
            own = [_deep_patch(e, aspect["attributeeffecttemplate"]) for e in own]
        return own

    def mods(record: dict[str, Any]) -> list[list[Any]]:
        out = []
        for aspect in record.get("aspects") or []:
            if not isinstance(aspect, dict):
                continue
            for effect in effects(aspect):
                target = (_quoted(effect.get("attributetomodify")) or "").lower()
                piece = _piece(tables, effect.get("modifiervalue"), 1.0)
                if target and target != "none" and piece[3]:
                    kind = _MOD_KINDS.get(effect.get("modifiertype") or "ScaleAdd", "S")
                    out.append([target, kind, *piece[:3]])
        return out

    def index(record: dict[str, Any]) -> str | None:
        found = (record.get("serialindex") or {}).get("index")
        return None if found is None else str(int(found))

    gear: dict[str, dict] = {}
    gear_parts: dict[str, dict[str, list]] = {}
    for inv in sorted({k.partition(".")[0] for k in item_keys}):
        types = chain(inv)
        if "weapon" in types or "turret_gadget" in types:
            # Weapons carry every value in their stats container. Turrets work out theirs from stat
            # points (inv_stat'gadget_turret'), which this doesn't do: they keep reading the container.
            continue
        entry: dict[str, Any] = {"type": [m for t in reversed(types) for m in mods(roots[t])], "comps": {}}
        for key, comp in comps.items():
            if not key.startswith(inv + ".") or (si := index(comp)) is None:
                continue
            line, seen, record, at = [], set(), comp, key
            while record is not None and at not in seen:
                seen.add(at)
                line.append(record)
                at = (_quoted(record.get("basecomposition")) or "").lower()
                record = comps.get(at) if at else None
            entry["comps"][si] = [m for r in reversed(line) for m in mods(r)]
        gear[inv] = entry
        for table in types:
            ti = index(roots[table])
            if ti is None or ti in gear_parts:
                continue
            gear_parts[ti] = {_part_key(roots[table], table, name, (record.get("serialindex") or {}).get("index")): found
                              for name, record in (parts.get(table) or {}).items() if (found := mods(record))}  # fmt: skip

    # Which attributes the item side works out itself, and which the game evaluates on the player
    # (level formulas: player_shield_capacity_formula...)
    targets = {m[0] for e in gear.values() for ms in [e["type"], *e["comps"].values()] for m in ms}
    targets |= {m[0] for t in gear_parts.values() for ms in t.values() for m in ms}
    refs = {m[3] for e in gear.values() for ms in [e["type"], *e["comps"].values()] for m in ms if m[3]}
    refs |= {m[3] for t in gear_parts.values() for ms in t.values() for m in ms if m[3]}

    def bound(name: str) -> bool:
        record = attributes.get(name) or {}
        context = str((record.get("context") or {}).get("structtype", ""))
        return name in targets or any(c in context for c in _GEAR_CONTEXTS) or _item_bound(record)

    game = sorted(r for r in refs if not bound(r))
    return {"gear": gear, "gear_parts": gear_parts, "gear_game": game, "gear_attrs": sorted(targets | {r for r in refs if bound(r)})}


# endregion


# Weapon type (inventory key suffix) -> Weapon_Acc_Uistat_Init row; attribute -> (column, default)
ACC_ROWS = {"ar": "AssaultRifle", "ps": "Pistol", "sg": "Shotgun", "sm": "SMG", "sr": "Sniper", "hw": "HW"}
ACC_COLUMNS = {
    "weapon_spread_ui_weight": ("spreadweight", 0.0),
    "weapon_spread_max_to_normalize": ("spreadmax", 1.0),
    "weapon_accuracy_impulse_ui_weight": ("impulseweight", 0.0),
    "weapon_accuracy_impulse_max_to_normalize": ("impulsemax", 1.0),
    "weapon_sway_ui_weight": ("swayweight", 0.0),
    "weapon_sway_max_to_normalize": ("swaymax", 1.0),
}


# endregion


# stats.json sections the mod only reads by key: each entry is stored as JSON text and parsed when
# first read (stats.py _Lazy). Parsing the whole ~900 KB file held the GIL 6-18 ms on the first roll.
LAZY_SECTIONS = ("ui_texts", "classmod_passives", "gear_parts", "part_texts", "parts")


def pack_stats(stats: dict[str, Any]) -> str:
    compact: dict[str, Any] = {"separators": (",", ":"), "ensure_ascii": False}
    out = {k: ({kk: json.dumps(vv, **compact) for kk, vv in v.items()} if k in LAZY_SECTIONS else v) for k, v in stats.items()}
    return json.dumps(out, **compact) + "\n"


def main() -> None:
    import gen_item_cards as G  # noqa: PLC0415  # needs the item cards mod's pak.py on the path

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

    dex = build_dex(load)
    data = gen_drops.Loot(load).data
    cm_skills, cm_passives = build_classmod_skills(data, list(dex["items"]))
    for key, found in cm_skills.items():
        dex["items"][key]["skills"] = found
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dex, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(dex['bosses'])} bosses, {len(dex['items'])} items -> {OUT}")
    effect_attrs = {spec["attr"] for item in dex["items"].values() for spec in (item.get("args") or {}).values()}
    effect_attrs |= {v["if"]["attr"] for item in dex["items"].values() for v in item.get("variants") or []}
    flags = _variant_flags(dex, data)
    gear = build_gear(data, list(dex["items"]))
    gear_attrs = set(gear["gear_attrs"])
    part_texts = build_part_texts(data, list(dex["items"]))
    text_attrs = {spec["attr"] for t in part_texts["ui_texts"].values() for spec in (t.get("args") or {}).values() if spec.get("attr")}
    text_attrs |= {t["if"]["attr"] for t in part_texts["ui_texts"].values() if t.get("if", {}).get("attr")}
    # Text numbers no part sets and no item holds, from a player balance formula (Exo Soldier raid2's
    # "Damage while Gliding": a skill applies it): asked from the game on the player, like gear_game
    attributes = data.records("attribute")
    for name in sorted(text_attrs - gear_attrs):
        record = attributes.get(name) or {}
        value = record.get("value")
        kind = str(value.get("structtype", "")) if isinstance(value, dict) else ""
        if "BalanceFormulaValueResolver" in kind and not _item_bound(record) and name not in gear["gear_game"]:
            gear["gear_game"].append(name)
            print(f"  card text number from the game: {name}")
    stats = build_stats(data, list(dex["items"]), effect_attrs | flags | gear_attrs | text_attrs, flags, gear_attrs)
    stats.update(gear)
    stats.update(part_texts)
    stats["classmod_passives"] = cm_passives
    STATS_OUT.write_text(pack_stats(stats), encoding="utf-8")
    print(f"{len(stats['lines'])} item types, {len(stats['stats'])} card lines, {len(stats['attrs'])} attributes -> {STATS_OUT}")
    # How each item rolls (the mod rolls its own serials: gen_weapondex_rolls.py)
    import gen_weapondex_rolls  # noqa: PLC0415

    rolls = gen_weapondex_rolls.build_rolls(data.entries("inv"), list(dex["items"]))
    gen_weapondex_rolls.OUT.write_text(gen_weapondex_rolls.pack(rolls), encoding="utf-8")
    print(f"{len(rolls['items'])} items' roll rules -> {gen_weapondex_rolls.OUT}")


if __name__ == "__main__":
    main()
