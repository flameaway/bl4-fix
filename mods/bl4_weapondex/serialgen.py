"""
Rolls a dex item ourselves and writes its serial: no pickup, no actor, nothing in the world.

The rules come from rolls.json (tools/gen_weapondex_rolls.py, read from the game's inv data; the
rules and how they were checked against real rolls are in CLAUDE.md "Rolling our own serials"):
per slot of the item type, how many parts (min/max, or min + one per success of a chance), which
parts (an allowlist, the tags each part needs or excludes, caps on parts carrying a tag), weighted
picks, and level gates. Weights, gates and chances that name an attribute or a data table cell
are asked from the game through `resolve` (roll.py caches them).

The serial: `type, 0, 1, level| 2, seed||` then the composition and the parts in slot order: own
table `{n}`, another table `{t:n}`, a run of one other table `{t:[a b]}`, and a part that has no
serial index as text `"ClassMod.statspecial_ricochet_damage"` (as the game's own serials do). Numbers take the shorter
of the two encodings, as the game's own serials do (the type is a varbit from 256 up). Format as
documented by the community decoders; our own encoder, checked against bl4_reroll/serial.py and
real serials (dev/re/serialgen/test_mod_gen.py).

Plain Python (no SDK imports).
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from functools import cache
from importlib import resources
from typing import Any

SEED_MAX = 4095  # real serials' seeds: 18..4093

# A weight / gate / chance: a float constant, or an int index into rolls.json "values"
Resolve = Callable[[dict[str, Any]], float]


@cache
def _file() -> dict[str, Any]:
    return json.loads((resources.files(__package__) / "rolls.json").read_text(encoding="utf-8"))


@cache
def _table(owner: str) -> dict[str, Any]:
    return json.loads(_file()["tables"][owner])


@cache
def _type(inv: str) -> dict[str, Any]:
    return json.loads(_file()["types"][inv])


def has(key: str) -> bool:
    inv = key.partition(".")[0]
    return inv in _file()["types"] and key in _type(inv)["items"]


@cache
def value_specs(key: str) -> tuple[dict[str, Any], ...]:
    """Every attribute / table value a roll of this item may ask `resolve` for (to look them up first)."""
    inv = key.partition(".")[0]
    t, item = _type(inv), _type(inv)["items"][key]
    used: set[int] = set()
    for owner in t["chain"]:
        for parts in _table(owner)["parts"].values():
            for part in parts:
                used.update(v for v in part[4:6] if type(v) is int)
    used.update(r[2] for r in item["rules"].values() if type(r[2]) is int)
    values = _file()["values"]
    return tuple(values[i] for i in sorted(used))


def _number(spec: float | int, resolve: Resolve) -> float:
    if type(spec) is int:
        return resolve(_file()["values"][spec])
    return float(spec)


def product(spec: dict[str, Any], attr: Callable[[str], float | None], cell: Callable[[str, str, str], float]) -> float:
    """
    A value from its pieces: a table cell, else the constant (the constant is the fallback: class
    mod stat gates are "25, table cell" with cells 1/25/30/35/40, and 25 x cell made them 625-1000,
    so no roll ever got them; a real level-50 Paladin mod has one), times the attribute and the post
    scale (constant x attribute: 0.25 x a Tediore weight, 0.2 x a class mod stat).
    """
    out = cell(*spec["t"]) if spec.get("t") else float(spec.get("c", 1.0))
    if spec.get("a"):
        found = attr(spec["a"])
        out *= 1.0 if found is None else found
    return out * float(spec.get("p", 1.0))


def _mask(tags: list[int]) -> int:
    """Tags as a bit mask: plain ints, which Python's GC doesn't track (65k frozensets were 30% of
    the objects every full collection walked, 2026-10-24); tuples of them get untracked too."""
    out = 0
    for t in tags:
        out |= 1 << t
    return out


@cache
def _candidates(inv: str, slot: str) -> tuple[tuple[int, int | str, int, int, int, float | int, float | int], ...]:
    """A slot's parts in the type's tables (its chain): (table, index, add, dep, exc, weight, gate); tags as masks."""
    out = []
    for owner in _type(inv)["chain"]:
        table = _table(owner)
        for part in table["parts"].get(slot, []):
            full = part + [[], [], [], 1.0, 0.0][len(part) - 1 :]
            out.append((table["ti"], full[0], _mask(full[1]), _mask(full[2]), _mask(full[3]), full[4], full[5]))
    return tuple(out)


class Prepared:
    """
    An item's slots with the parts it may roll at one level: allowlist, level gates and weights
    applied once (their values don't change in a session). generate() then only checks tags per
    pick (a class mod's 511 skill parts took ~2 ms a roll without this).
    """

    def __init__(self, key: str, level: int, resolve: Resolve) -> None:
        inv = key.partition(".")[0]
        t = _type(inv)
        item = t["items"][key]
        self.item_type = t["ti"]
        self.comp = item["comp"]
        self.tags = _mask(item["tags"])
        self.caps = [(_mask(c[0]), c[1]) for c in item["caps"]]
        # per slot: (min, max, chance or None, [(table, index, add, dep, exc, weight)]); tags as masks
        self.slots: list[tuple[int, int, float | None, list[tuple[int, int | str, int, int, int, float]]]] = []
        for slot in t["slots"]:
            cands = _candidates(inv, slot)
            lo, hi, chance, allow = item["rules"].get(slot, [1, 1, None, None])
            pool = []
            for i in range(len(cands)) if allow is None else allow:
                table, index, add, dep, exc, weight, gate = cands[i]
                if (type(gate) is int or gate > 0) and _number(gate, resolve) > level:
                    continue  # level gate (mingamestage)
                weight = _number(weight, resolve)
                if weight > 0:
                    pool.append((table, index, add, dep, exc, weight))
            self.slots.append((lo, hi, None if chance is None else _number(chance, resolve), pool))


def generate(prepared: Prepared, rng: random.Random) -> list[tuple[int, int]]:
    """One roll: [(table serial index, part index)], the composition first, then the parts in serial order."""
    out = [(prepared.item_type, prepared.comp)]
    tags = prepared.tags
    caps = prepared.caps
    capped = [0] * len(caps)
    for lo, hi, chance, cands in prepared.slots:
        if chance is not None:
            want = lo
            while want < hi and rng.random() < chance:
                want += 1
        else:
            want = rng.randint(lo, hi) if hi >= lo else lo
        picked: set[int] = set()
        for _ in range(want):
            pool, weights = [], []
            for i, (_, _, add, dep, exc, weight) in enumerate(cands):
                if i in picked or dep & tags != dep or exc & tags:
                    continue
                if caps and any(c >= m and cap & add for (cap, m), c in zip(caps, capped, strict=True)):
                    continue
                pool.append(i)
                weights.append(weight)
            if not pool:
                break  # a slot left short is fine when nothing pickable remains
            i = rng.choices(pool, weights=weights)[0]
            picked.add(i)
            table, index, add, _, _, _ = cands[i]
            tags |= add
            for n, (cap, _) in enumerate(caps):
                if cap & add:
                    capped[n] += 1
            out.append((table, index))
    return out


def serial(item_type: int, level: int, seed: int, parts: list[tuple[int, int]]) -> str:
    """The serial of an item: its type, level, seed and parts as generate() returns them."""
    bits = [_MAGIC, _num(item_type), "01", _num(0), "01", _num(1), "01", _num(level), "00",
            _num(2), "01", _num(seed), "00", "00"]  # fmt: skip
    i = 0
    while i < len(parts):
        table, index = parts[i]
        if isinstance(index, str):  # a part with no serial index: "<type>.<part>" as text
            bits.append("111" + _varint(len(index)) + "".join(_lsb(ord(c), 7) for c in index))
            i += 1
            continue
        if table == item_type:
            bits.append("101" + _varint(index) + "010")  # {n}
            i += 1
            continue
        j = i
        while j < len(parts) and parts[j][0] == table and not isinstance(parts[j][1], str):
            j += 1
        if j - i == 1:
            bits.append("101" + _varint(table) + "1" + _varint(index) + "000")  # {t:n}
        else:
            bits.append("101" + _varint(table) + "001" + "01" + "".join(_num(p[1]) for p in parts[i:j]) + "00")  # {t:[a b]}
        i = j
    bits.append("00")
    stream = "".join(bits)
    stream += "0" * (-len(stream) % 8)
    data = bytes(int(stream[k : k + 8][::-1], 2) for k in range(0, len(stream), 8))  # bytes are bit-reversed
    return "@U" + _b85(data)


def roll(prepared: Prepared, level: int, rng: random.Random | None = None) -> tuple[str, list[tuple[int, int]]]:
    """(serial, parts) of a new roll of a prepared item at `level` (the level it was prepared for)."""
    rng = rng or random.Random()
    parts = generate(prepared, rng)
    return serial(prepared.item_type, level, rng.randint(1, SEED_MAX), parts), parts


# region Bits

_MAGIC = "0010000"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{/}~"


def _lsb(value: int, n: int) -> str:
    return f"{value:0{n}b}"[::-1] if n else ""


def _varint(value: int) -> str:
    """4-bit groups, least significant first, each followed by a continuation bit (at most 4)."""
    groups = max(1, (value.bit_length() + 3) // 4)
    if groups > 4:  # noqa: PLR2004
        raise ValueError(f"{value} doesn't fit a varint")
    return "".join(_lsb((value >> (4 * g)) & 0xF, 4) + ("1" if g < groups - 1 else "0") for g in range(groups))


def _num(value: int) -> str:
    """A number token: varint (100) or varbit (110: 5-bit length, then the bits), whichever is shorter."""
    width = value.bit_length()
    varbit = "110" + _lsb(width, 5) + _lsb(value, width)
    if value >= 1 << 16:
        return varbit
    varint = "100" + _varint(value)
    return varbit if len(varbit) < len(varint) else varint


def _b85(data: bytes) -> str:
    out = []
    for start in range(0, len(data), 4):
        chunk = data[start : start + 4]
        value = int.from_bytes(chunk.ljust(4, b"\0"), "big")
        digits = []
        for _ in range(5):
            digits.append(value % 85)
            value //= 85
        digits.reverse()
        out += [_ALPHABET[d] for d in digits[: 5 if len(chunk) == 4 else len(chunk) + 1]]  # noqa: PLR2004
    return "".join(out)


# endregion
