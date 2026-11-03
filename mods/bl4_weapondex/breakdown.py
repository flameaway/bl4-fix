"""
The parts screen (click a legendary's picture in the dex, or a Collection entry's): every part of
the shown roll (or the exact item had), and what each one does to the item, good and bad.

Measured, not guessed: for each part we take the roll's serial without that one part, let the
game's own decoder build that item (identity.py, the same path as roll.py, nothing spawned), and
compare its card with the full roll's. What changes is that part's doing: a stat up or down (with
the game's card numbers and icons), a card line it adds or takes away (a licensed part's perk, the
legendary effect), the element it sets, a class mod's skill points. The seed doesn't change any
stat (checked 2026-11-02 on Stray and Slugger at seeds 1 / 2 / 3000), so the serials only differ
in that part.

A part the item can't do without (its barrel or body: without it most of its numbers read 0) is a
"core" part: its numbers aren't a bonus on top of anything, they're the item's base.

Part names and the game's own note on a part ("Damage mod", "ADS prof mod") come from
partnames.json (tools/gen_weapondex_rolls.py).

Each part's picture: as soon as a part is read, the game's thumbnail manager is asked to render a
serial holding only the composition and that one part (thumbs.shoot, the same request the
inventory's pictures use), so the page shows the part on its own. The game renders ~4 a second, so
asking while the rest are still being read overlaps the two. One that doesn't come in
PICTURE_WAIT_S shows no picture.

Everything is read at the roll's own level (from its serial): its stats, and the roll chances (the
level gates which parts can roll).

Work is spread over frames, several decodes per frame (~1 ms each), with the roll chances simulated
alongside. Game thread.
"""

from __future__ import annotations

import json
import math
import time
from functools import cache
from importlib import resources
from typing import Any

from mods_base import get_pc
from unrealsdk import logging

from . import data, identity, itemkey, roll, serialgen, stats

BUDGET_S = 0.010  # per frame (the dex is open over the pause menu)
SEED = 1  # any seed: it doesn't change stats
# A card line where a lower number is the better one
LOWER_BETTER = {
    "uistat_repair_time", "uistat_reload_speed", "uistat_gadget_cooldown", "uistat_repkit_cooldown",
    "uistat_shield_regen_delay", "uistat_ammo_per_shot",
}  # fmt: skip
# Without the part, at least this share of the card's numbers fall to nothing: a core part
CORE_SHARE = 0.5
PICTURE_PREFIX = "part:"  # thumbs.Shots keys of part pictures (roll.tick hands them to picture_done)
PICTURE_WAIT_S = 16.0
# Roll chances: this many rolls by the game's rules (serialgen.generate, the same as "Roll one"),
# made a few at a time per frame. A part's chance is the share of rolls that have it.
SIM_ROLLS = 4000
SIM_BUDGET_S = 0.004
ALT_SHOWN = 20  # most alternatives listed (the likeliest; a class mod's skill slot has hundreds)


class S:
    key: str | None = None  # the item while the screen is open
    own: str | None = None  # the Collection entry broken down (None: the item's latest roll)
    key_done: str | None = None  # the item `done` is about (kept after closing)
    serial: str | None = None  # the roll broken down
    parts: list[tuple[int, int | str]] = []  # noqa: RUF012 - its parts, the composition first
    full: dict[str, Any] | None = None  # the whole roll's reading
    done: list[dict[str, Any]] = []  # noqa: RUF012 - one entry per part so far
    status = ""  # "" / "working" / an error to show
    level = 1  # the roll's level (its serial's): the parts are read and their chances rolled at it
    pictures: dict[int, dict[str, Any]] = {}  # noqa: RUF012 - part position -> its picture (thumbs.tick_shots)
    pictures_at = 0.0  # when the last one was asked for (0: not yet)
    no_pictures = False  # the manager can't take them here (the bake has it, or this build can't)
    matched = False  # text parts given the table the item's rules have them in (_match_texts)
    # Roll chances (simulated): (table, index) -> rolls that had it, out of sim_n
    sim: dict[tuple[int, int | str], int] = {}  # noqa: RUF012
    sim_n = 0
    sim_for: tuple[str, int] | None = None  # (item, level)
    # Exact chances, worked out (not simulated) for the slots no other part's tags affect: the
    # simulation's noise showed equal parts as 37% and 39% (see _exact)
    exact: dict[tuple[int, int | str], float] = {}  # noqa: RUF012
    exact_for: tuple[str, int] | None = None
    # The slot whose alternatives the page shows (its drop-down under a part): its position
    slot_at: int | None = None
    alt_pictures: dict[str, dict[str, Any]] = {}  # noqa: RUF012 - "<table>_<index>" -> its picture alone
    # What each alternative does (the roll with it in place of the slot's part, against the roll
    # without that part, like the rolled parts): "<table>_<index>" -> _compare's dict, or None if
    # the game won't take it with this roll's other parts
    alt_effects: dict[str, dict[str, Any] | None] = {}  # noqa: RUF012
    alt_without: dict[str, Any] | None = None  # the roll without the slot's part (read once per slot)
    alt_slot_at: int | None = None  # the slot alt_effects and alt_without are about
    alts_pending = False  # some of the open slot's parts may be left to read (_read_alts)
    version = 0
    on_change: Any = None


@cache
def _names() -> dict[str, dict[str, list[str]]]:
    try:
        return json.loads((resources.files(__package__) / "partnames.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _changed() -> None:
    S.version += 1
    if S.on_change:
        S.on_change()


def request(key: str, serial: str | None = None, own: str | None = None) -> None:
    """
    Break down the latest roll of `key` (the one the details show), or with `own`, the Collection
    entry's exact item (its `serial`).
    """
    if own is None:
        rolls = roll.S.results.get(key) or []
        serial = rolls[-1].get("serial") if rolls else None
    if serial and serial == S.serial and key == S.key_done and S.status != "working" and S.done:
        S.key, S.own = key, own  # already done for this roll: just show it again
        _changed()
        return
    info = itemkey.read(serial) if serial else None
    S.key = key
    S.own = own
    S.key_done = key
    S.serial = serial
    # Our own rolls are remembered as rolled; anything else is read from its serial
    S.parts = list(roll.S.rolled.get(serial) or (info or {}).get("parts") or []) if serial else []
    S.level = max(1, int((info or {}).get("level") or 1))
    S.full = None
    S.done = []
    S.pictures = {}
    S.pictures_at = 0.0
    S.no_pictures = False
    S.matched = False
    S.slot_at = None
    S.alt_pictures = {}
    S.alt_effects = {}
    S.alt_without = None
    S.alt_slot_at = None
    S.status = "working" if S.parts else ("No roll to look at yet." if not serial else "This item's parts couldn't be read.")
    _changed()


def close() -> None:
    if S.status == "working":
        # Stopped half way: start over next time
        S.status = ""
        S.serial = None
        S.done = []
    S.key = None
    _changed()


def state() -> dict[str, Any] | None:
    if S.key is None:
        return None
    # A new roll of the item while the screen is open: break that one down instead
    rolls = (roll.S.results.get(S.key) or []) if S.own is None else []
    if rolls and rolls[-1].get("serial") != S.serial and S.status != "working":
        request(S.key)
    from . import thumbs  # noqa: PLC0415 - it imports roll, which imports this module

    def path(picture: dict[str, Any] | None) -> str | None:
        shot = thumbs.follow(picture)
        return shot["path"] if shot else None

    chances = _chances_ready()
    parts = []
    prepared = _prepared()
    for i, part in enumerate(S.done, start=1):
        n = _slot_of(S.parts[i])
        # Its slot (the page groups the parts of a slot that picks several) and how many it picks
        picks = list(prepared.slots[n][:2]) if prepared is not None and n is not None else None
        parts.append({**part, "picture": path(S.pictures.get(i)), "chance": _chance(S.parts[i], chances),
                      "has_slot": n is not None, "slot_n": n, "picks": picks})  # fmt: skip
    waiting = bool(S.pictures_at) and time.monotonic() - S.pictures_at < PICTURE_WAIT_S and len(S.pictures) < len(S.done)
    return {"item": S.key, "own": S.own, "serial": S.serial, "status": S.status, "parts": parts,
            "count": max(0, len(S.parts) - 1), "pictures": "waiting" if waiting or (S.status == "working") else "done",
            "chances": "done" if chances else "working", "rolls": SIM_ROLLS, "slot": _slot_state()}  # fmt: skip


def picture_done(key: str, picture: dict[str, Any]) -> None:
    """
    A picture came in (roll.tick: thumbs.tick_shots). Keys "part:<serial>:<tag>": <position> a part
    alone, a<table>_<index> a slot's alternative alone.
    """
    serial, _, tag = key.removeprefix(PICTURE_PREFIX).rpartition(":")  # (serials have no ":")
    if serial != S.serial:
        return
    if tag.isdigit():
        S.pictures[int(tag)] = picture
    elif tag[:1] == "a":
        S.alt_pictures[tag[1:]] = picture
    else:
        return
    _changed()


def _shoot(tag: str, parts: list[tuple[int, int | str]]) -> bool:
    from . import thumbs  # noqa: PLC0415

    return thumbs.shoot(f"{PICTURE_PREFIX}{S.serial}:{tag}", serialgen.serial(S.parts[0][0], S.level, SEED, parts))


# region Roll chances and alternatives


def _prepared() -> Any:
    return roll.Game.prepared.get((S.key, S.level)) if S.key else None


def _chances_ready() -> bool:
    return S.sim_n >= SIM_ROLLS and S.sim_for == (S.key, S.level)


EXACT_STATES = 2000  # most sets of picks worked through for one slot's exact chances (else simulated; game thread)


def _exact_chances() -> dict[tuple[int, int | str], float]:
    """
    The chance a roll has each part, exactly, for the slots whose picks don't depend on the roll's
    other parts (no part in them needs or rules out a tag, none counts toward a cap) and whose
    parts no other slot has. Picks as serialgen.generate makes them: how many (lo..hi, evenly or by
    `chance`), then one at a time by weight among those not picked yet.
    """
    prepared = _prepared()
    if prepared is None:
        return {}
    if S.exact_for == (S.key, S.level):
        return S.exact
    seen: dict[tuple[int, int | str], int] = {}
    for _, _, _, pool in prepared.slots:
        for c in pool:
            seen[(c[0], c[1])] = seen.get((c[0], c[1]), 0) + 1
    caps = 0
    for cap, _ in prepared.caps:
        caps |= cap
    out: dict[tuple[int, int | str], float] = {}
    for lo, hi, chance, pool in prepared.slots:
        if not pool or any(c[3] or c[4] or c[2] & caps or seen[(c[0], c[1])] > 1 for c in pool):
            continue
        weights = [max(0.0, float(c[5])) for c in pool]
        if not any(weights):
            continue
        n = len(pool)
        hi = max(lo, hi)
        if sum(math.comb(n, j) for j in range(1, min(hi, n) + 1)) > EXACT_STATES:
            continue  # too many (a class mod's skill slot): simulated
        # How many it picks: P(want = k)
        if chance is None:
            want = {k: 1 / (hi - lo + 1) for k in range(lo, hi + 1)}
        else:
            want = {lo + j: chance**j * (1 - chance) for j in range(hi - lo)}
            want[hi] = chance ** (hi - lo)
        # The picks are the same whatever the count: after j picks, the chance each part is in
        # (sets of picks as bit masks; a set with nothing left to pick stays as it is)
        level = {0: 1.0}
        inc = [[0.0] * n]
        for _ in range(hi):
            nxt: dict[int, float] = {}
            for mask, p in level.items():
                left = sum(weights[i] for i in range(n) if not mask >> i & 1)
                if left <= 0:
                    nxt[mask] = nxt.get(mask, 0.0) + p
                    continue
                for i in range(n):
                    if not mask >> i & 1 and weights[i]:
                        m = mask | 1 << i
                        nxt[m] = nxt.get(m, 0.0) + p * weights[i] / left
            level = nxt
            inc.append([sum(p for mask, p in level.items() if mask >> i & 1) for i in range(n)])
        has = [sum(w * inc[min(k, len(inc) - 1)][i] for k, w in want.items()) for i in range(n)]
        for i, c in enumerate(pool):
            out[(c[0], c[1])] = min(1.0, has[i])
    S.exact, S.exact_for = out, (S.key, S.level)
    return out


def _chance(part: tuple[int, int | str], chances: bool) -> float | None:
    """A part's chance per roll: exact where it can be worked out, else simulated (None: not yet)."""
    exact = _exact_chances()
    if part in exact:
        return exact[part]
    return S.sim.get(part, 0) / S.sim_n if chances else None


def _slot_of(part: tuple[int, int | str]) -> int | None:
    """Which of the item's slots a rolled part came from (its position in serialgen.Prepared.slots)."""
    prepared = _prepared()
    if prepared is None:
        return None
    for n, (_, _, _, pool) in enumerate(prepared.slots):
        if any(c[0] == part[0] and c[1] == part[1] for c in pool):
            return n
    return None


def open_slot(at: int | None) -> None:
    """
    The page opens a part's drop-down: every part its slot can roll there (their pictures asked
    for, what each does read over the next frames: tick), or None to close it.
    """
    S.slot_at = at if at is not None and 1 <= at < len(S.parts) else None
    S.alts_pending = S.slot_at is not None
    if S.slot_at != S.alt_slot_at:
        S.alt_slot_at = S.slot_at
        S.alt_effects = {}
        S.alt_without = None
    for c in _slot_list()[1]:
        tag = f"{c['table']}_{str(c['index']).lower()}"
        if tag not in S.alt_pictures:
            _shoot(f"a{tag}", [S.parts[0], (c["table"], c["index"])])
    _changed()


def _slot_list() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The open slot: (its header, or None; the parts it can roll, the likeliest first)."""
    if S.slot_at is None or S.slot_at >= len(S.parts):
        return None, []
    prepared = _prepared()
    n = _slot_of(S.parts[S.slot_at])
    if prepared is None or n is None:
        return {"at": S.slot_at, "slot": "", "picks": "", "status": "This part's slot isn't known (roll it again)."}, []
    lo, hi, _, pool = prepared.slots[n]
    rolled = set(S.parts[1:])
    total = sum(c[5] for c in pool) or 1.0
    chances = _chances_ready()
    out = []
    inv = (S.key or "").partition(".")[0]
    passives = stats.data().get("classmod_passives", {}).get(inv) or {}
    skills = {f"{x['graph']},{x['node']}": x for x in (data.dex()["items"].get(S.key or "") or {}).get("skills") or []}
    for table, index, _, _, _, weight in pool:
        found = _names().get(str(table), {}).get(str(index).lower()) or ["Part", f"Part {index}", "", ""]
        slot, part_name, note, maker = [*found, "", "", ""][:4]
        passive = passives.get(str(index)) if table == S.parts[0][0] else None
        if passive and (skill := skills.get(f"{passive[0]},{passive[1]}")):
            part_name = f"+{passive[2]} {skill.get('name', part_name)}"  # a class mod skill part: its skill
        out.append({"table": table, "index": index, "name": part_name, "slot": slot, "maker": maker, "note": note,
                    "weight": weight / total, "chance": _chance((table, index), chances),
                    "rolled": (table, index) in rolled})  # fmt: skip
    out.sort(key=lambda c: (-(c["chance"] if c["chance"] is not None else c["weight"]), c["name"]))
    label = next((c["slot"] for c in out if c["rolled"]), out[0]["slot"] if out else "")
    picks = f"{lo}" if lo == hi else f"{lo} to {hi}"
    # Once the chances are in: only the parts that can roll on this item (tags rule the rest out),
    # the likeliest ALT_SHOWN, and the ones in this roll always
    never = 0
    known = all(c["chance"] is not None for c in out)
    if known:
        never = sum(1 for c in out if not c["chance"])
        out = [c for c in out if c["chance"]]
    more = max(0, len(out) - ALT_SHOWN)
    out = [c for i, c in enumerate(out) if i < ALT_SHOWN or c["rolled"]]
    head = {"at": S.slot_at, "slot": label, "picks": picks, "more": more, "never": never, "known": known, "status": ""}
    return head, out


def _slot_state() -> dict[str, Any] | None:
    from . import thumbs  # noqa: PLC0415

    head, out = _slot_list()
    if head is None:
        return None
    for c in out:
        tag = f"{c['table']}_{str(c['index']).lower()}"
        picture = thumbs.follow(S.alt_pictures.get(tag))
        c["picture"] = picture["path"] if picture else None
        # What it does: a part in this roll as its own row has it; another as read (tick)
        if c["rolled"]:
            at = S.parts.index((c["table"], c["index"]))
            c["effect"] = S.done[at - 1] if at - 1 < len(S.done) else None
            c["effect_status"] = "done" if c["effect"] else "working"
        elif tag in S.alt_effects:
            c["effect"] = S.alt_effects[tag]
            c["effect_status"] = "done" if c["effect"] else "bad"
        else:
            c["effect"] = None
            c["effect_status"] = "working"
    return {**head, "parts": out}


def _read_alts(pc: Any, game: roll.Game, started: float) -> bool:
    """
    Reads what the open slot's other parts do, within this frame's budget: the roll with one in
    place of the slot's part, against the roll without that part. True if any is left to read.
    """
    if S.slot_at is None or S.full is None or S.status == "working":
        return False
    head, out = _slot_list()
    todo = [c for c in out if not c["rolled"] and f"{c['table']}_{str(c['index']).lower()}" not in S.alt_effects]
    if head is None or not todo:
        S.alts_pending = False
        return False
    at = S.slot_at
    if S.alt_without is None:
        S.alt_without = _read(S.key, S.parts[:at] + S.parts[at + 1 :], pc, S.level, game) or {}
    while todo and time.perf_counter() - started < BUDGET_S:
        c = todo.pop(0)
        part = (c["table"], c["index"])
        tag = f"{c['table']}_{str(c['index']).lower()}"
        swapped = _read(S.key, S.parts[:at] + [part] + S.parts[at + 1 :], pc, S.level, game)
        if swapped is None:
            S.alt_effects[tag] = None
            continue
        S.alt_effects[tag] = _compare(S.key, part, swapped, S.alt_without or None)
    S.alts_pending = bool(todo)
    _changed()
    return bool(todo)


def _simulate(budget_s: float) -> None:
    """Rolls the item by the game's rules (parts only: no serial, no decode) and counts the parts."""
    prepared = _prepared()
    if prepared is None:
        return
    if S.sim_for != (S.key, S.level):
        S.sim_for, S.sim, S.sim_n = (S.key, S.level), {}, 0
    started = time.perf_counter()
    rng = roll.S.rng
    while S.sim_n < SIM_ROLLS and time.perf_counter() - started < budget_s:
        for part in set(serialgen.generate(prepared, rng)[1:]):
            S.sim[part] = S.sim.get(part, 0) + 1
        S.sim_n += 1
    if S.sim_n >= SIM_ROLLS:
        S.alts_pending = S.slot_at is not None  # the open list may show others now
        _changed()


def _simulating() -> bool:
    return S.key is not None and bool(S.parts) and not _chances_ready() and _prepared() is not None


def _prepare(pc: Any) -> None:
    """
    The item's rules at the roll's level, for the chances and a slot's alternatives: made the way a
    roll makes them (roll.new_serial, a few ticks) when no roll at that level has (a Collection
    item, a level up since the roll, or forgotten past roll.KEEP_ITEMS).
    """
    if S.key is None or _prepared() is not None or not serialgen.has(S.key):
        return
    try:
        roll.new_serial(S.key, pc, level=S.level)
    except Exception as ex:  # noqa: BLE001 - the chances stay out, the rest works
        logging.dev_warning(f"[Kairosdex] couldn't prepare {S.key} at level {S.level} for its chances: {ex!r}")


def _match_texts() -> None:
    """
    A part written as text read from a serial comes as (item type, text); the item's rules (and so
    the simulated rolls) have it under its own table and spelling. Matched once the rules are in.
    """
    prepared = _prepared()
    if S.matched or prepared is None:
        return
    S.matched = True
    known = {str(c[1]).lower(): (c[0], c[1]) for _, _, _, pool in prepared.slots for c in pool if isinstance(c[1], str)}
    S.parts = [known.get(index.lower(), (table, index)) if isinstance(index, str) else (table, index) for table, index in S.parts]


# endregion


def _ask_picture(i: int) -> None:
    """Part i's own picture, asked as soon as it's read (the game renders ~4 a second, in the order asked)."""
    # A shared part (an element, secondary ammo) has no model of its own: rendered alone, the
    # thumbnail scene showed a close-up of the gun's material (seen 2026-11-02). The page shows its
    # icon instead.
    if S.no_pictures or i in S.pictures or not S.done[i - 1].get("maker"):
        return
    S.pictures_at = time.monotonic()
    if not _shoot(str(i), [S.parts[0], S.parts[i]]):
        S.no_pictures = True  # no pictures here (the bake has the manager, or this build can't)


# region Reading one variant


def _read(key: str, parts: list[tuple[int, int | str]], pc: Any, level: int, game: roll.Game) -> dict[str, Any] | None:
    """The card of the item made of exactly these parts, the way roll._result reads one; None if the game won't take it."""
    inv = key.partition(".")[0]
    item = data.dex()["items"].get(key) or {}
    serial = serialgen.serial(parts[0][0], level, SEED, parts)
    ident = identity.Identity(identity.source(pc))
    try:
        if not ident.load(serial):
            return None
        values = stats.read_values(ident.address)
        if values is None:
            return None
        indices = stats.part_indices(ident.address)
        texts = iter(index.lower() for _, index in parts if isinstance(index, str))
        owners = [(next(texts, index) if index == -1 else index, table) for index, table in stats.part_owners(ident.address)]
        values = {**stats.part_flags(inv, indices), **values}
        mods = stats.gear_mods(inv, stats.comp_index(ident.address), owners)
        return {
            "lines": {line["key"]: line for line in stats.card_lines(inv, values, mods, game)},
            "texts": [line for line in stats.part_lines(inv, owners, values, mods, game)],
            "skills": stats.classmod_skills(inv, indices) or {},
            "elements": stats.elements_of(values),
            "variants": stats.passing(inv, values, [v["if"] for v in item.get("variants") or []], mods, game),
            "fills": stats.fill_args(inv, values, item.get("args") or {}, mods, game),
        }
    finally:
        ident.destroy()


def _better(key: str, delta: float) -> bool:
    return delta < 0 if key in LOWER_BETTER else delta > 0


def _compare(key: str, part: tuple[int, int | str], full: dict[str, Any], without: dict[str, Any] | None) -> dict[str, Any]:
    """What one part does: the full roll compared with the roll without it."""
    table, index = part
    found = _names().get(str(table), {}).get(str(index).lower()) or ["Part", f"Part {index}", "", ""]
    slot, name, note, maker = [*found, "", "", ""][:4]
    out: dict[str, Any] = {"table": table, "index": index, "slot": slot, "name": name, "note": note, "maker": maker,
                           "core": False, "stats": [], "added": [], "removed": [], "changed": [], "skills": [], "elements": [],
                           "variants": []}  # fmt: skip
    item = data.dex()["items"].get(key) or {}
    own = bool(S.parts) and table == S.parts[0][0]
    if own and (passive := stats.data().get("classmod_passives", {}).get(key.partition(".")[0], {}).get(str(index))):
        # A class mod skill part: name it after its skill
        named = {f"{s['graph']},{s['node']}": s for s in item.get("skills") or []}
        skill = named.get(f"{passive[0]},{passive[1]}")
        if skill:
            out["name"] = skill.get("name", out["name"])
    if without is None:
        out["core"] = True
        return out
    lines, gone = full["lines"], without["lines"]
    numeric = [k for k, line in lines.items() if line["value"]]
    lost = [k for k in numeric if k not in gone or not gone[k]["value"]]
    if numeric and len(lost) >= max(1, len(numeric) * CORE_SHARE):
        # The item's base numbers come from it: list those it sets
        out["core"] = True
        out["stats"] = [_stat(lines[k], None, "base") for k in lost]
    for k, line in lines.items():
        other = gone.get(k)
        if other is None:
            if not out["core"]:
                out["stats"].append(_stat(line, None, "added"))
        elif other["text"] != line["text"] and not out["core"]:
            out["stats"].append(_stat(line, other, None))
    if not out["core"]:
        for k, other in gone.items():
            if k not in lines:
                out["stats"].append(_stat(other, None, "removed"))
    # Card texts (perks, licensed parts, augments, the legendary effect) by their line: one the part
    # brings, one it takes away, one whose numbers it changes
    texts = {t["key"]: t for t in full["texts"]}
    texts_without = {t["key"]: t for t in without["texts"]}
    out["added"] = [{"text": t["text"], "icon": t.get("icon", "")} for k, t in texts.items() if k not in texts_without]
    out["removed"] = [{"text": t["text"], "icon": t.get("icon", "")} for k, t in texts_without.items() if k not in texts]
    out["changed"] = [{"text": t["text"], "was": texts_without[k]["text"], "icon": t.get("icon", "")}
                      for k, t in texts.items() if k in texts_without and texts_without[k]["text"] != t["text"]]  # fmt: skip
    for ref, points in full["skills"].items():
        diff = points - without["skills"].get(ref, 0)
        if diff:
            named = {f"{s['graph']},{s['node']}": s for s in item.get("skills") or []}
            skill = named.get(ref, {})
            out["skills"].append({"name": skill.get("name", ref), "points": diff, "icon": skill.get("icon", ""), "color": skill.get("color", "")})
    if full["elements"] != without["elements"]:
        out["elements"] = [e for e in full["elements"] if e not in without["elements"]] or list(full["elements"])
    variants = item.get("variants") or []
    out["variants"] = [variants[i]["text"] for i in full["variants"] if i not in without["variants"] and i < len(variants)]
    out["variants"] = [_filled(t, full["fills"]) for t in out["variants"]]
    return out


def _filled(text: str, fills: dict[str, str]) -> str:
    from .card import _filled as fill  # noqa: PLC0415

    return fill(text, fills)


def _stat(line: dict[str, Any], other: dict[str, Any] | None, change: str | None) -> dict[str, Any]:
    """A card line the part changes: with it (`line`) and without it (`other`)."""
    label = (line.get("label") or "").rstrip(":") or line["key"].removeprefix("uistat_").replace("_", " ").title()
    out = {"key": line["key"], "label": label, "icon": line.get("icon") or "", "to": line["text"],
           "from": other["text"] if other else "", "change": change or "", "good": None, "pct": ""}  # fmt: skip
    if change == "removed":
        out["to"], out["from"] = "", line["text"]
        return out
    if other is not None:
        delta = line["value"] - other["value"]
        if delta:
            out["good"] = _better(line["key"], delta)
            if other["value"]:
                pct = delta / abs(other["value"]) * 100
                out["pct"] = f"{pct:+.0f}%" if abs(pct) >= 1 else f"{pct:+.1f}%"
    return out


# endregion


def tick() -> None:
    if S.key is None:
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        return
    _prepare(pc)
    _match_texts()
    # The chances roll alongside the reading (they need only the item's rules)
    if _simulating():
        _simulate(SIM_BUDGET_S)
    if S.status != "working":
        # The roll's own parts are read: what the open drop-down's other parts do
        if _alts_left():
            try:
                _read_alts(pc, roll.Game(pc.Pawn), time.perf_counter())
            except Exception as ex:  # noqa: BLE001
                logging.error(f"[Kairosdex] other parts of {S.key}: {ex!r}")
                S.slot_at = None
                _changed()
        return
    # The stat definitions (a roll or a Collection card resolves them, but the parts screen can come first)
    if not stats.resolve_defs(pc):
        S.status = "Couldn't read item stats in this game version."
        _changed()
        return
    game = roll.Game(pc.Pawn)
    level = S.level
    started = time.perf_counter()
    try:
        if S.full is None:
            S.full = _read(S.key, S.parts, pc, level, game)
            if S.full is None:
                S.status = "The game didn't accept this roll."
                _changed()
                return
        while len(S.done) < len(S.parts) - 1 and time.perf_counter() - started < BUDGET_S:
            i = len(S.done) + 1
            without = _read(S.key, S.parts[:i] + S.parts[i + 1 :], pc, level, game)
            S.done.append(_compare(S.key, S.parts[i], S.full, without))
            _ask_picture(i)
    except identity.Unavailable as ex:
        S.status = f"{str(ex)[:1].upper()}{str(ex)[1:]}."
        _changed()
        return
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Kairosdex] parts of {S.key}: {ex!r}")
        S.status = f"Couldn't read the parts ({ex})."
        _changed()
        return
    if len(S.done) >= len(S.parts) - 1:
        S.status = ""
    _changed()


def _alts_left() -> bool:
    return S.alts_pending and S.slot_at is not None and S.full is not None and S.status != "working"


def busy() -> bool:
    # (the chances count from before the item's rules are made: _prepare takes a few ticks)
    return (S.status == "working" or (S.key is not None and bool(S.parts) and not _chances_ready() and serialgen.has(S.key))
            or (S.key is not None and _alts_left()))  # fmt: skip
