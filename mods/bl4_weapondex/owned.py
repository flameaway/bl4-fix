"""
The legendaries the player has had, account-wide: the dex's "Obtained only" mode shows only those
(the rest read "???"). The game keeps no history of items found (the saves only count them,
`loot_legendaries`), so this collects what it can see and remembers it:

- every save (savegame.py, on its worker thread, whenever the saves change): each character's
  backpack and equipped gear, and the profile's shared bank;
- the live backpack when the dex opens (a few slots per frame), for what the saves don't have yet.

Lost loot doesn't count: it's drops the player left on the ground (and, on a modding setup,
everything spawned and never picked up), so a pearl showed as obtained that was never held.

Once seen an item stays obtained (selling it doesn't undo that). Kept in
settings/weapondex_owned.json ({"version": 3, "items": [item key, ...]}). An older file (it counted
lost loot, and on the dev setup test items other sessions spawned) is rebuilt from what the saves
hold now on the first save read (`rebuild`).
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR
from unrealsdk import logging

from . import data, itemkey

FILE = Path(SETTINGS_DIR) / "weapondex_owned.json"
VERSION = 3
SERIAL = re.compile(r"serial:\s*'(@U[^']+)'")
SAVE_KEY = re.compile(r"^( *)(?:- )?(\w+):")
HELD = "inventory"  # characters' state.inventory (backpack, equipped), the profile's shared inventory (bank)
SLOTS_PER_TICK = 25  # live backpack walk: slots read per frame
SERIAL_PTR, SERIAL_LEN = 0xA0, 0xB0  # FInventoryIdentity: the serial's text pointer and length


class S:
    items: frozenset[str] = frozenset()
    loaded = False
    version = 0  # bumped on every change, for the page
    lock = threading.Lock()
    walk: Any = None  # the live backpack walk in progress (a generator)
    stale = False  # the file is an older version's: rebuilt from the saves (rebuild)


def _key_map() -> dict[tuple[int, int], str]:
    return {(item["ti"], item["si"]): key for key, item in data.dex()["items"].items() if "ti" in item and "si" in item}


def load() -> None:
    if S.loaded:
        return
    with S.lock:  # the save worker and the game thread may both get here first
        if not S.loaded:
            _load()
            S.loaded = True


def _load() -> None:
    try:
        saved = json.loads(FILE.read_text(encoding="utf-8"))
        found = saved["items"]
        if not isinstance(found, list):
            raise ValueError("no item list")  # noqa: TRY301
        S.items = frozenset(k for k in found if isinstance(k, str))
        S.stale = saved.get("version") != VERSION
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError) as ex:
        bad = FILE.with_name(f"weapondex_owned.bad-{int(time.time())}.json")
        try:
            FILE.replace(bad)
        except OSError:
            pass
        logging.error(f"[Weapondex] {FILE.name} was unreadable ({ex}); moved aside to {bad.name}")


def items() -> frozenset[str]:
    load()
    return S.items


def _keys(serials: list[str]) -> set[str]:
    keys = _key_map()
    found = set()
    for serial in serials:
        if (ids := itemkey.type_and_comp(serial)) is not None and (key := keys.get(ids)):
            found.add(key)
    return found


def add_serials(serials: list[str]) -> int:
    """Records the legendaries among these serials. Any thread. Returns how many were new."""
    load()
    found = _keys(serials)
    with S.lock:
        new = found - S.items
        if not new:
            return 0
        S.items = frozenset(S.items | new)
        S.version += 1
        # Saved under the lock: two threads saving at once could land the older list last
        _save(sorted(S.items))
        total = len(S.items)
    logging.info(f"[Weapondex] Obtained: {len(new)} more legendaries recorded ({total} in all)")
    return len(new)


def rebuild(held: list[str]) -> bool:
    """
    Once, for an older version's file: the record becomes what the saves hold now (`held`: every
    save's serials, from_save_text). True if anything changed.
    """
    load()
    if not S.stale:
        return False
    keep = _keys(held)
    with S.lock:
        S.stale = False
        gone = S.items - keep
        S.items = frozenset(S.items & keep)
        S.version += 1
        _save(sorted(S.items))
    if gone:
        logging.info(f"[Weapondex] Obtained: rebuilt from the saves, {len(gone)} not held taken out ({len(S.items)} left)")
    return bool(gone)


def from_save_text(text: str) -> list[str]:
    """Serials of the items held (backpack, equipped, bank) in a save's YAML; lost loot and the rest are skipped."""
    held: list[str] = []
    inside = -1  # the inventory key's indent while inside it
    for line in text.splitlines():
        if (found := SAVE_KEY.match(line)) is None:
            continue
        indent, key = len(found.group(1)), found.group(2)
        if inside >= 0 and indent <= inside:
            inside = -1
        if inside < 0:
            if key == HELD:
                inside = indent
        elif key == "serial" and (serial := SERIAL.search(line)):
            held.append(serial.group(1))
    return held


def _save(keys: list[str]) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(f"{FILE.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"version": VERSION, "items": keys}, indent=1), encoding="utf-8")
        tmp.replace(FILE)
    except OSError as ex:
        logging.error(f"[Weapondex] Couldn't save {FILE.name}: {ex}")


# region The live backpack


def scan_backpack(pc: Any) -> None:
    """Starts reading the backpack's serials (spread over frames by tick)."""
    if S.walk is None and pc is not None and pc.PlayerState is not None:
        S.walk = _walk(pc)


def tick() -> None:
    if S.walk is None:
        return
    try:
        next(S.walk)
    except StopIteration:
        S.walk = None
    except Exception as ex:  # noqa: BLE001 - the dex works without it
        logging.dev_warning(f"[Weapondex] backpack read failed: {ex!r}")
        S.walk = None


def _walk(pc: Any) -> Any:
    from . import stats  # noqa: PLC0415

    slots = pc.PlayerState.BackpackItems.items
    count = len(slots)
    serials = []
    for start in range(0, count, SLOTS_PER_TICK):
        for i in range(start, min(count, start + SLOTS_PER_TICK)):
            item = slots[i].InventoryItem
            if item.Handle.Handle == -1:
                continue
            identity = item.Item.data.Identity._get_address()  # noqa: SLF001
            head = stats._read(identity + SERIAL_PTR, SERIAL_LEN - SERIAL_PTR + 8)  # noqa: SLF001
            if head is None:
                continue
            pointer = int.from_bytes(head[:8], "little")
            length = int.from_bytes(head[SERIAL_LEN - SERIAL_PTR :], "little")
            if 2 < length <= 4096 and (text := stats._read(pointer, length)):  # noqa: SLF001
                serials.append(text.split(b"\0", 1)[0].decode("ascii", "replace"))
        yield
    add_serials(serials)


# endregion
