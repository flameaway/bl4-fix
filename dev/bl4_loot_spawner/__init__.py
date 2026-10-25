"""
BL4 Loot Spawner (dev only, not shipped): drops loot next to you for testing.

Console (press ~ twice for the full console):
    spawn lucky clover            a legendary by name (128 have their own pool; see pools.json)
    spawn sm legendary            a random legendary SMG (types: ar ps sg sm sr hw shield grenade
                                  repkit enhancement classmod; rarities: common uncommon rare epic
                                  legendary pearl)
    spawn classmod                a legendary class mod for your character's class
    spawn pool itempool_ar_05_legendary
    spawn <anything> x5           five of them
    spawn find <text>             list matching pools and legendary names

    cash / eridium                show how much you have
    cash 5000000 / eridium 999    set it
    eridium +100 / eridium -50    add or take some

Or use the buttons in the mod menu (`mods` in the console, or F7). Items and money are real and
save to your character. The pool list is generated from the game's data by
tools/gen_loot_pools.py.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import ButtonOption, DropdownOption, NestedOption, build_mod, command, get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGameDataHandle, WrappedStruct

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

POOLS_FILE = Path(__file__).with_name("pools.json")
MAX_COUNT = 50  # spawns per call: they all run in one frame on the game thread
# Def handle types (from the spawn function's parameters)
ITEMPOOL_TYPE = 4144
SPAWN_PATTERN_TYPE = 49152

TYPES = {
    "ar": "ar", "assault": "ar", "rifle": "ar",
    "ps": "ps", "pistol": "ps",
    "sg": "sg", "shotgun": "sg",
    "sm": "sm", "smg": "sm",
    "sr": "sr", "sniper": "sr",
    "hw": "hw", "heavy": "hw",
    "shield": "shields", "shields": "shields",
    "grenade": "grenade_gadgets", "ordnance": "grenade_gadgets",
    "repkit": "repkit",
    "enhancement": "enhancements", "enhancements": "enhancements",
}
RARITIES = {"common": "01_common", "uncommon": "02_uncommon", "rare": "03_rare", "epic": "04_epic",
            "legendary": "05_legendary", "pearl": "06_pearl"}

_data: dict[str, Any] | None = None


def log(msg: str) -> None:
    logging.info(f"[Loot Spawner] {msg}")


def data() -> dict[str, Any]:
    global _data
    if _data is None:
        _data = json.loads(POOLS_FILE.read_text(encoding="utf-8"))
        _data["by_lower"] = {p.lower(): p for p in _data["pools"]}
    return _data


def spawn_pool(pool: str, count: int = 1) -> bool:
    pc = get_pc()
    pawn = pc.Pawn if pc else None
    if pawn is None:
        log("Load into the game first.")
        return False
    fn = unrealsdk.find_class("ItemPoolFunctionLibrary").ClassDefaultObject.SpawnItemsFromItemPoolUsingActor_Drop
    count = max(1, min(MAX_COUNT, count))
    for _ in range(count):
        args = WrappedStruct(fn.func)
        args.SourceActor = pawn
        args.itempool = FGameDataHandle(ITEMPOOL_TYPE, pool)
        args.SpawnPattern = FGameDataHandle(SPAWN_PATTERN_TYPE, "None")
        fn(args)
    log(f"Spawned {count} x {pool}")
    return True


def class_mod_pool() -> str | None:
    pc = get_pc()
    char = str(pc.PlayerState.ReplicatedCharacterDef) if pc and pc.PlayerState else ""
    # "Char_DarkSiren" -> "dark_siren"
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", char.removeprefix("Char_")).lower()
    return data()["by_lower"].get(f"itempool_class_mods_05_legendary_{name}")


def resolve(query: str) -> str | None:
    """A pool name for what the user typed, or None (after logging why)."""
    d = data()
    text = " ".join(query.lower().split())
    words = text.split()
    if not words:
        return None
    if words[0] == "pool" and len(words) == 2:
        return d["by_lower"].get(words[1]) or (log(f"No pool called {words[1]}") or None)
    if text in ("classmod", "class mod"):
        return class_mod_pool() or (log("No legendary class mod pool for this character") or None)
    if words[0] in TYPES:
        rarity = RARITIES.get(words[1] if len(words) > 1 else "legendary")
        pool = d["by_lower"].get(f"itempool_{TYPES[words[0]]}_{rarity}") if rarity else None
        if pool:
            return pool
    if text in d["legendaries"]:
        return d["legendaries"][text]
    names = [n for n in d["legendaries"] if text in n]
    pools = [p for p in d["pools"] if text.replace(" ", "_") in p.lower()]
    if len(names) == 1 and not pools:
        return d["legendaries"][names[0]]
    if len(pools) == 1 and not names:
        return pools[0]
    if names or pools:
        log(f"'{query}' matches several things; be more specific or use one of these:")
        for item in (names + pools)[:15]:
            log(f"  {item}")
    else:
        log(f"Nothing matches '{query}'. Try 'spawn find <text>'.")
    return None


@command(description="Spawn loot next to you, e.g. 'spawn lucky clover', 'spawn sm legendary x3', 'spawn classmod'.")
def spawn(args: argparse.Namespace) -> None:
    words = list(args.what)
    count = 1
    if words and re.fullmatch(r"x\d+", words[-1].lower()):
        count = int(words.pop()[1:])
    if words and words[0].lower() == "find":
        text = " ".join(words[1:]).lower()
        d = data()
        hits = [n for n in d["legendaries"] if text in n] + [p for p in d["pools"] if text.replace(" ", "_") in p.lower()]
        for hit in hits[:40]:
            log(f"  {hit}")
        log(f"{len(hits)} matches" + (" (showing 40)" if len(hits) > 40 else ""))
        return
    pool = resolve(" ".join(words))
    if pool:
        spawn_pool(pool, count)


spawn.add_argument("what", nargs="+", help="a legendary name, '<type> [rarity]', 'classmod', 'pool <name>' or 'find <text>'")


MENU_CHOICES = {
    "Legendary class mod (your class)": "classmod",
    "Legendary assault rifle": "ar legendary",
    "Legendary pistol": "ps legendary",
    "Legendary shotgun": "sg legendary",
    "Legendary SMG": "sm legendary",
    "Legendary sniper": "sr legendary",
    "Legendary heavy weapon": "hw legendary",
    "Legendary shield": "shield legendary",
    "Legendary ordnance": "grenade legendary",
    "Legendary repkit": "repkit legendary",
    "Legendary enhancement": "enhancement legendary",
}

what_option = DropdownOption("What to spawn", next(iter(MENU_CHOICES)), list(MENU_CHOICES))


def _spawn_selected(_: ButtonOption) -> None:
    pool = resolve(MENU_CHOICES[what_option.value])
    if pool:
        spawn_pool(pool)


# region Money

# Currency names in the controller's CurrencyManager
CURRENCIES = {"cash": ("Cash", "Cash"), "eridium": ("eridium", "Eridium")}
MAX_AMOUNT = 2_000_000_000


def _currency(name: str) -> Any:
    pc = get_pc()
    manager = pc.CurrencyManager if pc else None
    for entry in manager.currencies if manager else []:
        if entry.type.Name == name:
            return entry
    return None


def set_money(which: str, amount: int, *, relative: bool = False) -> None:
    """
    Sets (or with relative, changes) cash or Eridium. Writes the server's copy, which the game shows
    and saves; GiveCurrency can only add. Only works in your own game.
    """
    name, label = CURRENCIES[which]
    entry = _currency(name)
    if entry is None:
        log("Load into the game first.")
        return
    new = int(entry.Amount) + amount if relative else amount
    # The write alone doesn't refresh the UI: OnRep_Currencies(old) diffs and announces it
    manager = get_pc().CurrencyManager
    old = [copy.copy(e) for e in manager.currencies]
    entry.Amount = max(0, min(MAX_AMOUNT, new))
    try:
        manager.OnRep_Currencies(old)
    except Exception:  # noqa: BLE001 - the value changed; only the display may lag
        pass
    log(f"{label}: {int(entry.Amount):,}")


def _money_command(which: str) -> Any:
    def run(args: argparse.Namespace) -> None:
        if args.amount is None:
            entry = _currency(CURRENCIES[which][0])
            log(f"{CURRENCIES[which][1]}: {int(entry.Amount):,}" if entry is not None else "Load into the game first.")
            return
        text = args.amount.replace(",", "").replace("_", "")
        if not re.fullmatch(r"[+-]?\d+", text):
            log(f"'{args.amount}' isn't a number. Try '{which} 1000', '{which} +100' or '{which} -50'.")
            return
        set_money(which, int(text), relative=text[0] in "+-")

    cmd = command(which, description=f"Show or set your {CURRENCIES[which][1]}: '{which} 1000', '{which} +100', '{which} -50'.")(run)
    cmd.add_argument("amount", nargs="?", help="a number to set, or +N / -N to add or take")
    return cmd


cash_command = _money_command("cash")
eridium_command = _money_command("eridium")


def _money_button(label: str, which: str, amount: int, *, relative: bool = True) -> ButtonOption:
    return ButtonOption(label, on_press=lambda _: set_money(which, amount, relative=relative))


money_page = NestedOption(
    "Money",
    [
        _money_button("Add 100 Eridium", "eridium", 100),
        _money_button("Add 1,000 Eridium", "eridium", 1000),
        _money_button("Take 100 Eridium", "eridium", -100),
        _money_button("Set Eridium to 0", "eridium", 0, relative=False),
        _money_button("Add 100,000 cash", "cash", 100_000),
        _money_button("Add 10,000,000 cash", "cash", 10_000_000),
        _money_button("Set cash to 0", "cash", 0, relative=False),
    ],
    description="Set your cash and Eridium for testing. The console's 'cash' and 'eridium' commands set exact amounts.",
)

# endregion


mod = build_mod(
    options=[what_option, ButtonOption("Spawn it", on_press=_spawn_selected), money_page],
    commands=[spawn, cash_command, eridium_command],
)
