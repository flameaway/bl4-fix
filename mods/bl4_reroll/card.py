"""
Rerolling from the item card: hover a class mod or enhancement in your backpack and press a key.

- The reroll key rerolls every modifier, after asking (press it again to confirm), or, once one
  is picked, replaces just that one. The card spins first and the item changes once the reels
  land.
- The pick key steps through the item's modifiers: the picked one is marked on the card, and the
  reroll key then replaces it. Stepping past the last one goes back to "all".

The card lives in the game's menu page, the same page as the BL4 Mod Menu, so this borrows the
Mod Menu's link (bl4_mod_menu.extensions): the menu loads ui/card.js into the page, the script
tells us which card is showing (its name, level, rarity, skills and modifier texts: the card has
no item id), and we find the backpack item that matches. No key events reach page scripts, so the keys are
read from Windows here, and only count while the script says a matching card is up. Each reroll
goes back to the script in the state, with the old and new modifiers and some of the other
modifiers each line could have rolled, and the script plays it as a slot machine on the card.
"""

from __future__ import annotations

import random
import re
import sys
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import game, reroll

KEY = "reroll"
UI_DIR = "OakGame/Content/uiresources/bl4reroll"
SCRIPT_URL = "coui://uiresources/bl4reroll/card.js"
UI_FILES = ("card.js", "card.css")
PUBLISH_S = 1.0
# The script repeats what it shows a few times a second; older than this and the card is gone
HOVER_FRESH_S = 1.0
# Names a reel shows before it lands
REEL_NAMES = 12
# "Reroll all?" waits this long for the second press
CONFIRM_S = 5.0
# A hovered class mod / enhancement that matches nothing in the cached backpack list looks at the
# backpack again, at most this often (the list may be behind a pickup)
RECHECK_S = 0.5
# card.js's reel timing (SPIN_MS, STAGGER_MS, ONE_SPIN_MS, LANDED_MS): if the page doesn't say the
# reels landed by then (say, the card was closed), the reroll is made anyway
SPIN_S, STAGGER_S, ONE_SPIN_S, LANDED_S = 1.1, 0.42, 1.5, 0.8
SLACK_S = 2.0

_TAGS = re.compile(r"\[/?\w+\]|<[^>]*>")
_NUMBERS = re.compile(r"[-+]?\d[\d,.]*\s*%?")
_TRAILER = re.compile(r"\b(?:is|are) (?:increased|decreased|reduced) by\b")


def _menu_module(name: str) -> Any:
    return sys.modules.get(f"bl4_mod_menu.{name}")


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    # What the script last said is showing, and the item it is
    hover: dict[str, Any] | None = None
    hover_at = 0.0
    # Index (in the item's lines) of the picked modifier, or None for "all", and whose it is: it
    # stays through rerolls of that item (same handle) and goes when another item is hovered
    pick: int | None = None
    pick_handle: int | None = None
    # Asking "reroll all?" for this item (handle) until this time
    confirm_handle: int | None = None
    confirm_until = 0.0
    items_cache: list[dict[str, Any]] | None = None
    items_src: list[game.Slot] | None = None  # the game.cached_items() list items_cache was made from
    roll: dict[str, Any] = {"id": 0}  # noqa: RUF012
    keys_down: dict[str, bool] = {}  # noqa: RUF012
    # A roll the card is playing: made once its reels land (or after `due`, if the card went away)
    pending: dict[str, Any] | None = None
    # The mod's own reroll functions and options, set by __init__
    plan_all: Callable[[int, str], Any] | None = None
    plan_one: Callable[[int, int, str], Any] | None = None
    apply: Callable[[Any, Callable[[dict[str, Any]], None]], None] | None = None  # (plan, done)
    keys: Callable[[], tuple[str | None, str | None]] = lambda: (None, None)
    costs: tuple[int, int] = (0, 0)
    blocked: Callable[[], str | None] = lambda: None


def _changed() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.changed(KEY)


def _polling() -> bool:
    """The menu page is running our script (one of the game's menus is up)."""
    exts = _menu_module("extensions")
    polling = getattr(exts, "polling", None)
    return polling is not None and bool(polling(KEY))


# region Matching the card to an item


def _plain(text: Any) -> str:
    """A modifier's text reduced to its name: no markup, numbers or "is increased by"."""
    text = _NUMBERS.sub(" ", _TAGS.sub(" ", str(text or "")))
    text = _TRAILER.sub(" ", text.lower())
    return " ".join(re.sub(r"[^a-z' ]", " ", text).split())


def _rows_for(item: reroll.Item, rows: list[str]) -> list[int] | None:
    """Which card row shows each of the item's lines, or None if they don't all appear."""
    names = [_plain(line.name) for line in item.lines]
    taken: dict[int, int] = {}
    # Exact names first, then the longest names, so "Damage" can't take "Gun Damage"'s row
    order = sorted(range(len(names)), key=lambda i: -len(names[i]))
    for exact in (True, False):
        for i in order:
            if i in taken.values():
                continue
            for r, row in enumerate(rows):
                if r not in taken and names[i] and (row == names[i] if exact else names[i] in row):
                    taken[r] = i
                    break
    if len(taken) != len(names):
        return None
    by_line = {i: r for r, i in taken.items()}
    return [by_line[i] for i in range(len(names))]


def _level(text: Any) -> int | None:
    match = re.search(r"\d+", str(text or ""))
    return int(match.group()) if match else None


def _skills(card: dict[str, Any]) -> dict[str, int] | None:
    """The class mod skills the card lists ("<graph>,<node>" -> points), or None if it lists none."""
    out: dict[str, int] = {}
    for entry in card.get("skills") or []:
        node, value = (list(entry) + ["", ""])[:2]
        if node:
            out[str(node).lower()] = out.get(str(node).lower(), 0) + (_level(value) or 0)
    return out or None


def match(card: dict[str, Any]) -> tuple[game.Slot | None, list[int], str | None]:
    """
    The backpack item the card shows: (slot, the card row of each line, why not). From the cached
    backpack list (for showing; a reroll reads the item live again), looked at again if nothing
    matches, in case the list is behind the backpack.
    """
    if card.get("type") not in ("classmod", "enhancement"):
        return None, [], "not rerollable"
    slot, rows, why = _match_in(game.cached_items(), card)
    if why is not None:
        slot, rows, why = _match_in(game.cached_items(RECHECK_S), card)
    return slot, rows, why


def _match_in(slots: list[game.Slot], card: dict[str, Any]) -> tuple[game.Slot | None, list[int], str | None]:
    kind = card.get("type")
    rows = [_plain(text) for text in card.get("secondary") or []]
    level = _level(card.get("level"))
    rarity = str(card.get("rarity") or "").lower()
    equipped = card.get("banner") == "equipped"
    skills = _skills(card)
    found: list[tuple[game.Slot, list[int]]] = []
    for slot in slots:
        item = slot.item
        if item.kind != kind or item.level != level or slot.equipped != equipped:
            continue
        if item.rarity and rarity and item.rarity != rarity:
            continue
        if skills is not None and item.skills and item.skills != skills:
            continue
        line_rows = _rows_for(item, rows)
        if line_rows is not None:
            found.append((slot, line_rows))
    if not found:
        return None, [], "not in your backpack"
    if len({slot.item.serial for slot, _ in found}) > 1:
        # Two different items the card can't tell apart (say, different skills): don't guess
        return None, [], "ambiguous"
    return found[0][0], found[0][1], None


# endregion
# region Page link


def _pick_order(hover: dict[str, Any]) -> list[int]:
    """The lines the pick key steps through (not the legendary's own), in the card's order."""
    free = [i for i, locked in enumerate(hover["locked"]) if not locked]
    return sorted(free, key=lambda i: hover["rows"][i])


def _hover_state() -> dict[str, Any] | None:
    hover = S.hover
    if hover is None:
        return None
    out = {k: hover[k] for k in ("fp", "ok", "reason", "rows", "names", "locked", "equipped")}
    out["pick"] = S.pick
    out["order"] = _pick_order(hover) if hover["ok"] else []
    out["confirm"] = _confirming(hover)
    return out


def _confirming(hover: dict[str, Any] | None) -> bool:
    return (hover is not None and hover["ok"] and S.confirm_handle == hover["handle"]
            and time.monotonic() < S.confirm_until)  # fmt: skip


def _cancel_confirm() -> None:
    if S.confirm_handle is not None:
        S.confirm_handle = None
        _changed()


def _items_state() -> list[dict[str, Any]]:
    """
    What the page needs to recognise each rerollable item on a card by itself (the same checks
    as match()), so it can draw the prompts the moment a card shows instead of asking first.

    From game.cached_items(): no backpack walk unless the backpack's key changed or the list is
    old, and none at all while the page sleeps (no menu up: nobody would see it).
    """
    if S.items_cache is not None and not _polling():
        return S.items_cache
    try:
        slots = game.cached_items() if S.blocked() is None else []
    except game.RerollError:
        slots = []
    if slots is S.items_src and S.items_cache is not None:
        return S.items_cache
    out = []
    for slot in slots:
        item = slot.item
        out.append({
            "handle": slot.handle,
            "serial": item.serial,
            "kind": item.kind,
            "level": item.level,
            "rarity": item.rarity,
            "equipped": slot.equipped,
            "plain": [_plain(line.name) for line in item.lines],
            "names": [line.name for line in item.lines],
            "locked": [line.locked for line in item.lines],
            "skills": item.skills,
        })  # fmt: skip
    S.items_cache, S.items_src = out, slots
    return out


def state() -> dict[str, Any]:
    reroll_key, pick_key = S.keys()
    try:
        eridium = game.balance()
    except game.RerollError:
        eridium = None
    confirming = S.confirm_handle is not None and time.monotonic() < S.confirm_until
    return {
        "keys": {"reroll": reroll_key, "pick": pick_key},
        "costs": {"all": S.costs[0], "one": S.costs[1]},
        "eridium": eridium,
        "blocked": S.blocked(),
        "items": _items_state(),
        # Per item (handle): the picked line (an index into its lines), and "reroll all?"
        "pick": {"handle": S.pick_handle, "line": S.pick},
        "confirm": S.confirm_handle if confirming else None,
        "hover": _hover_state(),
        "roll": S.roll,
    }


def _set_hover(fp: str | None, card: dict[str, Any] | None) -> None:
    now = time.monotonic()
    if fp is None or card is None:
        if S.hover is not None:
            S.hover = None
            _changed()
        return
    S.hover_at = now
    if S.hover is not None and S.hover["fp"] == fp:
        return
    hover: dict[str, Any] = {"fp": fp, "ok": False, "reason": None, "rows": [], "names": [], "locked": [],
                             "equipped": False, "handle": None, "serial": None}  # fmt: skip
    if (why := S.blocked()) is not None:
        hover["reason"] = why
    else:
        try:
            slot, rows, why = match(card)
        except game.RerollError as ex:
            slot, rows, why = None, [], str(ex)
        hover["reason"] = why
        if slot is not None:
            hover.update(
                ok=True,
                rows=rows,
                names=[line.name for line in slot.item.lines],
                locked=[line.locked for line in slot.item.lines],
                equipped=slot.equipped,
                handle=slot.handle,
                serial=slot.item.serial,
            )
    if hover["ok"] and hover["handle"] != S.pick_handle:
        S.pick = None
        S.pick_handle = hover["handle"]
    S.hover = hover
    _changed()


def act(action: dict[str, Any]) -> None:
    match action.get("do"):
        case "hover":
            fp = action.get("fp")
            _set_hover(str(fp) if fp else None, action.get("card"))
        case "landed":
            if S.pending is not None and action.get("id") == S.pending["id"]:
                _apply()
        case _:
            pass


def register(game_dir: Path) -> bool:
    S.game_dir = game_dir
    exts = _menu_module("extensions")
    if exts is None:
        return False
    publish(force=True)
    exts.register(KEY, SCRIPT_URL, state, act)
    return True


def unregister() -> None:
    S.hover = None
    S.pending = None  # a spin cut short by disabling mustn't go through on the next enable
    if (exts := _menu_module("extensions")) is not None:
        exts.unregister(KEY)


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4reroll/, touching only files that changed."""
    now = time.monotonic()
    if S.game_dir is None or (not force and now - S.published_at < PUBLISH_S):
        return
    S.published_at = now
    root = resources.files(__package__)
    from_source = isinstance(root, Path)
    if not force and not from_source:
        return  # a packed .sdkmod can't change while the game runs: copied once, at register
    for name in UI_FILES:
        try:
            source = root / "ui" / name
            if from_source:
                # Dev: re-read only a file whose size or date moved
                st = source.stat()
                stamp = (st.st_size, st.st_mtime_ns)
                if not force and S.published.get(name) == stamp:
                    continue
                S.published[name] = stamp
            content = source.read_bytes()
            target = S.game_dir / UI_DIR / name
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(target)
        except OSError as ex:
            S.published.pop(name, None)  # try again next time
            logging.dev_warning(f"[Reroll] couldn't publish {name}: {ex}")


# endregion
# region Keys and rolls


def _reel(item: reroll.Item, index: int, landed: str, rng: random.Random) -> list[str]:
    """Names for one line's reel: other modifiers it could have rolled, then the one it got."""
    line = item.lines[index]
    others = [ln.part for i, ln in enumerate(item.lines) if i != index]
    stats = reroll.data()["bases"][str(item.base)]["stats"]
    names = sorted({stats[str(p)]["name"] for p in reroll.candidates(item, line, others)} - {landed})
    rng.shuffle(names)
    return names[:REEL_NAMES]


def _spin_time(spinning: int, kind: str) -> float:
    """How long the card's reels take to land (card.js), plus room for a slow page."""
    return (ONE_SPIN_S if kind == "one" else SPIN_S + STAGGER_S * max(0, spinning - 1)) + LANDED_S + SLACK_S


def _roll(kind: str, hover: dict[str, Any], line: int | None) -> None:
    """Rolls the new modifiers and sends them to the card; they're made once its reels land."""
    rng = random.Random()
    roll: dict[str, Any] = {"id": S.roll["id"] + 1, "fp": hover["fp"], "kind": kind, "line": line}
    try:
        if kind == "all":
            assert S.plan_all is not None
            plan = S.plan_all(hover["handle"], hover["serial"])
        else:
            assert S.plan_one is not None and line is not None
            plan = S.plan_one(hover["handle"], line, hover["serial"])
    except Exception as ex:  # noqa: BLE001 - Refused / RerollError: shown on the card
        roll["error"] = str(ex)
        S.roll = roll
        _changed()
        return
    lines = []
    for i, (old, new) in enumerate(zip(plan.slot.item.lines, plan.after.lines, strict=True)):
        spins = not old.locked and (kind == "all" or i == line)
        lines.append({
            "row": hover["rows"][i],
            "old": old.name,
            "new": new.name,
            "spins": spins,
            "changed": old.part != new.part,
            "reel": _reel(plan.after, i, new.name, rng) if spins else [],
        })  # fmt: skip
    roll.update(lines=lines, cost=plan.cost, equipped=plan.slot.equipped)
    S.roll = roll
    spinning = sum(1 for ln in lines if ln["spins"])
    S.pending = {"id": roll["id"], "plan": plan, "due": time.monotonic() + _spin_time(spinning, kind)}
    _changed()


def _apply() -> None:
    pending, S.pending = S.pending, None
    if pending is None or S.apply is None:
        return

    def done(result: dict[str, Any]) -> None:
        if result.get("ok"):
            logging.info(f"[Reroll] {result.get('message')}")
        else:
            logging.error(f"[Reroll] {result.get('error')}")
        if S.roll.get("id") == pending["id"]:
            S.roll = {**S.roll, "applied": result}
        # Match the card again (it shows the changed item); the pick stays, the item keeps its handle
        S.hover = None
        S.items_cache = None
        _changed()

    try:
        # At once in solo or as host; a co-op client's comes back from the host a moment later
        S.apply(pending["plan"], done)
    except Exception as ex:  # noqa: BLE001 - shown on the card
        done({"ok": False, "error": str(ex)})


def _pressed(name: str, key: str | None) -> bool:
    keys = _menu_module("keys")
    down = keys is not None and keys.key_down(key)
    was = S.keys_down.get(name, True)
    S.keys_down[name] = down
    return down and not was


def tick() -> None:
    """Every frame: publishes the page's files and handles the keys, while a menu is up."""
    if S.pending is None and not _polling():
        # No menu is up, so no card and nothing to publish for: read no keys either. A key held
        # from now on must be let go and pressed again to count once a card is up.
        if S.keys_down:
            S.keys_down.clear()
        return
    publish()
    reroll_key, pick_key = S.keys()
    reroll_pressed = _pressed("reroll", reroll_key)
    pick_pressed = _pressed("pick", pick_key)
    if S.pending is not None:
        # One roll at a time: keys wait until it's made
        if time.monotonic() > S.pending["due"]:
            _apply()
        return
    hover = S.hover
    if S.confirm_handle is not None and not _confirming(hover):
        _cancel_confirm()  # timed out, or the card shows another item
    if hover is None or time.monotonic() - S.hover_at > HOVER_FRESH_S:
        return
    if not hover["ok"]:
        if reroll_pressed and hover["reason"] not in (None, "not rerollable", "not in your backpack"):
            S.roll = {"id": S.roll["id"] + 1, "fp": hover["fp"], "error": _why(hover["reason"])}
            _changed()
        return
    if pick_pressed:
        # Top to bottom as the card shows them (the serial lists them in another order)
        order = _pick_order(hover)
        if order:
            at = order.index(S.pick) + 1 if S.pick in order else 0
            S.pick = order[at] if at < len(order) else None
            _cancel_confirm()
            _changed()
    if reroll_pressed:
        if S.pick is not None:
            _roll("one", hover, S.pick)
        elif _confirming(hover):
            _cancel_confirm()
            _roll("all", hover, None)
        else:
            # Ask first: the card shows "Reroll all modifiers?" until the second press
            S.confirm_handle = hover["handle"]
            S.confirm_until = time.monotonic() + CONFIRM_S
            _changed()


def _why(reason: str) -> str:
    if reason == "ambiguous":
        return "Another item looks just like this one; reroll it from the mod menu (F7) instead."
    return reason


# endregion
