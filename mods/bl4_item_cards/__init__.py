"""
Detailed item cards for Borderlands 4.

- Class mods: each skill's name, your rank, the mod's +N, its description and its numbers as
  "now -> with this mod".
- Secondary stats get their names, and banner/flavour text renders properly.
- Firmware: every bonus tier with its numbers, which tiers you have now, and which this item would
  unlock or cost you if you equipped it.
- Legendaries: who drops them and how often at your UVH level, with keys to set a waypoint to (or
  travel to) that boss's Moxxi's Big Encore machine.
- Inventory: each legendary's own name on the equip slots and in the switch grid (option).

How it fits together:

- card_page/: our script and stylesheet (loose files) and the generated game data the cards
  read. The BL4 UI Link (uilink/, shared by our mods) runs the script in the game's pages; it
  hooks the game's own card (no replaced page, so it works next to other mods' pages).
- skill_stats.py: works out every skill's numbers for your character (a little each frame, a pass
  a second in menus) and leaves them in a loose file the card reads.
- encore_waypoint.py: the Big Encore keys, and the messages the card sends back through UI sounds.
- Card size and "fit to screen" go to the script in a loose settings file. A card taller than the
  screen scrolls with the mouse wheel (the script handles that on its own).

Turning the mod off stops the script and takes everything it drew off the cards at once.
"""

from __future__ import annotations

import sys
import threading
import types
from typing import Any

from mods_base import SETTINGS_DIR, BoolOption, ButtonOption, KeybindOption, SliderOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import card_page, encore_waypoint, frametick, health, skill_stats, uilink

__version__ = "0.2.2"
__version_info__ = (0, 2, 2)

# The page script's key in the BL4 UI Link
LINK_KEY = "itemcards"

GAME_DIR = SETTINGS_DIR.parent.parent

_status = "Detailed item cards: not checked yet."
_errors: set[str] = set()


def log(msg: str) -> None:
    logging.info(f"[Item Cards] {msg}")


def _report_error(ex: Exception) -> None:
    """The tick runs every frame; report each distinct problem only once."""
    message = repr(ex)
    if message not in _errors:
        _errors.add(message)
        logging.error(f"[Item Cards] {message}")


# Shared by every copy of this module (a hot reload imports a new one while the old copy's
# disable may still be syncing): one sync at a time, and the newest wish wins
_SYNC = sys.modules.setdefault("_bl4_item_cards_sync", types.ModuleType("_bl4_item_cards_sync"))
if not hasattr(_SYNC, "lock"):
    _SYNC.lock = threading.Lock()
    _SYNC.want = None  # (sync function, enabled) not done yet


def sync_page(enabled: bool) -> None:
    """
    card_page.sync reads the game's pak indexes (~0.7 s) and writes files: a worker does it, so
    turning the mod on mid-game (installed from the Mod Menu's Discover, which loads it on the
    game thread) doesn't freeze the game. At startup the SDK enables mods off the game thread
    anyway. The cards show once it's done.
    """
    _SYNC.want = (_sync_now, enabled)
    threading.Thread(target=_sync_latest, name="bl4_item_cards.sync", daemon=True).start()


def _sync_latest() -> None:
    with _SYNC.lock:
        want, _SYNC.want = _SYNC.want, None
        if want is not None:
            want[0](want[1])


def _sync_now(enabled: bool) -> None:
    global _status
    try:
        _status = card_page.sync(GAME_DIR, enabled)
    except Exception as ex:  # noqa: BLE001 - never let a file problem break the mod
        _status = f"Detailed item cards: couldn't update ({ex})."
    log(_status)
    # The script runs in every game page with an item card (HUD ground loot, menus, vendors...)
    # while the game's card is the one it was written for. Nothing to poll: it reads loose files
    if enabled and card_page.page_current:
        uilink.register(
            LINK_KEY, card_page.SCRIPT_URL, dict, lambda _action: None, views=(uilink.ANY,), mod=__name__
        )
    else:
        uilink.unregister(LINK_KEY)


@frametick.tick("bl4_item_cards")  # once per engine frame (shared/frametick: one hook for all our mods)
def tick() -> None:
    # Every frame: the Big Encore keys and trips in progress (cheap, see encore_waypoint.py)
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        uilink.tick()  # the BL4 UI Link's server (once a frame overall, whichever of our mods calls it)
        encore_waypoint.poll_keys(GAME_DIR, waypoint_key.value, travel_key.value)
    except Exception as ex:  # noqa: BLE001 - never let this break the game's tick
        _report_error(ex)
    # skill_stats throttles itself too (a pass is sliced across frames, under 1 ms each)
    try:
        skill_stats.tick(GAME_DIR)
    except Exception as ex:  # noqa: BLE001
        _report_error(ex)


@hook(encore_waypoint.CHANNEL_FUNCS[0], Type.PRE)
def encore_waypoint_from_inventory(_obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> Any:
    return encore_waypoint.on_channel(GAME_DIR, args)


@hook(encore_waypoint.CHANNEL_FUNCS[1], Type.PRE)
def encore_waypoint_from_hud(_obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> Any:
    return encore_waypoint.on_channel(GAME_DIR, args)


waypoint_key = KeybindOption(
    "Big Encore waypoint key",
    "J",
    description=(
        "While hovering a legendary that a boss drops, press this to set your waypoint (and a"
        " map pin) on that boss's Moxxi's Big Encore machine."
    ),
)

travel_key = KeybindOption(
    "Big Encore travel key",
    "K",
    description=(
        "While hovering a legendary that a boss drops, press this to go to that boss's Moxxi's Big"
        " Encore machine (fast travel to its map if needed). Solo or as host only."
    ),
)

def card_settings(
    scale: float | None = None,
    fit: bool | None = None,
    firmware: bool | None = None,
    slot_names: bool | None = None,
    full_names: bool | None = None,
) -> dict:
    return {
        "scale": (scale_option.value if scale is None else scale) / 100,
        "fit": fit_option.value if fit is None else fit,
        "firmware": firmware_option.value if firmware is None else firmware,
        "slot_names": slot_names_option.value if slot_names is None else slot_names,
        "full_names": full_names_option.value if full_names is None else full_names,
    }


def publish_settings(settings: dict | None) -> None:
    try:
        card_page.write_settings(GAME_DIR, settings)
    except OSError as ex:
        _report_error(ex)


# Option callbacks run before the new value is stored, so pass it along
scale_option = SliderOption(
    "Card size",
    100,
    50,
    150,
    5,
    description="Scales item cards (percent of the game's size). Applies within a couple of seconds.",
    on_change_while_enabled=lambda _, value: publish_settings(card_settings(scale=value)),
)

fit_option = BoolOption(
    "Shrink tall cards to fit",
    False,
    description=(
        "Shrinks a card that would be taller than the screen until it fits. Otherwise a tall card"
        " scrolls with the mouse wheel in menus (on the HUD it shows its top part)."
    ),
    on_change_while_enabled=lambda _, value: publish_settings(card_settings(fit=value)),
)

firmware_option = BoolOption(
    "Detailed firmware",
    True,
    description=(
        "Lists every firmware bonus tier with its numbers, marks the tiers you have now, and shows"
        " which ones equipping the item would unlock or lose."
    ),
    on_change_while_enabled=lambda _, value: publish_settings(card_settings(firmware=value)),
)

slot_names_option = BoolOption(
    "Legendary names in the inventory",
    True,
    description=(
        "Shows each legendary's own name (\"Golden God\" for a \"Churned Aching Golden God\") on the"
        " inventory's weapon and gear slots and in the list of items you can switch to."
    ),
    on_change_while_enabled=lambda _, value: publish_settings(card_settings(slot_names=value)),
)

full_names_option = BoolOption(
    "Full legendary names",
    False,
    description=(
        "Shows the whole name with its rolled prefixes (\"Churned Aching Golden God\") instead of"
        " the legendary's own name. Long names get smaller text, on two lines if needed."
    ),
    on_change_while_enabled=lambda _, value: publish_settings(card_settings(full_names=value)),
)

status_button = ButtonOption(
    "Show status",
    description="Prints to the console whether the detailed cards are installed, or why not.",
    on_press=lambda _: log(_status),
)


mod = build_mod(
    options=[scale_option, fit_option, firmware_option, slot_names_option, full_names_option, waypoint_key, travel_key, status_button],
    hooks=[tick, encore_waypoint_from_inventory, encore_waypoint_from_hud],
    on_enable=lambda: (sync_page(True), publish_settings(card_settings())),
    on_disable=lambda: (sync_page(False), publish_settings(None), encore_waypoint.cancel_trip()),
)
