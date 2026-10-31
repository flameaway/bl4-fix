"""
Reroll for Borderlands 4: reroll the modifiers at the bottom of a class mod or enhancement (the
stat lines like "+12% Gun Damage"), or a whole legendary or pearl, for Eridium.

- Reroll all: 25 Eridium. Every modifier rolls again.
- Replace one: 50 Eridium. Pick a modifier; it changes to a different one, the rest stay.
- Reroll a legendary or pearl: 100 Eridium. A fresh drop of the same item at its level: every part
  the game rolls (element, barrel and body accessories, grip, scope, magazine, licensed parts,
  firmware, a class mod's skills) rolls again with the game's own weights, so rare parts stay
  rare (legendary.py). Its level and an applied cosmetic stay.

The prices are options (the host's count in co-op: its copy makes and charges every reroll).

The skills, manufacturer perks, firmware, level and rarity never change, and a legendary keeps the
stat it always comes with. New modifiers follow the game's own rules (see reroll.py), so a
rerolled item is one the game could have dropped.

The quickest way is from the item card (card.py): hover a class mod or enhancement in your
backpack and press T to reroll all its modifiers, or G to pick one (G again for the next) and then T
to replace just that one. On a legendary or pearl, T (twice) rerolls the whole item. The card plays
it as a slot machine. The card's script runs in the game's menu page through the BL4 UI Link
(uilink/, no Mod Menu needed).

Or open it from the mod menu (F7, or MODS in the pause menu): each class mod, enhancement,
legendary and pearl you carry has a page (a legendary class mod has both). Or from the console:
`reroll` lists them, `reroll 2` rerolls all of item 2's modifiers (a whole legendary), `reroll 2 1`
replaces its first one.

How the item changes is in native.py (in place, with the game's own functions, the way its
Firmware Transfer does it) and game.py (the fallback for other game builds: sell + mail); which
lines can change, and to what, is in reroll.py, from game data in parts.json
(tools/gen_reroll_parts.py); legendary rolls are in legendary.py + serialgen.py, from
legendaries.json (tools/gen_reroll_legendaries.py). If a game patch changes that data, rerolling
turns itself off until both are regenerated, rather than making items from stale tables.
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mods_base import (
    SETTINGS_DIR,
    BaseOption,
    ButtonOption,
    KeybindOption,
    NestedOption,
    SliderOption,
    build_mod,
    command,
)
from unrealsdk import logging

from . import card, frametick, game, legendary, pak, reroll

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host only
    coop = None

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

FEATURE = "reroll"
SERVICE = "reroll.apply"

GAME_DIR = SETTINGS_DIR.parent.parent
REFRESH_S = 0.5
# Whether the Mod Menu's screen shows is checked this often (it was every frame); its item pages
# are rebuilt at most this much later after it opens
MENU_CHECK_S = 0.1


def log(msg: str) -> None:
    logging.info(f"[Reroll] {msg}")


def tell(text: str, *, error: bool = False) -> None:
    """Shows a message in the mod menu if it's there, and in the console."""
    web = sys.modules.get("bl4_mod_menu.web")
    if web is not None:
        try:
            web.say(text, error=error)
            return
        except Exception:  # noqa: BLE001
            pass
    (logging.error if error else logging.info)(f"[Reroll] {text}")


# region Game data check

_data_ok: bool | None = None
_data_thread: threading.Thread | None = None


def _check_data() -> None:
    """On a worker thread (only files: reading 360 pak indexes takes most of a second)."""
    global _data_ok
    try:
        paks = sorted((GAME_DIR / "OakGame" / "Content" / "Paks").glob("*.pak"))
        # The class mod / enhancement tables and the legendary rules (the same inv files)
        prints = {**(reroll.data().get("source_data") or {}), **legendary.source_data()}
        ok = bool(prints) and all(
            (entry := pak.winning_entry(paks, path)) is not None and entry.sha1 == sha1
            for path, sha1 in prints.items()
        )
    except Exception as ex:  # noqa: BLE001 - can't tell: stay off rather than guess
        logging.error(f"[Reroll] Checking the game files failed: {ex!r}")
        ok = False
    if not ok:
        log("This game version changed the item data Reroll was made from, so it's off until it's updated.")
    _data_ok = ok


def start_data_check() -> None:
    global _data_thread
    if _data_ok is None and (_data_thread is None or not _data_thread.is_alive()):
        _data_thread = threading.Thread(target=_check_data, name="bl4_reroll.data_ok", daemon=True)
        _data_thread.start()


def data_ok() -> bool | None:
    """
    Whether the game files parts.json and legendaries.json were made from are unchanged (checked once per session, off
    the game thread), or None while that's still being checked.
    """
    if _data_ok is None:
        start_data_check()
    return _data_ok


# endregion
# region Rerolling


def _blocked() -> str | None:
    ok = data_ok()
    if ok is None:
        return "Still checking the game files; try again in a moment."
    if not ok:
        return "Reroll needs an update for this game version."
    if not game.is_host() and not _host_rerolls():
        return "In co-op the host needs Reroll to reroll your items."
    return None


def _host_rerolls() -> bool:
    """As a co-op client: the host's copy of this mod makes our rerolls."""
    return coop is not None and coop.is_client() and coop.host_has(FEATURE)


class Refused(Exception):
    """Why a reroll didn't happen (nothing was changed or paid)."""


@dataclass
class Plan:
    """A reroll that's been rolled but not made yet."""

    slot: game.Slot
    after: reroll.Item | legendary.Legend
    cost: int
    what: str  # "Rerolled: X is now Y."
    # What rolled it: the host rolls the same again for a co-op client's reroll and checks it matches
    kind: str = "all"
    line: int | None = None
    seed: int = field(default_factory=lambda: random.getrandbits(48))


@dataclass
class Outcome:
    before: reroll.Item | legendary.Legend
    after: reroll.Item | legendary.Legend
    equipped: bool  # it was equipped and now sits unequipped in the backpack (the slow swap)
    handle: int  # the new item's
    cost: int
    left: int
    message: str


def _slot(handle: int, serial: str | None) -> game.Slot:
    if (why := _blocked()) is not None:
        raise Refused(why)
    # Read live from the backpack (never the cache the menu and card show)
    slot = game.find(handle)
    if slot is None or (serial is not None and slot.item.serial != serial):
        S.signature = None
        game.invalidate(handle)
        raise Refused("That item changed; have another look.")
    return slot


def _check(cost: int) -> None:
    have = game.balance()
    if have < cost:
        raise Refused(f"Not enough Eridium: this costs {cost} and you have {have}.")
    if _host_rerolls():
        return  # the host checks how it can swap
    if (why := game.can_replace()) is not None:
        raise Refused(why)


def plan_all(handle: int, serial: str | None = None, seed: int | None = None) -> Plan:
    """Rolls every free modifier again. Raises Refused if it can't."""
    slot = _slot(handle, serial)
    if isinstance(slot.item, legendary.Legend):
        raise Refused("A legendary's parts are rerolled all together, not one by one.")
    _check(cost_all())
    seed = random.getrandbits(48) if seed is None else seed
    new_serial = reroll.reroll_all(slot.item, random.Random(seed))
    after = reroll.read(new_serial) if new_serial else None
    if after is None:
        raise Refused("This item has no modifiers that can be rerolled.")
    changes = [f"{a.name} is now {b.name}" for a, b in zip(slot.item.lines, after.lines, strict=True) if a.part != b.part]
    return Plan(slot, after, cost_all(), f"Rerolled: {', '.join(changes) if changes else 'the same modifiers came up again'}.",
                "all", None, seed)  # fmt: skip


def plan_one(handle: int, line: int, serial: str | None = None, seed: int | None = None) -> Plan:
    """Rolls one modifier into a different one. Raises Refused if it can't."""
    slot = _slot(handle, serial)
    if isinstance(slot.item, legendary.Legend):
        raise Refused("A legendary's parts are rerolled all together, not one by one.")
    if not 0 <= line < len(slot.item.lines):  # a negative line from a co-op request indexes from the end
        S.signature = None
        raise Refused("That item changed; have another look.")
    old = slot.item.lines[line]
    if old.locked:
        raise Refused(f"{old.name} comes with this legendary and can't be rerolled.")
    _check(cost_one())
    seed = random.getrandbits(48) if seed is None else seed
    new_serial = reroll.reroll_one(slot.item, line, random.Random(seed))
    after = reroll.read(new_serial) if new_serial else None
    if after is None:
        raise Refused(f"There's nothing else {old.name} could become on this item.")
    return Plan(slot, after, cost_one(), f"Replaced {old.name} with {after.lines[line].name}.", "one", line, seed)


def plan_legendary(handle: int, serial: str | None = None, seed: int | None = None) -> Plan:
    """Rolls a legendary or pearl again, as a fresh drop at its level. Raises Refused if it can't."""
    slot = _slot(handle, serial)
    item = legendary.read(slot.item.serial)
    if item is None:
        raise Refused("Only legendaries and pearls can be rerolled whole.")
    _check(cost_legendary())
    seed = random.getrandbits(48) if seed is None else seed
    try:
        new_serial = legendary.reroll(item, random.Random(seed), game.Values.resolve)
    except (ValueError, KeyError, IndexError) as ex:
        raise Refused(f"Couldn't roll this {item.name} again ({ex}); nothing was changed.") from ex
    after = game.decode(new_serial)
    if after is None:
        raise Refused(f"Couldn't roll this {item.name} again; nothing was changed.")
    kind = "pearl" if item.pearl else "legendary"
    return Plan(slot, after, cost_legendary(), f"Rerolled your {item.name}: a fresh {kind} roll at level {item.level}.",
                "legendary", None, seed)  # fmt: skip


def apply(plan: Plan) -> Outcome:
    """Pays for and makes a planned reroll. Raises Refused or game.RerollError if it can't."""
    slot = _slot(plan.slot.handle, plan.slot.item.serial)
    _check(plan.cost)
    handle, equipped = game.replace(slot, plan.after.serial)
    left = game.set_balance(game.ERIDIUM, game.balance() - plan.cost)
    note = " It's in your backpack now: equip it again." if slot.equipped and not equipped else ""
    log(f"{slot.item.label} (handle {slot.handle} -> {handle}): {slot.item.serial} -> {plan.after.serial}")
    S.signature = None
    message = f"{plan.what} {plan.cost} Eridium spent, {left} left.{note}"
    return Outcome(slot.item, plan.after, slot.equipped and not equipped, handle, plan.cost, left, message)


def _outcome_dict(outcome: Outcome) -> dict[str, Any]:
    return {"ok": True, "left": outcome.left, "equipped": outcome.equipped, "message": outcome.message}


def apply_async(plan: Plan, done: Callable[[dict[str, Any]], None]) -> None:
    """
    Makes a planned reroll. done() gets {"ok": True, "left", "equipped", "message"} or
    {"ok": False, "error"}: at once in solo or as host; later as a co-op client, whose rerolls the
    host's copy of this mod makes (the item and the Eridium belong to the host).
    """
    if not _host_rerolls():
        try:
            result = _outcome_dict(apply(plan))
        except (Refused, game.RerollError) as ex:
            result = {"ok": False, "error": str(ex)}
        done(result)
        return
    # Nothing is charged or changed here: the host rolls the same again from the seed, checks it
    # matches, then pays from our Eridium and swaps our item
    coop.request(SERVICE, {
        "kind": plan.kind,
        "line": plan.line,
        "serial": plan.slot.item.serial,
        "seed": plan.seed,
        "after": plan.after.serial,
    }, lambda reply: (game.invalidate(plan.slot.handle), done(reply)), timeout=15.0)  # fmt: skip


def _serve(req: Any) -> dict[str, Any]:
    """On the host: a co-op friend's reroll, rolled again here from their seed."""
    if not mod.is_enabled:
        return {"ok": False, "error": "Reroll is off on the host"}
    data = req.data
    with game.acting_for(req.pc):
        try:
            slot = game.find_serial(str(data["serial"]))
            if slot is None:
                return {"ok": False, "error": "That item changed; have another look."}
            seed = int(data["seed"])
            if data.get("kind") == "one":
                plan = plan_one(slot.handle, int(data["line"]), slot.item.serial, seed)
            elif data.get("kind") == "legendary":
                plan = plan_legendary(slot.handle, slot.item.serial, seed)
            else:
                plan = plan_all(slot.handle, slot.item.serial, seed)
            if plan.after.serial != data.get("after"):
                return {"ok": False, "error": "The host's Reroll is a different version; nothing was changed."}
            outcome = apply(plan)
        except (Refused, game.RerollError, KeyError, TypeError, ValueError) as ex:
            return {"ok": False, "error": str(ex) or type(ex).__name__}
    log(f"rerolled a friend's {slot.item.label}")
    return _outcome_dict(outcome)


def _tell_result(result: dict[str, Any]) -> None:
    if result.get("ok"):
        tell(result["message"])
    else:
        tell(str(result.get("error")), error=True)


def reroll_all(handle: int, serial: str | None = None) -> None:
    try:
        apply_async(plan_all(handle, serial), _tell_result)
    except (Refused, game.RerollError) as ex:
        tell(str(ex), error=True)


def reroll_one(handle: int, line: int, serial: str | None = None) -> None:
    try:
        apply_async(plan_one(handle, line, serial), _tell_result)
    except (Refused, game.RerollError) as ex:
        tell(str(ex), error=True)


def reroll_legendary(handle: int, serial: str | None = None) -> None:
    try:
        apply_async(plan_legendary(handle, serial), _tell_result)
    except (Refused, game.RerollError) as ex:
        tell(str(ex), error=True)


# endregion
# region Menu


class S:
    signature: Any = None
    refreshed_at = 0.0
    errors: set[str] = set()  # noqa: RUF012
    # Whether the mod menu's screen was showing last tick (the options are only rebuilt then)
    showing = False
    # When _menu_showing() is asked next (MENU_CHECK_S)
    menu_check_at = 0.0
    # The "You have N Eridium" row, updated in place (a pickup mustn't rebuild every page)
    balance_option: ButtonOption | None = None
    balance: int | None = None


def _title(slot: game.Slot) -> str:
    item = slot.item
    if isinstance(item, legendary.Legend):
        text = f"{item.name} ({'Pearl' if item.pearl else 'Legendary'} {item.label}), Lv {item.level}"
    else:
        text = f"{item.rarity.capitalize()} {item.label}, Lv {item.level}".strip()
    return text + (" (equipped)" if slot.equipped else "")


def _legendary_button(slot: game.Slot, item: legendary.Legend, display_name: str) -> ButtonOption:
    handle, serial = slot.handle, slot.item.serial
    return ButtonOption(
        "legendary",
        display_name=f"{display_name} ({cost_legendary()} Eridium)",
        description=(
            f"A fresh roll of this {item.name} at level {item.level}, as if it dropped again: every"
            " part the game rolls (element, accessories, licensed parts, firmware, skills and"
            " modifiers) rolls again with the game's own odds, so rare parts stay rare. Its level"
            " and any cosmetic stay."
        ),
        on_press=lambda _: reroll_legendary(handle, serial),
    )


def _legendary_page(slot: game.Slot, item: legendary.Legend) -> NestedOption:
    kind = "pearl" if item.pearl else "legendary"
    children: list[BaseOption] = [_legendary_button(slot, item, f"Reroll this {kind}")]
    return NestedOption(f"item{slot.handle}", children, display_name=_title(slot),
                        description=f"{item.maker} {item.label}. Reroll it for a new roll of its parts.".strip())  # fmt: skip


def _item_page(slot: game.Slot) -> NestedOption:
    if isinstance(slot.item, legendary.Legend):
        return _legendary_page(slot, slot.item)
    item, handle, serial = slot.item, slot.handle, slot.item.serial
    children: list[BaseOption] = [
        ButtonOption(
            "all",
            display_name=f"Reroll all modifiers ({cost_all()} Eridium)",
            description="Every modifier rolls again (it can land on what it had)."
            + (" The legendary's own stat stays." if any(line.locked for line in item.lines) else ""),
            on_press=lambda _: reroll_all(handle, serial),
        ),
    ]
    for i, line in enumerate(item.lines):
        if line.locked:
            children.append(ButtonOption(
                f"line{i}",
                display_name=f"{line.name} (stays: comes with this legendary)",
                description="This modifier is part of the legendary and can't be rerolled.",
                on_press=lambda _, i=i: reroll_one(handle, i, serial),
            ))  # fmt: skip
        else:
            children.append(ButtonOption(
                f"line{i}",
                display_name=f"Replace {line.name} ({cost_one()} Eridium)",
                description=f"Swaps {line.name} for a different random modifier. The others stay.",
                on_press=lambda _, i=i: reroll_one(handle, i, serial),
            ))  # fmt: skip
    if (whole := legendary.read(serial)) is not None:
        # A legendary class mod: its skills, modifiers and firmware can also roll again together
        children.append(_legendary_button(slot, whole, "Reroll the whole item"))
    names = ", ".join(line.name for line in item.lines) or "none"
    return NestedOption(f"item{handle}", children, display_name=_title(slot), description=f"Modifiers: {names}.")


def _info(text: str, description: str = "") -> ButtonOption:
    return ButtonOption("info", display_name=text, description=description, on_press=lambda _: _refresh(force=True))


def _balance_text(eridium: int) -> str:
    return f"You have {eridium} Eridium"


def _build() -> list[BaseOption]:
    settings: list[BaseOption] = [reroll_key, pick_key, cost_all_option, cost_one_option, cost_legendary_option]
    S.balance_option = S.balance = None
    if (why := _blocked()) is not None:
        return [_info(why), *settings]
    try:
        # Showing only: the buttons carry handle + serial, and a press reads the item live (_slot)
        slots = game.cached_items()
        eridium = game.balance()
    except game.RerollError as ex:
        return [_info(str(ex)), *settings]
    info = _info(
        _balance_text(eridium),
        f"Reroll all of an item's modifiers for {cost_all()} Eridium, or replace one for {cost_one()};"
        f" a whole legendary or pearl for {cost_legendary()}. Or hover the item in your backpack and"
        " use the keys below.",
    )
    S.balance_option, S.balance = info, eridium
    options: list[BaseOption] = [info, *settings]
    if not slots:
        options.append(_info("No class mods, enhancements, legendaries or pearls in your backpack"))
    options += [_item_page(slot) for slot in slots]
    return options


def _signature() -> Any:
    """What the pages are built from, without the Eridium balance (that row is updated in place)."""
    ok = data_ok()
    host = game.is_host() or _host_rerolls()
    if not ok or not host:
        return ("blocked", ok, host)
    try:
        return cost_all(), cost_one(), cost_legendary(), tuple((s.handle, s.item.serial, s.equipped) for s in game.cached_items())
    except game.RerollError as ex:
        return str(ex)


def _update_balance() -> None:
    option = S.balance_option
    if option is None:
        return
    try:
        eridium = game.balance()
    except game.RerollError:
        return
    if eridium != S.balance:
        S.balance = eridium
        option.display_name = _balance_text(eridium)


def _attach(options: list[BaseOption]) -> None:
    for option in options:
        option.mod = mod
        if isinstance(option, NestedOption):
            _attach(list(option.children))


def _refresh(*, force: bool = False) -> None:
    if force:
        game.invalidate()
    signature = _signature()
    if force or signature != S.signature:
        S.signature = signature
        options = _build()
        _attach(options)
        mod.options = options
    else:
        _update_balance()


def _menu_showing() -> bool:
    """The BL4 Mod Menu's screen (its page, or its UMG panel) is showing: the only reader of our pages."""
    web = sys.modules.get("bl4_mod_menu.web")
    try:
        # page_open goes stale while the page sleeps (no game menu up), so only with connected()
        if web is not None and web.Link.page_open and web.connected():
            return True
        # F7 asked the page to open its screen (it sends the mods list from then on)
        fresh = getattr(web, "_open_fresh", None)
        if fresh is not None and fresh():
            return True
    except Exception:  # noqa: BLE001 - another Mod Menu version: fall through
        pass
    menu = sys.modules.get("bl4_mod_menu.menu")
    try:
        return menu is not None and bool(menu.S.is_open)
    except Exception:  # noqa: BLE001
        return False


@frametick.tick("bl4_reroll")  # once per engine frame (shared/frametick: one hook for all our mods)
def _tick() -> None:
    try:
        card.tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        _report(ex)
    # The item pages are only rebuilt while the mod menu's screen shows (right away when it opens,
    # then every REFRESH_S); in gameplay nothing here reads the backpack
    # Asked MENU_CHECK_S apart, not every frame (it looks through the Mod Menu's modules)
    now = time.monotonic()
    if now < S.menu_check_at:
        return
    S.menu_check_at = now + MENU_CHECK_S
    showing = _menu_showing()
    opened = showing and not S.showing
    S.showing = showing
    if not showing:
        return
    if not opened and now - S.refreshed_at < REFRESH_S:
        return
    S.refreshed_at = now
    try:
        _refresh()
    except Exception as ex:  # noqa: BLE001
        _report(ex)


def _report(ex: Exception) -> None:
    """The tick runs every frame; report each distinct problem once."""
    if repr(ex) not in S.errors:
        S.errors.add(repr(ex))
        logging.error("".join(traceback.format_exception(ex)))


# endregion
# region Console


@command("reroll", description="List your class mods, enhancements, legendaries and pearls, or reroll them.")
def reroll_command(args: argparse.Namespace) -> None:
    try:
        slots = game.items()
    except game.RerollError as ex:
        log(str(ex))
        return
    if args.item is None:
        log(f"You have {game.balance()} Eridium. 'reroll <item>' rerolls all ({cost_all()}; a whole legendary or pearl"
            f" {cost_legendary()}), 'reroll <item> <modifier>' replaces one ({cost_one()}).")
        for n, slot in enumerate(slots, 1):
            log(f"{n}. {_title(slot)}")
            for m, line in enumerate(slot.item.lines, 1):
                log(f"     {m}. {line.name}{' (comes with the legendary)' if line.locked else ''}")
        return
    if not 1 <= args.item <= len(slots):
        log(f"No item {args.item}; 'reroll' lists them.")
        return
    slot = slots[args.item - 1]
    if isinstance(slot.item, legendary.Legend):
        reroll_legendary(slot.handle)
    elif args.modifier is None:
        reroll_all(slot.handle)
    elif 1 <= args.modifier <= len(slot.item.lines):
        reroll_one(slot.handle, args.modifier - 1)
    else:
        log(f"That item has no modifier {args.modifier}.")


reroll_command.add_argument("item", type=int, nargs="?", help="item number from 'reroll'")
reroll_command.add_argument("modifier", type=int, nargs="?", help="replace just this modifier")

# endregion


# region Card keys

reroll_key = KeybindOption(
    "Reroll key",
    "T",
    description=(
        "While hovering a class mod or enhancement in your backpack: rerolls all its modifiers, or"
        " the one you picked with the pick key. On a legendary or pearl: rerolls the whole item."
        " Rerolling all asks first: press it again to confirm."
    ),
)
pick_key = KeybindOption(
    "Pick modifier key",
    "G",
    description=(
        "While hovering a class mod or enhancement: picks one of its modifiers for the reroll key to"
        " replace. Press again for the next one; past the last it goes back to rerolling all."
    ),
)


# The item pages show the prices: they're in _signature, so changing one rebuilds the pages

cost_all_option = SliderOption(
    "Reroll all cost",
    25,
    0,
    1000,
    5,
    description=(
        "Eridium to reroll every modifier on an item. In co-op the host's price counts: its copy"
        " makes and charges every reroll."
    ),
)
cost_one_option = SliderOption(
    "Replace one cost",
    50,
    0,
    1000,
    5,
    description=(
        "Eridium to replace one modifier you picked. In co-op the host's price counts: its copy"
        " makes and charges every reroll."
    ),
)


cost_legendary_option = SliderOption(
    "Legendary / pearl reroll cost",
    100,
    0,
    1000,
    5,
    description=(
        "Eridium to reroll a whole legendary or pearl: a fresh roll of its parts at its level, with"
        " the game's own odds. In co-op the host's price counts: its copy makes and charges every"
        " reroll."
    ),
)


def cost_all() -> int:
    return int(cost_all_option.value)


def cost_one() -> int:
    return int(cost_one_option.value)


def cost_legendary() -> int:
    return int(cost_legendary_option.value)


card.S.plan_all = plan_all
card.S.plan_one = plan_one
card.S.plan_legendary = plan_legendary
card.S.apply = apply_async
card.S.keys = lambda: (reroll_key.value, pick_key.value)
card.S.costs = lambda: (cost_all(), cost_one(), cost_legendary())
card.S.blocked = _blocked


def _enable() -> None:
    start_data_check()
    card.register(GAME_DIR)


def _disable() -> None:
    card.unregister()


# endregion

mod = build_mod(
    # The rest of the options are built from the backpack (_refresh); these are listed here so
    # their saved values load
    options=[reroll_key, pick_key, cost_all_option, cost_one_option, cost_legendary_option],
    hooks=[_tick],
    commands=[reroll_command],
    on_enable=_enable,
    on_disable=_disable,
)


if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
    coop.handle(SERVICE, _serve)
