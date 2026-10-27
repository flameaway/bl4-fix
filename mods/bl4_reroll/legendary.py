"""
Legendaries and pearls: which items are one, and a new roll of one at its own level.

A legendary reroll is a fresh drop of the same legendary: every part the game rolls (barrel and
body accessories, grip, scope, magazine, underbarrel, element, licensed parts, firmware on gear, a
class mod's skills and stats) is rolled again by the game's own rules, weights and level gates
(serialgen.py, the roller Kairosdex checked against 393 real drops), plus a new seed. Rare parts
stay rare: a part with a small weight comes up as often as it would on a real drop.

What isn't rolled stays: the item, its level, header fields we don't know (`9, 1` on some gear),
and a cosmetic applied to it (`"c", 27` after the parts).

Plain Python (no SDK imports): the game values the weights need come in through `resolve`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from functools import lru_cache

from . import serial as sn
from . import serialgen

SEED_FIELD = 2  # header `2, seed`
# Prepared rolls kept: (item, level) -> its parts with weights and gates applied
_MAX_PREPARED = 64
_prepared: dict[tuple[str, int], serialgen.Prepared] = {}


@dataclass
class Legend:
    serial: str
    key: str  # "<inv>.<comp>", as in legendaries.json
    name: str  # "Slugger"
    rarity: str  # "legendary" or "pearlescent"
    label: str  # "Shotgun"
    maker: str
    level: int
    type_id: int
    # Read like a class mod's reroll.Item where code lists both: no stat lines, no skills
    kind: str = "legendary"
    lines: tuple = ()
    skills: dict = field(default_factory=dict)

    @property
    def pearl(self) -> bool:
        return self.rarity.startswith("pearl")


def has_data() -> bool:
    try:
        return bool(serialgen._file().get("index"))  # noqa: SLF001
    except (OSError, ValueError):
        return False


def source_data() -> dict[str, str]:
    """{game file: SHA1} the rules were made from."""
    return dict(serialgen._file().get("source_data") or {})  # noqa: SLF001


def _comp(parsed: sn.Serial) -> int | None:
    """The composition: the first part, a plain `{n}` of the item's own table."""
    parts = parsed.parts()
    if not parts or parts[0].value is not None or parts[0].values is not None:
        return None
    return parts[0].index


@lru_cache(maxsize=1024)
def read(serial: str) -> Legend | None:
    """The legendary / pearl this serial is, or None for anything else. Don't change the result."""
    try:
        parsed = sn.decode(serial)
        type_id, level = parsed.item_type, parsed.level
    except (ValueError, IndexError):
        return None
    comp = _comp(parsed)
    if comp is None:
        return None
    data = serialgen._file()  # noqa: SLF001
    key = (data.get("index") or {}).get(f"{type_id}:{comp}")
    if key is None:
        return None
    name, rarity, label, maker = (data.get("names") or {}).get(key) or [key, "Legendary", "", ""]
    return Legend(serial, key, name, rarity.lower(), label, maker, level, type_id)


def _sections(parsed: sn.Serial) -> tuple[list[sn.Token], list[sn.Token], list[sn.Token]]:
    """(header up to and with its `||`, the parts, the rest from the `|` after them)."""
    tokens = parsed.tokens
    first = next((i for i, t in enumerate(tokens) if isinstance(t, sn.Part)), None)
    if first is None:
        raise ValueError("no parts")
    end = next((i for i in range(first, len(tokens)) if isinstance(tokens[i], sn.Sep) and tokens[i].hard), len(tokens))
    return tokens[:first], tokens[first:end], tokens[end:]


def _with_seed(head: list[sn.Token], seed: int) -> list[sn.Token]:
    """The header with its seed field set (a copy; a header without one is left as it is)."""
    out = list(head)
    nums = [i for i, t in enumerate(out) if isinstance(t, sn.Num)]
    # `type, 0, 1, level| 2, seed|| ...`: key / value pairs from the third number on
    for k in range(2, len(nums) - 1, 2):
        key = out[nums[k]]
        if isinstance(key, sn.Num) and key.value == SEED_FIELD:
            out[nums[k + 1]] = sn.Num(seed)
            break
    return out


def prepare(item: Legend, resolve: serialgen.Resolve) -> serialgen.Prepared:
    """The item's slots and pickable parts at its level (asks `resolve` for game values the first time)."""
    key = (item.key, item.level)
    prepared = _prepared.get(key)
    if prepared is None:
        if len(_prepared) >= _MAX_PREPARED:
            _prepared.clear()
        prepared = _prepared[key] = serialgen.Prepared(item.key, item.level, resolve)
    return prepared


def reroll(item: Legend, rng: random.Random, resolve: serialgen.Resolve) -> str:
    """
    The serial of a new roll of this legendary at its level. The same rng seed and game values
    give the same serial (a co-op host rolls a friend's reroll again and compares).
    """
    prepared = prepare(item, resolve)
    parts = serialgen.generate(prepared, rng)
    seed = rng.randint(1, serialgen.SEED_MAX)
    fresh = sn.decode(serialgen.serial(prepared.item_type, item.level, seed, parts))
    old = sn.decode(item.serial)
    head, _, tail = _sections(old)
    _, new_parts, _ = _sections(fresh)
    out = sn.encode(sn.Serial(_with_seed(head, seed) + new_parts + tail))

    # Check it reads back as the same item with exactly the rolled parts, and kept what isn't rolled
    check = read(out)
    if check is None or (check.key, check.level, check.type_id) != (item.key, item.level, item.type_id):
        raise ValueError("the rerolled serial didn't read back as the same item")
    back = sn.decode(out)
    _, b_parts, b_tail = _sections(back)
    if (str(sn.Serial(b_parts)) != str(sn.Serial(new_parts)) or str(sn.Serial(b_tail)) != str(sn.Serial(tail))
            or {k: v for k, v in back.header().items() if k != SEED_FIELD}
            != {k: v for k, v in old.header().items() if k != SEED_FIELD}):  # fmt: skip
        raise ValueError("the rerolled serial didn't read back the same")
    return out
