"""
An item's card stats, computed the way the game's item card does, from the item's own stats.

Every item identity (FInventoryIdentity) holds a shared pointer at +0x90 to a stats container
(GbxWeapon.WeaponStatsContainer and friends) once the game has built its stats: a TArray at
+0x290 of 32-byte entries {GbxAttributeDef*, u32, f32 value at +0xC, f32 base at +0x10, ...}.
Those are the item's own attribute values (damage, fire rate, spread, reload time, ...). The card
lines (ui_stat records) show some of them directly and others through small formulas and
conditions over them (weapon_compare_reload_time, weapon_accuracy_ui_compare, ...); stats.json
(tools/gen_weapondex.py) has both the lines per item type and those formulas.

Attribute definitions have no readable name, so we go name -> definition: the game resolves a
GameDataHandle of the attribute type when it's passed to GbxAttributeBlueprintLibrary.
GetAttributeAsFloat, and the returned handle's address is the definition. Done once per name.

Memory is read with ReadProcessMemory only (a bad pointer fails instead of crashing the game).
Game thread only for resolve_defs(); the rest is plain Python.
"""

from __future__ import annotations

import ast
import ctypes
import json
import math
import operator
import struct
import time
from functools import cache
from importlib import resources
from typing import Any

ATTRIBUTE_HANDLE_TYPE = 0xF00000D0
IDENTITY_STATS = 0x90  # TSharedPtr<FInventoryStatsContainer>
CONTAINER_VALUES_AT = (0x290, 0x108)  # TArray<entry>: weapons, everything else
ENTRY_SIZE = 32
ENTRY_VALUE = 0xC
MAX_ENTRIES = 1024

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype = ctypes.c_int
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_PROCESS = _k32.GetCurrentProcess()


def _read(address: int, size: int) -> bytes | None:
    if not 0x10000 <= address < 0x7FFFFFFFFFFF or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_PROCESS, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)) or got.value != size:
        return None
    return buffer.raw


def _u64(address: int) -> int | None:
    data = _read(address, 8)
    return None if data is None else struct.unpack("<Q", data)[0]


# Sections of stats.json whose entries are stored as JSON text (tools/gen_weapondex.py LAZY_SECTIONS):
# parsing the whole file at once held the GIL 6-18 ms on the first roll; now ~1-2 ms
LAZY_SECTIONS = ("ui_texts", "classmod_passives", "gear_parts", "part_texts", "parts")


class _Lazy(dict):
    """A section whose entries are parsed when first read (only read by key: get / [])."""

    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        if isinstance(value, str):
            value = json.loads(value)
            super().__setitem__(key, value)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self else default


@cache
def data() -> dict[str, Any]:
    found = json.loads((resources.files(__package__) / "stats.json").read_text(encoding="utf-8"))
    for section in LAZY_SECTIONS:
        if isinstance(found.get(section), dict):
            found[section] = _Lazy(found[section])
    return found


# region Reading an item's stats


class _Defs:
    by_address: dict[int, str] = {}  # noqa: RUF012
    resolved = False
    todo: list[str] | None = None  # names still to look up (resolve_defs with a budget)
    found: dict[int, str] = {}  # noqa: RUF012


def resolve_defs(context: Any, budget_s: float | None = None) -> bool:
    """
    Finds the definition of every attribute we read (once; ~4.5 ms for all 334). Game thread;
    context: any UObject. With a budget it stops after that long and goes on at the next call
    (False until it's done).
    """
    if _Defs.resolved:
        return True
    import unrealsdk  # noqa: PLC0415 - only in game
    from unrealsdk.unreal import FGameDataHandle  # noqa: PLC0415

    lib = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject
    if _Defs.todo is None:
        _Defs.todo = [name for name, spec in data()["attrs"].items() if spec["k"] == "stored"]
        _Defs.found = {}
    start = time.perf_counter()
    while _Defs.todo:
        if budget_s is not None and time.perf_counter() - start > budget_s:
            return False
        name = _Defs.todo.pop()
        try:
            address = lib.GetAttributeAsFloat(FGameDataHandle(ATTRIBUTE_HANDLE_TYPE, name), context, 0.0)[1]._get_address()
        except Exception:  # noqa: BLE001 - an attribute this build doesn't have
            continue
        if address:
            _Defs.found[address] = name
    _Defs.by_address, _Defs.todo = _Defs.found, None  # (none found: tried again next time)
    _Defs.resolved = bool(_Defs.by_address)
    return _Defs.resolved


def has_stats(identity: int) -> bool:
    return bool(_u64(identity + IDENTITY_STATS))


def _values_array(container: int) -> tuple[int, int] | None:
    """
    The container's attribute values TArray: at +0x290 in a WeaponStatsContainer, +0x108 in the
    others (shields, gadgets, repkits, class mods). Checked: its first entry must point at an
    attribute definition (same vtable as the ones we resolved).
    """
    def_vtable = _u64(next(iter(_Defs.by_address))) if _Defs.by_address else None
    for offset in CONTAINER_VALUES_AT:
        head = _read(container + offset, 12)
        if head is None:
            continue
        entries, count = struct.unpack("<QI", head)
        if not 0 < count <= MAX_ENTRIES:
            continue
        first = _u64(entries)
        if first and (first in _Defs.by_address or (def_vtable and _u64(first) == def_vtable)):
            return entries, count
    return None


def read_values(identity: int) -> dict[str, float] | None:
    """{attribute: value} from an identity's stats container, or None if it has none (yet)."""
    container = _u64(identity + IDENTITY_STATS)
    if not container:
        return None
    found = _values_array(container)
    if found is None:
        return None
    entries, count = found
    raw = _read(entries, count * ENTRY_SIZE)
    if raw is None:
        return None
    values: dict[str, float] = {}
    for i in range(count):
        name = _Defs.by_address.get(struct.unpack_from("<Q", raw, i * ENTRY_SIZE)[0])
        if name:
            values[name] = struct.unpack_from("<f", raw, i * ENTRY_SIZE + ENTRY_VALUE)[0]
    return values


# endregion
# region Formulas


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Gt: operator.gt, ast.Lt: operator.lt, ast.GtE: operator.ge, ast.LtE: operator.le,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}  # fmt: skip


@cache
def _parse(formula: str) -> ast.Expression:
    text = formula.replace("&&", " and ").replace("||", " or ").replace("!=", "<>").replace("!", " not ").replace("<>", "!=")
    return ast.parse(text.strip(), mode="eval")


Mods = dict[str, list[tuple[str, float, str, "list[str] | None"]]]


class Game:
    """
    What only the game knows (roll.py's version asks it, on the game thread): the player's level
    formulas, and data table cells the data leaves at their struct's default.
    """

    def attr(self, name: str) -> float | None:  # noqa: ARG002
        return None

    def cell(self, table: str, row: str, column: str) -> float:  # noqa: ARG002
        return 0.0



class Evaluator:
    """
    Attribute values for one item: stored ones from its container, the rest computed. A gear item
    also has its modifiers (`mods`, see gear_mods) and the game's value of the player's level
    formulas (`game`).
    """

    def __init__(self, stored: dict[str, float], defaults: dict[str, float] | None = None, mods: Mods | None = None,
                 game: Game | None = None) -> None:  # fmt: skip
        self.stored = {**(defaults or {}), **stored}
        self.mods = mods or {}
        self.game = game
        self.cache: dict[str, float] = {}
        self.missing: set[str] = set()  # stored attributes read that the item doesn't have

    def known(self, name: str) -> bool:
        """False for a stored attribute the item doesn't have (computed ones always count)."""
        return (data()["attrs"].get(name, {"k": "stored"})["k"] != "stored" or name in self.stored or name in self.mods
                or (self.game is not None and name in _game_attrs()))  # fmt: skip

    def attr(self, name: str, depth: int = 0) -> float:
        if name in self.cache:
            return self.cache[name]
        spec = data()["attrs"].get(name, {"k": "stored"})
        value = 0.0
        if depth < 32:
            kind = spec["k"]
            if name in self.stored and (kind == "stored" or name in self.mods):
                value = self.stored[name]
            elif self.game is not None and name in _game_attrs():
                found = self.game.attr(name)
                if found is None:
                    self.missing.add(name)
                value = found or 0.0
            elif name in self.mods:
                value = self.modded(name, spec, depth)
            elif kind == "balance":
                value = self.balance(spec, depth)
            elif kind == "stored":
                value = self.stored.get(name, 0.0)
                if name not in self.stored:
                    self.missing.add(name)
            elif kind == "const":
                value = spec.get("v", 0.0)
            elif kind == "partis":
                value = float(self.attr(spec["attr"], depth + 1) == spec["v"])
            elif kind == "expr":
                value = self.formula(spec, depth)
            elif kind == "cond":
                value = self.value(spec["default"], depth)
                for cond, case in spec["cases"]:
                    if self.condition(cond, depth):
                        value = self.value(case, depth)
                        break
        self.cache[name] = value
        return value

    def modded(self, name: str, spec: dict[str, Any], depth: int) -> float:
        """
        A value built from modifiers, the way the game combines them (checked against its cards):
        (base + pre-adds) x (1 + positive scale-adds) / (1 + negative ones) x multipliers + post-adds.
        The base is the last OverrideBaseValue, else the attribute's own value.
        """
        base: float | None = None
        pre = post = up = down = 0.0
        scale = 1.0
        for kind, factor, ref, cell in self.mods[name]:
            amount = factor * (self.attr(ref, depth + 1) if ref else 1.0) * self.cell(cell)
            if kind == "O":
                base = amount
            elif kind == "M":
                scale *= amount
            elif kind == "S":
                up, down = (up + amount, down) if amount >= 0 else (up, down - amount)
            elif kind == "P":
                pre += amount
            elif kind == "Q":
                post += amount
        if base is None:
            base = self.unmodded(spec, depth)
        return (base + pre) * (1 + up) / (1 + down) * scale + post

    def cell(self, cell: list[str] | None) -> float:
        if not cell:
            return 1.0
        return self.game.cell(*cell) if self.game is not None else 0.0

    def unmodded(self, spec: dict[str, Any], depth: int) -> float:
        kind = spec["k"]
        if kind == "const":
            return spec.get("v", 0.0)
        if kind == "expr":
            return self.formula(spec, depth)
        if kind == "balance":
            return self.balance(spec, depth)
        return 0.0

    def balance(self, spec: dict[str, Any], depth: int) -> float:
        """A balance formula: multiplier x (level ^ power + offset)."""

        def piece(key: str) -> float:
            factor, ref, cell = spec[key]
            return factor * (self.attr(ref, depth + 1) if ref else 1.0) * self.cell(cell)

        try:
            return float(piece("m") * (piece("l") ** piece("p") + piece("o")))
        except (ArithmeticError, TypeError, ValueError):
            return 0.0

    def value(self, spec: dict[str, Any], depth: int) -> float:
        if "attr" in spec:
            return self.attr(spec["attr"], depth + 1) * spec.get("scale", 1.0)
        return float(spec.get("const", 0.0))

    def condition(self, spec: dict[str, Any], depth: int) -> bool:
        if "attr" in spec:
            return self.attr(spec["attr"], depth + 1) != 0
        return bool(self.formula(spec["expr"], depth))

    def formula(self, spec: dict[str, Any], depth: int) -> float:
        variables = {k: self.value(v, depth) for k, v in spec.get("vars", {}).items()}
        try:
            return float(self._eval(_parse(spec["formula"]).body, variables, depth))
        except (ArithmeticError, KeyError, SyntaxError, TypeError, ValueError):
            return 0.0

    def _eval(self, node: ast.AST, variables: dict[str, float], depth: int) -> float:  # noqa: PLR0911
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            return variables.get(node.id, 0.0)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "attr" and node.args:
            arg = node.args[0]
            return self.attr(arg.id.lower() if isinstance(arg, ast.Name) else str(arg.value).lower(), depth + 1)  # type: ignore[attr-defined]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = self._eval(node.left, variables, depth), self._eval(node.right, variables, depth)
            if isinstance(node.op, ast.Div) and right == 0:
                return 0.0
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, variables, depth)
            return -operand if isinstance(node.op, ast.USub) else float(not operand) if isinstance(node.op, ast.Not) else operand
        if isinstance(node, ast.BoolOp):
            results = [bool(self._eval(v, variables, depth)) for v in node.values]
            return float(all(results) if isinstance(node.op, ast.And) else any(results))
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, variables, depth)
            for op, comparator in zip(node.ops, node.comparators, strict=False):
                right = self._eval(comparator, variables, depth)
                if type(op) not in _OPS or not _OPS[type(op)](left, right):
                    return 0.0
                left = right
            return 1.0
        raise ValueError(f"unsupported {type(node).__name__}")


# endregion
# region Card lines

_COMPARE = {
    "GreaterThan": operator.gt, "GreaterOrEqual": operator.ge, "LessThan": operator.lt, "LessOrEqual": operator.le,
    "Equal": operator.eq, "NotEqual": operator.ne,
}  # fmt: skip


def _number(value: float, spec: dict[str, Any]) -> str:
    if spec.get("sign"):
        value = abs(value) if spec["sign"] == "Positive" else -abs(value)
    if spec.get("percent"):
        value *= 100
    precision = spec.get("precision", 0)
    mode = spec.get("round", "")
    if mode == "RoundToInt" or precision == 0:
        value = float(math.floor(value + 0.5)) if mode != "Floor" else float(math.floor(value))
        precision = 0
    if spec.get("percent"):
        # The card shows whole percents (a shotgun at 82.5% reads 82%)
        value, precision = float(round(value)), 0
    text = f"{value:,.{precision}f}"
    if spec.get("percent"):
        text += "%"
    if spec.get("plus") and value > 0:
        text = "+" + text
    if fmt := spec.get("format"):
        text = fmt.replace("$VALUE$", text)
    return text


def card_lines(inv: str, stored: dict[str, float], mods: Mods | None = None, game: Game | None = None) -> list[dict[str, Any]]:
    """The stat lines an item of type `inv` shows on its card: [{"key", "label", "text", "group", "icon", "value"}]."""
    ev = Evaluator(stored, data().get("defaults", {}).get(inv), mods, game)
    # Gear (not weapons): its stats container only holds what this roll's parts change, so a line
    # needing anything else would show a made-up 0; leave those out. (A weapon's missing values
    # really are 0: no burst, no charge time.)
    strict = "weapon" not in data().get("bases", {}).get(inv, [])
    out = []
    for key in data()["lines"].get(inv, []):
        line = data()["stats"][key]
        cond = line.get("if")
        if cond and cond["attr"]:
            if not ev.known(cond["attr"]) and cond.get("missing") is False:
                continue
            if not _COMPARE.get(cond["cmp"], operator.eq)(ev.attr(cond["attr"]), cond["value"]):
                continue
        spec = line["value"]
        # A line whose own value the item doesn't carry (a heavy weapon's cooldown lives on the
        # character's gadget) would read 0: leave it out
        if not any(ev.known(arg["attr"]) for arg in (spec["args"].values() if "args" in spec else [spec])):
            continue
        ev.missing.clear()
        ev.cache.clear()
        if "args" in spec:
            text = spec["text"]
            numbers = {name: ev.attr(arg["attr"]) for name, arg in spec["args"].items()}
            for name, arg in spec["args"].items():
                text = text.replace("{" + name + "}", _number(numbers[name], arg))
            main = next(iter(numbers.values()), 0.0)
        else:
            main = ev.attr(spec["attr"])
            text = _number(main, spec)
        if strict and ev.missing:
            continue
        out.append({"key": key, "label": line["label"], "text": text, "group": line["group"], "icon": line.get("icon", ""), "value": main})
    return out


# endregion


def fill_args(inv: str, stored: dict[str, float], args: dict[str, dict[str, Any]], mods: Mods | None = None,
              game: Game | None = None) -> dict[str, str]:  # fmt: skip
    """
    Numbers for an effect text's placeholders ({name: card text}), for the ones this roll has. Args
    marked "game" (not item-bound) are roll.level_fills' (it leaves out 0, "not the player's").
    """
    ev = Evaluator(stored, data().get("defaults", {}).get(inv), mods, game)
    return {name: _number(ev.attr(spec["attr"]), spec) for name, spec in args.items() if not spec.get("game") and ev.known(spec["attr"])}


def passing(inv: str, stored: dict[str, float], conditions: list[dict[str, Any]], mods: Mods | None = None,
            game: Game | None = None) -> list[int]:  # fmt: skip
    """Which of these display conditions this roll meets (indexes)."""
    ev = Evaluator(stored, data().get("defaults", {}).get(inv), mods, game)
    out = []
    for i, cond in enumerate(conditions):
        if not ev.known(cond["attr"]) and cond.get("missing") is False:
            continue
        if _COMPARE.get(cond["cmp"], operator.eq)(ev.attr(cond["attr"]), cond["value"]):
            out.append(i)
    return out


# region Composition and parts of an identity

IDENTITY_COMP = 0x8  # the composition's def (InventoryPartDef-like, GbxSerialNumberAwareDef)
IDENTITY_PARTS = 0x10  # TArray of part def pointers
DEF_SERIAL_INDEX = 0x18 + 0x12  # GbxSerialNumberIndex.Index (int16) in a GbxSerialNumberAwareDef
MAX_PARTS = 64


def _serial_index(definition: int) -> int | None:
    raw = _read(definition + DEF_SERIAL_INDEX, 2)
    return None if raw is None else struct.unpack("<h", raw)[0]


def comp_index(identity: int) -> int | None:
    """The serial index of the composition an identity was rolled as."""
    comp = _u64(identity + IDENTITY_COMP)
    return _serial_index(comp) if comp else None


DEF_OWNER = 0x30  # the table (item type) a part or composition def belongs to


def part_indices(identity: int) -> list[tuple[int, bool]]:
    """
    The rolled parts: (serial index, from the item's own type's table). Parts of an ancestor type's
    table (element parts are in "weapon") have their own numbering; they're told apart by the table
    their def belongs to, compared with the composition's.
    """
    head = _read(identity + IDENTITY_PARTS, 12)
    comp = _u64(identity + IDENTITY_COMP)
    own = _u64(comp + DEF_OWNER) if comp else None
    if head is None:
        return []
    array, count = struct.unpack("<QI", head)
    if not 0 < count <= MAX_PARTS:
        return []
    raw = _read(array, count * 8) or b""
    out = []
    for (definition,) in struct.iter_unpack("<Q", raw):
        index = _serial_index(definition) if definition else None
        if index is not None:
            out.append((index, own is not None and _u64(definition + DEF_OWNER) == own))
    return out


def part_owners(identity: int) -> list[tuple[int, int]]:
    """The rolled parts as (serial index, serial index of the item type whose table it's from)."""
    head = _read(identity + IDENTITY_PARTS, 12)
    if head is None:
        return []
    array, count = struct.unpack("<QI", head)
    if not 0 < count <= MAX_PARTS:
        return []
    out = []
    for (definition,) in struct.iter_unpack("<Q", _read(array, count * 8) or b""):
        index = _serial_index(definition) if definition else None
        owner = _u64(definition + DEF_OWNER) if definition else None
        table = _serial_index(owner) if owner else None
        if index is not None and table is not None:
            out.append((index, table))
    return out


def gear_mods(inv: str, comp: int | None, owners: list[tuple[int, int]]) -> Mods | None:
    """
    A gear item's modifiers by attribute, in the order the game applies them: its item type (base
    types first), its composition (base ones first), then each rolled part. None for weapons.
    """
    gear = data().get("gear", {}).get(inv)
    if gear is None:
        return None
    tables = data().get("gear_parts", {})
    found = [*gear["type"], *gear["comps"].get(str(comp), [])]
    for index, table in owners:
        found.extend(tables.get(str(table), {}).get(str(index), []))
    out: Mods = {}
    for target, kind, factor, ref, cell in found:
        out.setdefault(target, []).append((kind, factor, ref, cell))
    return out


def part_lines(inv: str, owners: list[tuple[int, int]], stored: dict[str, float], mods: Mods | None = None,
               game: Game | None = None) -> list[dict[str, str]]:  # fmt: skip
    """
    The card's text lines this roll's item type and parts add (manufacturer perks, licensed parts,
    augments): [{"key", "text", "icon"}], numbers filled, ones whose display condition fails left out.
    """
    texts = data().get("ui_texts", {})
    tables = data().get("part_texts", {})
    keys = list(data().get("type_texts", {}).get(inv, []))
    for index, table in owners:
        keys.extend(tables.get(str(table), {}).get(str(index), []))
    ev = Evaluator(stored, data().get("defaults", {}).get(inv), mods, game)
    out, seen = [], set()
    for key in keys:
        entry = texts.get(key)
        if not entry or key in seen:
            continue
        seen.add(key)
        cond = entry.get("if")
        if cond and cond.get("attr"):
            if not ev.known(cond["attr"]) and cond.get("missing") is False:
                continue
            if not _COMPARE.get(cond["cmp"], operator.eq)(ev.attr(cond["attr"]), cond["value"]):
                continue
        text = entry["text"]
        for name, spec in (entry.get("args") or {}).items():
            text = text.replace("{" + name + "}", _number(ev.attr(spec["attr"]), spec) if ev.known(spec["attr"]) else "")
        out.append({"key": key, "text": text, "icon": entry.get("icon", "")})
    return out


def classmod_skills(inv: str, parts: list[tuple[int, bool]]) -> dict[str, int] | None:
    """A class mod roll's skill points, {"<graph>,<node>": points}, from its own table's skill parts."""
    table = data().get("classmod_passives", {}).get(inv)
    if table is None:
        return None
    out: dict[str, int] = {}
    for index, own in parts:
        if own and (found := table.get(str(index))):
            graph, node, points = found
            out[f"{graph},{node}"] = out.get(f"{graph},{node}", 0) + points
    return out


@cache
def _game_attrs() -> frozenset[str]:
    return frozenset(data().get("gear_game", []))


ELEMENT_ATTRS = {
    "weapon_ui_calc_elemental_dps_scalar": (0, "dps"), "weapon_ui_calc_elemental_duration": (0, "duration"),
    "weapon_ui_calc_elemental_dps_scalar_mode02": (1, "dps"),
}  # fmt: skip


def part_flags(inv: str, parts: list[tuple[int, bool]]) -> dict[str, float]:
    """
    What the rolled parts tell (stats.json "parts"): part values, variant flags (weapon_ted_homing,
    ...) and the element's status effect values (the card's elemental lines).
    """
    tables = data().get("parts", {})
    bases = data().get("bases", {}).get(inv, [])
    out: dict[str, float] = {}
    main: str | None = None
    second: list[str] = []
    for index, own in parts:
        part = None
        for table in [inv] if own else bases:
            part = tables.get(table, {}).get(str(index))
            if part:
                break
        if not part:
            continue
        for word in part.get("flags", []):
            out["weapon_" + word] = max(out.get("weapon_" + word, 0.0), 1.0)
        for attr, value in part.get("effects", {}).items():
            # Part values are set (OverrideBaseValue), flags add up
            out[attr] = value if attr.startswith("weapon_part_") else out.get(attr, 0.0) + value
        if part.get("el"):
            main = part["el"][0]
        if part.get("el2"):
            second = part["el2"]
    elements = [main] if main else []
    if second:
        elements.append(next((e for e in second if e != main), second[0]))
    defaults = data().get("elements", {})
    for attr, (slot, column) in ELEMENT_ATTRS.items():
        if slot < len(elements) and elements[slot] in defaults:
            out[attr] = defaults[elements[slot]][column]
    for slot, element in enumerate(elements[:2]):
        out[f"$element{slot}"] = _ELEMENT_NAMES.index(element) if element in _ELEMENT_NAMES else -1
    return out


_ELEMENT_NAMES = ["Fire", "Shock", "Cryo", "Corrosive", "Radiation"]


def elements_of(values: dict[str, float]) -> list[str]:
    """The roll's elements (main, second mode's), as part_flags noted them."""
    out = []
    for slot in (0, 1):
        index = int(values.get(f"$element{slot}", -1))
        if 0 <= index < len(_ELEMENT_NAMES):
            out.append(_ELEMENT_NAMES[index])
    return out


# endregion


def entry_count(identity: int) -> int:
    """How many attribute values the identity's stats container holds so far (it fills over frames)."""
    container = _u64(identity + IDENTITY_STATS)
    found = _values_array(container) if container else None
    return found[1] if found else 0
