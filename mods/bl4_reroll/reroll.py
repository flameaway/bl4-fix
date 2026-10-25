"""
Which lines of a class mod or enhancement can be rerolled, and new serials with them rerolled.

Plain Python (no SDK imports): it works on serial strings and parts.json, so it can be tested
outside the game.

The stat lines at the bottom of these items are parts of a shared table (234 for class mods, 247
for enhancements), written into the serial as {234:[a b c]} or as several {247:a} tokens. That
list also holds the item's firmware and, for enhancements, its body. We only touch the stat parts
(stat_group1/2/3), and follow the same rules the game rolls them with:

- a line stays in its group (a class mod's second-group stat comes from the second group)
- no two lines may share a tag the other excludes (that's how the game keeps e.g. "Critical Hit
  Damage" from rolling twice, once in each group)
- a stat isn't rolled below its minimum item level
- the special stats only legendaries carry are never rolled onto anything, and the stat a
  legendary always comes with stays put

Everything else in the serial (level, seed, skills, perks, firmware, rarity) is kept as it is.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from functools import cache, lru_cache
from importlib import resources

from . import serial as sn

STAT_GROUPS = ("stat_group1", "stat_group2", "stat_group3")


@cache
def data() -> dict:
    return json.loads(resources.files(__package__).joinpath("parts.json").read_text(encoding="utf-8"))


@dataclass
class Line:
    """One stat line: where it sits in the serial and what it is."""

    token: int  # index of the part token in Serial.tokens
    slot: int | None  # position in that token's list, or None for a {table:value} token
    part: int
    key: str
    name: str
    group: str
    locked: bool  # the legendary's own stat


@dataclass
class Item:
    serial: str
    parsed: sn.Serial
    type_id: int
    kind: str  # "classmod" or "enhancement"
    label: str  # "Siren Class Mod"
    rarity: str  # "legendary", ...
    level: int
    base: int  # the shared table's index
    lines: list[Line]
    # Class mod skills: "<graph>,<node>" (lowercase, as on the item card) -> points
    skills: dict[str, int]

    @property
    def rerollable(self) -> list[Line]:
        return [line for line in self.lines if not line.locked]


@lru_cache(maxsize=1024)
def read(serial: str) -> Item | None:
    """The item's stat lines, or None if it isn't a class mod / enhancement we know. Don't change the result."""
    try:
        parsed = sn.decode(serial)
        type_id = parsed.item_type
        level = parsed.level
    except (ValueError, IndexError):
        return None
    info = data()["types"].get(str(type_id))
    if info is None:
        return None
    base = info["base"]
    stats = data()["bases"][str(base)]["stats"]

    comp = None
    skills: dict[str, int] = {}
    for part in parsed.parts():
        if part.value is not None or part.values is not None:
            continue
        if comp is None and str(part.index) in info["comps"]:
            comp = info["comps"][str(part.index)]
        for node, points in info.get("skills", {}).get(str(part.index), []):
            skills[node.lower()] = skills.get(node.lower(), 0) + points
    pinned = (comp or {}).get("pinned", {})

    lines = []
    for i, token in enumerate(parsed.tokens):
        if not isinstance(token, sn.Part) or token.index != base:
            continue
        values = [(None, token.value)] if token.values is None else list(enumerate(v.value for v in token.values))
        for slot, value in values:
            stat = stats.get(str(value))
            if value is None or stat is None or stat["group"] not in STAT_GROUPS:
                continue  # firmware, body
            locked = stat["special"] or stat["key"].lower() in pinned.get(stat["group"], [])
            lines.append(Line(i, slot, value, stat["key"], stat["name"], stat["group"], locked))
    return Item(serial, parsed, type_id, info["kind"], info["label"], (comp or {}).get("rarity", ""),
                level, base, lines, skills)  # fmt: skip


def _compatible(stat: dict, others: list[dict]) -> bool:
    add, excl = set(stat["add"]), set(stat["excl"])
    return all(not (add & set(o["excl"])) and not (excl & set(o["add"])) for o in others)


def candidates(item: Item, line: Line, others: list[int]) -> list[int]:
    """Parts this line could roll, given the parts on the item's other lines."""
    stats = data()["bases"][str(item.base)]["stats"]
    other_stats = [stats[str(p)] for p in others if str(p) in stats]
    return [
        int(part)
        for part, stat in stats.items()
        if stat["group"] == line.group
        and not stat["special"]
        and stat["min_level"] <= item.level
        and int(part) not in others
        and _compatible(stat, other_stats)
    ]


def _with_parts(item: Item, parts: dict[int, int]) -> str:
    """The item's serial with some lines (by index in item.lines) set to new parts."""
    parsed = sn.decode(item.serial)
    for index, part in parts.items():
        line = item.lines[index]
        token = parsed.tokens[line.token]
        assert isinstance(token, sn.Part)
        if line.slot is None:
            token.value, token.value_groups = part, None
        else:
            values = token.values or []
            values[line.slot] = sn.Num(part, varbit=values[line.slot].varbit)
    out = sn.encode(parsed)
    check = read(out)
    if check is None or [ln.part for ln in check.lines] != [
        parts.get(i, ln.part) for i, ln in enumerate(item.lines)
    ]:
        raise ValueError("the rerolled serial didn't read back the same")
    return out


def reroll_all(item: Item, rng: random.Random | None = None) -> str | None:
    """Every unlocked line rolled again (a line may land on what it had). None if nothing can change."""
    rng = rng or random.Random()
    current = {i: line.part for i, line in enumerate(item.lines)}
    free = [i for i, line in enumerate(item.lines) if not line.locked]
    if not free:
        return None
    for i in free:
        del current[i]
    rng.shuffle(free)  # no line gets first pick every time
    for i in free:
        pool = candidates(item, item.lines[i], list(current.values()))
        current[i] = rng.choice(pool) if pool else item.lines[i].part
    changes = {i: current[i] for i in free}
    return _with_parts(item, changes)


def reroll_one(item: Item, index: int, rng: random.Random | None = None) -> str | None:
    """Line `index` (in item.lines) swapped for a different stat. None if it can't change."""
    rng = rng or random.Random()
    line = item.lines[index]
    if line.locked:
        return None
    others = [ln.part for i, ln in enumerate(item.lines) if i != index]
    pool = [p for p in candidates(item, line, others) if p != line.part]
    if not pool:
        return None
    return _with_parts(item, {index: rng.choice(pool)})
