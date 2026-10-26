"""
"Roll one at my level": we roll a legendary ourselves and read its card stats, with nothing spawned.

serialgen.py rolls the item's parts by the game's own rules (rolls.json) at the player's level and
writes its serial; identity.py decodes that serial into an item identity of our own with the game's
save-load decoder, which also builds the item's stats container, exactly as the game does for an
item it loads; stats.py turns the container into card lines, and the identity is destroyed, all in
one tick. No pickup, no actor, no compass star, nothing for the Lost Loot machine.

Works the same as host, client or solo (it's all local). One roll per frame (~1-3 ms).
"""

from __future__ import annotations

import math
import random
import struct
import time
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

from . import card, data, identity, serialgen, stats

MAX_ROLLS = 20
KEEP_ITEMS = 60  # items whose rolls we remember (hovering the list rolls each item once)
LOOKUP_BUDGET_S = 0.002  # per tick: weights / gates asked from the game before an item's first roll


class S:
    queue: list[tuple[str, int]] = []  # noqa: RUF012 - (item key, rolls still to do)
    results: dict[str, list[dict[str, Any]]] = {}  # noqa: RUF012 - item key -> rolls ({lines, fills})
    status: dict[str, str] = {}  # noqa: RUF012 - item key -> "" / "rolling" / an error to show
    pc: WeakPointer | None = None
    latency: list[float] = []  # noqa: RUF012 - ms per roll, for checks
    no_picture: set[str] = set()  # noqa: RUF012 - items rolled for a hover card: no picture render
    current: str | None = None  # the item being rolled (its weights still being looked up)
    version = 0  # bumped on every change, for the page
    on_change: Any = None
    pictures: dict[str, dict[str, Any]] = {}  # noqa: RUF012 - item key -> its latest roll's picture (thumbs.tick_shots)
    last_values: dict[str, float] = {}  # noqa: RUF012
    history: list[tuple[str, str, dict[str, float], list[tuple[int, int]]]] = []  # noqa: RUF012 - (key, serial, values, owners)
    rng = random.Random()


class Game(stats.Game):
    """
    What a roll needs from the game: the player's level formulas (evaluated on the player, so at
    their level), data table cells the data leaves at their default (read through
    GbxDataTableFunctionLibrary, cached: there are a few hundred), and the attributes behind part
    weights and level gates (constants; cached).
    """

    MISSING = -1.0e30
    PARAM_FLOAT = 8  # GbxParam: the value sits at +8
    cells: dict[tuple[str, str, str], float] = {}  # noqa: RUF012
    constants: dict[str, float | None] = {}  # noqa: RUF012 - weight / gate / chance attributes
    prepared: dict[tuple[str, int], serialgen.Prepared] = {}  # noqa: RUF012 - (item, level): its parts, values applied

    def __init__(self, pawn: Any) -> None:
        self.pawn = pawn
        self.lib = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject

    def attr(self, name: str) -> float | None:
        try:
            value = float(self.lib.GetValueOfAttribute(name, self.pawn, self.MISSING))
        except Exception:  # noqa: BLE001
            return None
        # Not a number = no value (Undershield's formula gives infinity on a player without one)
        return None if value == self.MISSING or not math.isfinite(value) else value

    def constant(self, name: str) -> float | None:
        if name not in Game.constants:
            Game.constants[name] = self.attr(name)
        return Game.constants[name]

    def cell(self, table: str, row: str, column: str) -> float:
        key = (table, row, column)
        if key not in Game.cells:
            value = 0.0
            try:
                from unrealsdk.unreal import FGbxDefPtr  # noqa: PLC0415

                tables = unrealsdk.find_object("ScriptStruct", "/Script/GbxEngine.GbxUEDataTableDef")
                handle = unrealsdk.make_struct("DataTableValueHandle", DataTable=FGbxDefPtr(table, tables), RowName=row, ColumnName=column)
                param = unrealsdk.find_class("GbxDataTableFunctionLibrary").ClassDefaultObject.GetDataTableValueFromHandle(handle, self.pawn)[0]
                raw = stats._read(param._get_address() + self.PARAM_FLOAT, 4) or bytes(4)  # noqa: SLF001
                bits = struct.unpack("<I", raw)[0]
                # Int columns (Charges_Value: 2) come back as the int itself; as a float that would
                # be a denormal, which no table holds
                value = float(struct.unpack("<f", raw)[0]) if bits & 0x7F800000 else float(struct.unpack("<i", raw)[0])
            except Exception as ex:  # noqa: BLE001
                logging.dev_warning(f"[Kairosdex] table cell {key}: {ex!r}")
            Game.cells[key] = value
        return Game.cells[key]

    def known(self, spec: dict[str, Any]) -> bool:
        return (not spec.get("a") or spec["a"] in Game.constants) and (not spec.get("t") or tuple(spec["t"]) in Game.cells)

    def value(self, spec: dict[str, Any]) -> float:
        """A weight / gate / chance from rolls.json (serialgen.Resolve)."""
        return serialgen.product(spec, self.constant, self.cell)


def _changed() -> None:
    S.version += 1
    if S.on_change:
        S.on_change()


def request(key: str, count: int = 1, *, picture: bool = True) -> None:
    """Queues `count` rolls of an item ("<inv>.<comp>"). picture=False: no picture (hover rolls)."""
    if "." not in key:
        return
    if picture:
        S.no_picture.discard(key)
    else:
        S.no_picture.add(key)
    count = max(1, min(MAX_ROLLS, count))
    S.queue = [q for q in S.queue if q[0] != key] + [(key, count)]
    S.status[key] = "rolling"
    _changed()


def clear(key: str) -> None:
    S.results.pop(key, None)
    S.status.pop(key, None)
    S.pictures.pop(key, None)
    _changed()


def state(key: str | None) -> dict[str, Any] | None:
    """What the page shows for one item: its rolls and status."""
    if not key or (key not in S.results and key not in S.status):
        return None
    rolls = S.results.get(key, [])
    # Only the latest roll's card goes to the page (a few KB each)
    shown = [{k: v for k, v in r.items() if k != "card"} for r in rolls[:-1]] + rolls[-1:]
    # (no global version in it: a hover roll of another item would make the page redraw everything)
    # The latest roll's picture: the game's render target, while the thumbnail manager still has it
    picture = S.pictures.get(key)
    from . import thumbs  # noqa: PLC0415 - it imports this module

    picture = thumbs.follow(picture)
    path = picture["path"] if picture else None
    return {"item": key, "status": S.status.get(key, ""), "rolls": shown, "picture": path}


def drop_hover_requests(keep: str | None) -> None:
    """Hover rolls not started yet for items the mouse has left (sweeping down the list)."""
    dropped = [k for k, _ in S.queue if k in S.no_picture and k != keep and k != S.current]
    if dropped:
        S.queue = [q for q in S.queue if q[0] not in dropped]
        for k in dropped:
            S.status.pop(k, None)
        _changed()


def hover_state(key: str | None) -> dict[str, Any] | None:
    """The hovered item's latest card, for the page's hover card."""
    if not key:
        return None
    rolls = S.results.get(key) or []
    return {"item": key, "card": rolls[-1].get("card") if rolls else None, "status": S.status.get(key, "")}


def _fail(key: str, text: str) -> None:
    logging.dev_warning(f"[Kairosdex] roll {key}: {text}")
    S.status[key] = text
    S.queue = [q for q in S.queue if q[0] != key]
    S.current = None
    _changed()


def _result(key: str, item: dict[str, Any], ident: int, values: dict[str, float], game: Game, level: int | None,
            parts: list[tuple[int, int | str]]) -> tuple[dict[str, Any], list[tuple[int | str, int]]]:  # fmt: skip
    """A roll as the page shows it (card lines, effect numbers, variants, skills, part lines, card), and its parts."""
    inv = key.partition(".")[0]
    got = stats.comp_index(ident)
    indices = stats.part_indices(ident)
    # A part with no serial index (a class mod's special stat) reads -1: stats.json keys it by the
    # text the serial carries ("classmod.statspecial_ricochet_damage"); ours, in order
    texts = iter(index.lower() for _, index in parts if isinstance(index, str))
    owners = [(next(texts, index) if index == -1 else index, table) for index, table in stats.part_owners(ident)]
    values = {**stats.part_flags(inv, indices), **values}
    # Gear: its modifiers (item type, composition, each part) give the values its container doesn't hold
    mods = stats.gear_mods(inv, got, owners)
    S.last_values = values  # for checks through the bridge
    result = {
        "lines": stats.card_lines(inv, values, mods, game),
        "fills": stats.fill_args(inv, values, item.get("args") or {}, mods, game),
        # Which of the item's variant texts (Tediore throw types, ...) this roll shows
        "variants": stats.passing(inv, values, [v["if"] for v in item.get("variants") or []], mods, game),
        # A class mod's skills: {"<graph>,<node>": points} from its skill parts
        "skills": stats.classmod_skills(inv, indices),
        "elements": stats.elements_of(values),
        # Lines its type and parts add to the card (perks, licensed parts, augments)
        "parts": stats.part_lines(inv, owners, values, mods, game),
    }
    # The game's own item card for it (card.py; the page shows it in the game's card widget)
    result["card"] = card.model(key, item, result, level, (level_fills(key) or {}).get("fills"))
    return result, owners


def new_serial(key: str, pc: Any) -> tuple[str, list[tuple[int, int]]] | None:
    """
    A fresh serial of an item at the player's level (thumbs.py's bake uses this too), or None to
    call again next tick. An item's first roll takes three ticks so none is long: its weights and
    gates looked up from the game (LOOKUP_BUDGET_S at a time), its parts prepared, then the roll.
    Game thread.
    """
    game = Game(pc.Pawn)
    level = max(1, int(game.attr("pawn_experience_level") or 1))
    prepared = Game.prepared.get((key, level))
    if prepared is not None:
        return serialgen.roll(prepared, level, S.rng)
    start = time.perf_counter()
    looked = False
    for spec in serialgen.value_specs(key):
        if not game.known(spec):
            if looked and time.perf_counter() - start > LOOKUP_BUDGET_S:
                return None
            game.value(spec)
            looked = True
    if not looked:
        Game.prepared[(key, level)] = serialgen.Prepared(key, level, game.value)
        while len(Game.prepared) > KEEP_ITEMS:
            Game.prepared.pop(next(iter(Game.prepared)))
    return None


def roll_now(key: str, pc: Any) -> dict[str, Any] | str | None:
    """
    One roll of an item: its result, an error to show, or None to try again next tick (still
    looking up its weights). Game thread.
    """
    if not serialgen.has(key):
        return "This item can't be rolled (no rules for it)."
    if not stats.resolve_defs(pc):
        return "Couldn't read item stats in this game version."  # (warm() got it ready)
    try:
        source = identity.source(pc)
    except identity.Unavailable as ex:
        return f"{str(ex)[:1].upper()}{str(ex)[1:]}."
    rolled = new_serial(key, pc)
    if rolled is None:
        return None
    serial, parts = rolled
    item = data.dex()["items"].get(key) or {}
    try:
        ident = identity.Identity(source)
    except identity.Unavailable as ex:
        return f"Rolling is off for this game version ({ex})."
    try:
        if not ident.load(serial):
            logging.dev_warning(f"[Kairosdex] the game didn't read our roll of {key}: {serial}")
            return "The game didn't accept the rolled item."
        if not identity.check_parts(ident.address, parts[0][1], parts[1:]):
            logging.dev_warning(f"[Kairosdex] the game read our roll of {key} differently: {serial}")
            return "The game read the roll differently."
        values = stats.read_values(ident.address)
        if values is None:
            return "The roll had no stats."
        level = Game(pc.Pawn).attr("pawn_experience_level")
        result, owners = _result(key, item, ident.address, values, Game(pc.Pawn), int(level) if level else None, parts)
    finally:
        ident.destroy()
    S.history = [*S.history, (key, serial, S.last_values, owners)][-10:]
    result["serial"] = serial
    return result


class _Warm:
    step = 0


def warm(pc: Any) -> bool:
    """
    One-time work before the first roll, one piece per tick so no tick goes over a few ms: the
    stats data (~3 ms), the roll rules (~1 ms), the attribute definitions (2 ms at a time).
    """
    if _Warm.step == 0:
        stats.data()
    elif _Warm.step == 1:
        serialgen.has("")
    elif not stats.resolve_defs(pc, budget_s=LOOKUP_BUDGET_S):
        return stats._Defs.todo is None  # noqa: SLF001 - finished, but found nothing: roll_now says why
    _Warm.step = min(_Warm.step + 1, 2)
    return _Warm.step == 2 and stats._Defs.resolved  # noqa: SLF001


def tick() -> None:
    from . import thumbs  # noqa: PLC0415 - it imports this module

    if thumbs.Shots.pending:
        for key, picture in thumbs.tick_shots():
            S.pictures[key] = picture
            _changed()
    if not S.queue:
        S.current = None
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        return
    if S.pc is None or S.pc() is not pc:
        # New map / controller: attribute lookups were made against the old pawn
        S.pc = WeakPointer(pc)
        Game.constants.clear()
        Game.prepared.clear()
    key = S.queue[0][0]
    S.current = key
    if not warm(pc):
        return
    started = time.perf_counter()
    try:
        result = roll_now(key, pc)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Kairosdex] roll {key}: {ex!r}")
        result = f"Rolling failed ({ex})."
    if result is None:
        return  # weights still being looked up
    if isinstance(result, str):
        _fail(key, result)
        return
    S.latency = [*S.latency, round((time.perf_counter() - started) * 1000, 2)][-20:]
    # Its inventory picture: the game renders it from the serial (thumbs.py)
    if key not in S.no_picture:
        thumbs.shoot(key, result["serial"])
    rolls = S.results.setdefault(key, [])
    rolls.append(result)
    del rolls[:-MAX_ROLLS]
    while len(S.results) > KEEP_ITEMS:
        S.results.pop(next(iter(S.results)))
    S.current = None
    left = [(k, n - 1) for k, n in S.queue if k == key]
    S.queue = [q for q in S.queue if q[0] != key] + ([(key, left[0][1])] if left and left[0][1] > 0 else [])
    if not any(k == key for k, _ in S.queue):
        S.status[key] = ""
    _changed()


def stop() -> None:
    """Drops everything (mod disabled or reloaded). Nothing in the world to clean up."""
    S.queue.clear()
    S.current = None


class _Level:
    key: str | None = None
    pc: WeakPointer | None = None
    fills: dict[str, str] = {}  # noqa: RUF012
    MISSING = -1.0e30


def level_fills(key: str | None) -> dict[str, Any] | None:
    """
    Numbers for an item's effect texts that don't depend on the roll (balance formulas and the like,
    marked "game" in dex.json): the game evaluates them against the player, i.e. at their level.
    Cached per item and player controller. Game thread.
    """
    if not key:
        return None
    item = data.dex()["items"].get(key) or {}
    specs = {name: spec for name, spec in (item.get("args") or {}).items() if spec.get("game")}
    if not specs:
        return None
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        return None
    if _Level.key != key or _Level.pc is None or _Level.pc() is not pc:
        lib = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject
        fills = {}
        for name, spec in specs.items():
            try:
                value = float(lib.GetValueOfAttribute(spec["attr"], pc.Pawn, _Level.MISSING))
            except Exception:  # noqa: BLE001
                continue
            # 0 here means "not something the player has" (a grenade's own pellets): leave it out;
            # so is a value that isn't a number (Undershield's formula gave infinity, 2026-10-24)
            if value != _Level.MISSING and value != 0 and math.isfinite(value):
                fills[name] = stats._number(value, spec)  # noqa: SLF001
        _Level.key, _Level.pc, _Level.fills = key, WeakPointer(pc), fills
    return {"item": key, "fills": _Level.fills}
