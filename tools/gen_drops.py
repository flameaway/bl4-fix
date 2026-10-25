"""
Builds item_cards/drops.json for legendary item cards: who drops each legendary and how often.

Used by tools/gen_item_cards.py (dev only). Everything comes from the game's NCS data:

- `gbx_ue_data_table` tables named `Table_*_DedicatedDrops` have a row per boss: the boss
  (`characterdef` -> `gbxactor'Char_...'`) and its loot list (`itempoollist'ItemPoolList_...'`).
  Each boss has a normal row and a `<row>_TrueBoss` row, the Big Encore version.
- An `ItemPoolList` has `itempools[]`: an item (or pool) and a `probability`, each rolled on its
  own, so the chance per kill is that probability (times `postscale` if present).
- Probabilities are a data table cell, a constant, or an attribute whose formula usually depends
  on the Ultimate Vault Hunter level, e.g. "(UVH_Level * UVH_Scalar) + Loot_Probability". We
  evaluate them at UVH 0 and UVH 5.
- Boss names: `gbxactor.uxdisplayname` -> `display_data` text (following `parent` if needed).
- Item names: an item's `inv_comp` lists its parts; the part with an `InventoryNamingAspect`
  names it through `inv_name_part` (third field of "namespace, key, text" is the English name).
- World drops: the item appears in a general rarity pool like `itempool_sr_05_legendary`.
- Big Encore machines: `gbx_discovery_location_meta_data` boss fight markers
  (`DiscoveryLocation_Activity_BossFight*`), whose `locationdisplaydata` text is the fight and
  `worldmapname` the map. Bosses are tied to them as the Weapondex does (gen_weapondex.build_dex).

Returns the drops, the Big Encore machines (list; sources point at one with "encore_id"), and the
fingerprints of the game data used. Drops are keyed by lowercased display name (one name can
belong to several items):
    {"hellwalker": [{"item": "JAK_SG.comp_05_legendary_Hellwalker", "world": true,
                     "sources": [{"boss": "The Immortal Boneface", "uvh0": 0.09, "uvh5": 0.19,
                                  "encore": 0.3, "encore_map": "Upper City"}]}]}
"""

from __future__ import annotations

import ast
import operator
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import Any

import ncs_parse

Loader = Callable[[str], tuple[list[dict], dict[str, str]]]

_QUOTED = re.compile(r"'([^']+)'")
_WORLD_POOL = re.compile(r"^itempool_[a-z0-9_]+_0[56]_(legendary|pearl)$", re.IGNORECASE)
_TRUE_BOSS = "_trueboss"
# "Big Encore X", also "The Big Encore X"
_ENCORE_WORDS = re.compile(r"(?:\bThe\s+)?\bBig Encore\s+", re.IGNORECASE)
_RARITY_WORDS = {"legendary", "unique", "pearl", "pearlescent", "epic", "rare", "common", "uncommon"}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _quoted(value: Any) -> str | None:
    match = _QUOTED.search(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


def _calc(formula: str, variables: dict[str, float]) -> float:
    """Evaluates a game formula: numbers, + - * /, parentheses and named variables only."""

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return variables[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise ValueError(f"unsupported formula: {formula}")

    return ev(ast.parse(formula, mode="eval"))


class _Data:
    def __init__(self, load: Loader) -> None:
        self.fingerprints: dict[str, str] = {}
        self.load = load

    def records(self, *names: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in self.entries(*names):
            if isinstance(item.get("value"), dict):
                out.setdefault(item["key"].lower(), item["value"])
        return out

    def entries(self, *names: str) -> list[dict]:
        out: list[dict] = []
        for name in names:
            entries, prints = self.load(name)
            self.fingerprints.update(prints)
            out.extend(entries)
        return out


class Loot:
    """
    The game's loot data with the lookups both item cards and the Weapondex need: drop chances,
    boss names and legendary item names. Records come from `load` (see _Data).
    """

    def __init__(self, load: Loader) -> None:
        self.data = data = _Data(load)
        self.texts = {k: ncs_parse.loc_text(v["text"]) for k, v in data.records("display_data").items() if "text" in v}
        self.actors = data.records("GbxActor")
        self.tables = data.records("gbx_ue_data_table")
        self.attributes = data.records("attribute")
        self.pool_lists = data.records("ItemPoolList")

    # region Probabilities

    def cell(self, table: str, row: str, column: str) -> float:
        rows = (self.tables.get(table.lower()) or {}).get("data") or []
        name = column.split("_")[0].lower()
        for entry in rows:
            if entry.get("row_name") == row:
                return float((entry.get("row_value") or {}).get(name, 0) or 0)
        return 0.0

    def resolve(self, value: dict, uvh: int) -> float:
        kind, inner = value.get("type"), value.get("value")
        if kind == "DataTable":
            return self.cell(_quoted(inner["datatable"]) or "", inner["rowname"], inner["columnname"])
        if kind == "Attribute":
            name = _quoted(inner) or ""
            return float(uvh) if name.lower() in ("vault_hunter_level", "att_mayhemuvh_level") else self.attribute(name, uvh)
        if kind == "Constant":
            return float(inner)
        raise ValueError(f"unsupported value {kind}")

    def attribute(self, name: str, uvh: int) -> float:
        record = self.attributes.get(name.lower()) or {}
        expression = ((record.get("value") or {}).get("expression")) or {}
        formula = expression.get("formula")
        if not formula:
            return 0.0
        pairs = (((expression.get("variables") or {}).get("variablevalues") or {}).get("pairs")) or {}
        variables = {p["key"]: self.resolve(p["value"]["value"], uvh) for p in pairs.values() if isinstance(p, dict)}
        return _calc(formula, variables)

    def chance(self, probability: Any, uvh: int) -> float | None:
        """The chance per kill of an ItemPoolList entry's `probability`, at this UVH level."""
        if not isinstance(probability, dict):
            return None
        scale = float(probability.get("postscale", "1") or 1)
        if "datatablevalue" in probability:
            d = probability["datatablevalue"]
            return self.cell(_quoted(d["datatable"]) or "", d["rowname"], d["columnname"]) * scale
        if "attribute" in probability:
            return self.attribute(_quoted(probability["attribute"]) or "", uvh) * scale
        if "constant" in probability:
            return float(probability["constant"]) * scale
        return None

    # endregion
    # region Names

    def boss_name(self, actor: str, depth: int = 0) -> str | None:
        record = self.actors.get(actor.lower())
        if not record or depth > 10:
            return None
        display = _quoted(record.get("uxdisplayname"))
        if display and display.lower() in self.texts:
            return self.texts[display.lower()]
        parent = _quoted(record.get("parent"))
        return self.boss_name(parent, depth + 1) if parent else None

    def display_def(self, actor: str, depth: int = 0) -> str | None:
        """The UI name record an actor shows (e.g. Name_OldThrash), following `parent`."""
        record = self.actors.get(actor.lower())
        if not record or depth > 10:
            return None
        display = _quoted(record.get("uxdisplayname"))
        if display:
            return display
        parent = _quoted(record.get("parent"))
        return self.display_def(parent, depth + 1) if parent else None

    def item_names(self, comp_filter: Callable[[str], bool] | None = None) -> dict[str, set[str]]:
        """
        {"<inv>.<comp>" lowercased: display names} for the compositions comp_filter accepts
        (given the lowercased comp name; default: legendaries and pearls).
        """
        if comp_filter is None:
            comp_filter = lambda comp: "05_legendary" in comp or "pearl" in comp  # noqa: E731
        name_parts = {
            k: ncs_parse.loc_text(v["partname"]) for k, v in self.data.records("inv_name_part").items() if "partname" in v
        }
        # An item type's compositions and its named parts can sit in different files (base game
        # and patches) under the same key, so collect everything per key before matching them up
        by_part: dict[str, dict[str, list[str]]] = defaultdict(dict)
        by_tag: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        comps: dict[str, list[tuple[str, list[str], list[str]]]] = defaultdict(list)
        for item in self.data.entries("inv"):
            inv_key = item["key"].lower()
            for dep in item.get("deps") or []:
                value = dep.get("value")
                if not isinstance(value, dict):
                    continue
                if dep.get("table") == "inv_comp":
                    rules = value.get("parttypeselectionrules")
                    parts = [s for s in _walk_strings(rules) if s and "'" not in s and " " not in s]
                    tags = [next(iter(t)).lower() for t in value.get("basetags") or [] if isinstance(t, dict) and t]
                    comps[inv_key].append((value.get("inv_comp", dep["key"]), parts, tags))
                    continue
                strings = list(_walk_strings(value))
                if not any("InventoryNamingAspect" in s for s in strings):
                    continue
                titles = [name_parts.get(n.lower()) for s in strings if (n := _quoted(s)) and s.startswith("inv_name_part")]
                titles = [t.strip() for t in titles if t and t.strip()]
                if not titles:
                    continue
                by_part[inv_key][dep["key"].lower()] = titles
                for tag in value.get("dependencytags") or []:
                    if isinstance(tag, dict) and tag and next(iter(tag)).lower() not in _RARITY_WORDS:
                        by_tag[inv_key][next(iter(tag)).lower()].update(titles)

        item_names: dict[str, set[str]] = defaultdict(set)
        for inv_key, inv_comps in comps.items():
            for comp, parts, tags in inv_comps:
                if not comp_filter(comp.lower()):
                    continue
                names: set[str] = set()
                for part in parts:
                    names.update(by_part[inv_key].get(part.lower(), []))
                if not names:
                    for tag in tags:
                        names.update(by_tag[inv_key].get(tag, []))
                item_names[f"{inv_key}.{comp}".lower()].update(names)
        return item_names

    def world_items(self) -> set[str]:
        """Items (lowercased "<inv>.<comp>") in the general legendary and pearl world pools."""
        out: set[str] = set()
        for key, record in self.data.records("itempool").items():
            if _WORLD_POOL.match(key):
                for s in _walk_strings(record):
                    if s.startswith("inv'") and (item := _quoted(s)):
                        out.add(item.lower())
        return out

    def dedicated_rows(self) -> Iterator[tuple[str, str, str | None, str | None]]:
        """(table, row name, boss actor, item pool list) for each row of the DedicatedDrops tables."""
        for key, table in self.tables.items():
            if not key.endswith("dedicateddrops"):
                continue
            for row in table.get("data") or []:
                strings = list(_walk_strings(row.get("row_value")))
                pool_list = next((_quoted(s) for s in strings if s.startswith("itempoollist")), None)
                actor = next((_quoted(s) for s in strings if s.startswith("gbxactor")), None)
                yield key, row.get("row_name", ""), actor, pool_list

    def map_name(self, world: str) -> str:
        """The region name players see ("Providence Island"), else a readable map id."""
        base = world.removesuffix("_P").lower()
        if base == "world":
            return ""  # the open world: the game's own map shows where the machine is
        for key in (f"uiregiondisplaydata_{base}", *sorted(k for k in self.texts if k.startswith("uiregiondisplaydata_") and k.endswith("_" + base))):
            if self.texts.get(key):
                return self.texts[key]
        return _pretty_map(world)

    # endregion


def build_drops(load: Loader) -> tuple[dict[str, list[dict]], list[dict], dict[str, str]]:
    loot = Loot(load)
    data, texts, pool_lists, chance, boss_name = loot.data, loot.texts, loot.pool_lists, loot.chance, loot.boss_name
    item_names = loot.item_names()
    map_name = loot.map_name

    # region Big Encore machines

    # Each machine: its boss, map, discovery record (for a map pin) and where it stands. Every
    # boss fight marker is one (story, raid and challenge fights alike); most are named
    # *BossReplay*, but drill sites and some DLC ones are plain OakInteractiveObjects.
    machines: list[dict] = []
    machine_at: dict[str, int] = {}  # marker key -> index into machines
    machine_of: dict[str, int] = {}  # boss name -> index into machines
    for key, record in sorted(data.records("gbx_discovery_location_meta_data").items()):
        if "BossFight" not in str(record.get("locationtype")):
            continue
        display = record.get("locationdisplaydata")
        boss = None
        if isinstance(display, dict) and display.get("text"):
            boss = ncs_parse.loc_text(display["text"])
        elif (name := _quoted(display)) and name.lower() in texts:
            boss = texts[name.lower()]
        world = record.get("worldmapname") or ""
        where = record.get("actorworldlocation") or {}
        if not (boss and world and where):
            continue
        machine_at[key] = len(machines)
        machine_of.setdefault(_boss_key(boss), len(machines))
        machines.append({
            "boss": boss,
            "map": world,
            "place": map_name(world),
            "location": key,
            "x": float(where.get("x", 0)),
            "y": float(where.get("y", 0)),
            "z": float(where.get("z", 0)),
        })

    # The fast travel station to arrive at before walking (teleporting) to each machine: the
    # nearest one on the machine's map. Level travel stations count when a map has no other kind.
    stations: list[tuple[int, str, str, float, float, float]] = []  # (rank, name, map, x, y, z)
    for record in data.records("gbx_discovery_location_meta_data").values():
        kind = str(record.get("locationtype") or "")
        station = _quoted(record.get("travelstationdef"))
        where = record.get("actorworldlocation") or {}
        if not station or station.lower() == "none" or not where or "SendOnly" in kind:
            continue
        rank = 0 if "MenuIO_FastTravel" in kind else 1 if "MenuIO_LevelTravel" in kind else None
        if rank is None:
            continue
        stations.append((rank, station, record.get("worldmapname") or "", float(where["x"]), float(where["y"]), float(where["z"])))
    for machine in machines:
        here = [s for s in stations if s[2].lower() == machine["map"].lower()]
        if here:
            best = min(here, key=lambda s: (s[0], (s[3] - machine["x"]) ** 2 + (s[4] - machine["y"]) ** 2 + (s[5] - machine["z"]) ** 2))
            machine["station"] = best[1]

    # endregion

    world_items = loot.world_items()

    # Which boss each actor def is and which fight (machine) it belongs to, as the Weapondex groups
    # them: by row name as well as by name, so a boss whose rows show different names ("The Big
    # Encore Callous Harbinger..." next to "Callous Harbinger...") or whose machine has another
    # name ("Sturm" and "Drang" at "Sturmdrang") still finds its machine
    import gen_weapondex  # noqa: PLC0415  # it imports this module

    boss_of_actor: dict[str, tuple[str, int | None]] = {}
    for dex_boss in gen_weapondex.build_dex(load)["bosses"]:
        for char in dex_boss["chars"]:
            boss_of_actor[char] = (dex_boss["name"], machine_at.get(dex_boss.get("fight") or ""))

    # item -> boss -> source. Big Encore rows are named "<row>_TrueBoss" / "<row>_TRUE" or their
    # boss is called "Big Encore <boss>"; both count toward the same boss as its normal rows.
    sources: dict[str, dict[str, dict]] = defaultdict(dict)
    for _table, row_name, actor, pool_list in loot.dedicated_rows():
        boss = boss_name(actor) if actor else None
        if not boss or not actor:
            continue
        encore = row_name.lower().endswith((_TRUE_BOSS, "_true")) or bool(_ENCORE_WORDS.search(boss))
        boss = _ENCORE_WORDS.sub("", boss).strip()
        boss, machine = boss_of_actor.get(actor.lower(), (boss, None))
        if machine is None:
            machine = machine_of.get(_boss_key(boss))
        for entry in (pool_lists.get((pool_list or "").lower()) or {}).get("itempools") or []:
            if not isinstance(entry, dict):
                continue
            items = [i.lower() for s in _walk_strings(entry.get("itempool")) if s.startswith("inv'") and (i := _quoted(s))]
            if not items:
                continue  # shiny variants and nested pools: not a specific legendary
            low, high = chance(entry.get("probability"), 0), chance(entry.get("probability"), 5)
            if low is None:
                continue
            for item in items:
                source = sources[item].setdefault(_boss_key(boss), {"boss": boss})
                if encore:
                    source["encore"] = max(source.get("encore", 0.0), round(low, 5))
                else:
                    source["uvh0"] = max(source.get("uvh0", 0.0), round(low, 5))
                    source["uvh5"] = max(source.get("uvh5", 0.0), round(high if high is not None else low, 5))
                if machine is not None:
                    source.setdefault("machine", machine)

    out: dict[str, list[dict]] = defaultdict(list)
    for item, names in item_names.items():
        found = []
        for source in sources.get(item, {}).values():
            machine = source.pop("machine", None)
            if "encore" in source and machine is not None:
                source["encore_id"] = machine
                if machines[machine]["place"]:
                    source["encore_map"] = machines[machine]["place"]
            found.append(source)
        entry = {"item": item, "world": item in world_items, "sources": found}
        for name in names:
            out[name.lower()].append(entry)
    return dict(out), machines, data.fingerprints


def _boss_key(name: str) -> str:
    """'The Divisioner' and 'Divisioner' are the same boss."""
    return re.sub(r"^the\s+", "", " ".join(name.lower().split()))


def _pretty_map(world: str) -> str:
    """'UpperCity_P' -> 'Upper City'. The open world (World_P) gets no name: the map shows it."""
    base = world.removesuffix("_P")
    if base.lower() == "world":
        return ""
    words = []
    for part in base.split("_"):
        words.extend(re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+|\d+", part))
    return " ".join(words)
