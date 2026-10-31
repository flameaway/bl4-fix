"""
What to call the things that dealt damage.

Gear: a hit's DamageCauser is the live weapon / shield / grenade / repair kit actor (grenade hits
come from the thrown projectile, which we tie to the equipped grenade). Each player's equipped
actors are listed in `pawn.EquippedInventorySlots.items` (`InstancedInventory` + the backpack
handle `SourceItemHandle.Handle`); the backpack entry with that handle holds the item's serial
(text pointer at identity +0xA0, length +0xB0, as Reroll reads it). The serial's first two
numbers (item type, composition) look up items.json (tools/gen_damage_meter.py): legendary and
pearl names, otherwise "<Manufacturer> <Kind>", with rarity and the game's icon.

`Equipped` keeps that map per player and rebuilds it only when the slots' ArrayReplicationKey
or the actors in them change, so the per-hit lookup is one dict get.

items.json is pinned to the game files it came from ("source_data" SHA1s); `data_current()`
checks them on a worker thread (the pak lookup takes about a second). After a game patch the
legendary names are dropped (indices may have moved), leaving manufacturer and kind.
"""

from __future__ import annotations

import json
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import itemkey, native, pak

SERIAL_PTR = 0xA0
SERIAL_LEN = 0xB0
MAX_SERIAL = 4096
RARITY_ORDER = ("common", "uncommon", "rare", "epic", "legendary", "pearl")

_items: dict[str, Any] | None = None
_current: bool | None = None
_check_thread: threading.Thread | None = None


def items() -> dict[str, Any]:
    global _items
    if _items is None:
        try:
            _items = json.loads((resources.files(__package__) / "items.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            logging.error(f"[Carnage Meter] items.json: {ex!r}")
            _items = {"types": {}, "comps": {}, "classes": {}}
    return _items


def _check(game_dir: Path) -> None:
    global _current
    paks = sorted((game_dir / "OakGame" / "Content" / "Paks").glob("*.pak"))
    prints = items().get("source_data") or {}
    try:
        _current = bool(prints) and all(
            (entry := pak.winning_entry(paks, path)) is not None and entry.sha1 == sha1 for path, sha1 in prints.items()
        )
    except Exception:  # noqa: BLE001 - can't tell: keep the names
        _current = True
    if not _current:
        logging.warning("[Carnage Meter] the game's item data changed since items.json was made: legendary names are off")


def start_check(game_dir: Path) -> None:
    global _check_thread
    if _current is None and (_check_thread is None or not _check_thread.is_alive()):
        _check_thread = threading.Thread(target=_check, args=(game_dir,), name="bl4_damage_meter.data", daemon=True)
        _check_thread.start()


def data_current() -> bool:
    return _current is not False


def describe(type_index: int, comp_index: int) -> dict[str, Any]:
    """{"name", "rarity", "icon", "kind"} for an item (type, composition)."""
    data = items()
    kind = data["types"].get(str(type_index)) or {}
    comp = (data["comps"].get(str(type_index)) or {}).get(str(comp_index))
    rarity, own = (comp[0], comp[1]) if isinstance(comp, list) else (comp, None)
    if not data_current():
        own = None
    if own:
        name = own
    elif kind.get("class"):
        name = f"{kind['class']} Class Mod"
    elif kind:
        name = f"{kind.get('maker', '')} {kind.get('kind', '')}".strip()
    else:
        name = "Gear"
    return {"name": name, "rarity": rarity or "common", "icon": kind.get("icon", ""), "kind": kind.get("kind", "")}


def character(def_name: str) -> dict[str, Any]:
    """Class info for a character def name ("Char_DarkSiren")."""
    key = def_name.lower().removeprefix("char_").replace("_", "")
    found = items().get("classes", {}).get(key)
    return dict(found) if found else {"class": "", "hero": "", "portrait": "", "trait": ""}


def read_serial(identity: Any) -> str | None:
    head = native.safe_read(identity._get_address(), SERIAL_LEN + 8)
    if head is None:
        return None
    pointer = int.from_bytes(head[SERIAL_PTR : SERIAL_PTR + 8], "little")
    length = int.from_bytes(head[SERIAL_LEN : SERIAL_LEN + 8], "little")
    if not 2 < length <= MAX_SERIAL:
        return None
    text = native.safe_read(pointer, length)
    if text is None:
        return None
    serial = text.split(b"\0", 1)[0].decode("ascii", "replace")
    return serial if serial.startswith("@U") else None


class Equipped:
    """One player's equipped actors -> (item key, slot kind), rebuilt only when the slots change."""

    def __init__(self) -> None:
        self.signature: tuple[Any, ...] = ()
        # actor address -> item key ("item:<type>:<comp>"); also "grenade" -> the grenade's key
        self.by_actor: dict[int, str] = {}
        self.grenade: str | None = None
        self.info: dict[str, dict[str, Any]] = {}  # item key -> describe()

    def refresh(self, pawn: Any, player_state: Any) -> bool:
        """Re-reads the slots if they changed. True if anything did."""
        slots = pawn.EquippedInventorySlots
        items = slots.items
        entries = [items[i] for i in range(len(items))]  # by index: the array's iterator is slow (tracker.elements)
        actors = []
        for entry in entries:
            actor = entry.InstancedInventory
            actors.append((actor._get_address() if actor is not None else 0, entry.SourceItemHandle.Handle))
        signature = (pawn._get_address(), slots.ArrayReplicationKey, tuple(actors))
        if signature == self.signature:
            return False
        self.signature = signature
        wanted = {handle for address, handle in actors if address and handle != -1}
        serials: dict[int, str] = {}
        for entry in player_state.BackpackItems.items:
            inv = entry.InventoryItem
            handle = inv.Handle.Handle
            if handle in wanted and handle not in serials:
                serial = read_serial(inv.Item.data.Identity)
                if serial:
                    serials[handle] = serial
                if len(serials) == len(wanted):
                    break
        self.by_actor, self.grenade = {}, None
        for entry, (address, handle) in zip(entries, actors, strict=True):
            serial = serials.get(handle)
            found = itemkey.type_and_comp(serial) if serial else None
            if not address or found is None:
                continue
            key = f"item:{found[0]}:{found[1]}"
            self.by_actor[address] = key
            if key not in self.info:
                self.info[key] = describe(*found)
            if entry.InstancedInventory.Class.Name == "GrenadeGadget":
                self.grenade = key
        return True
