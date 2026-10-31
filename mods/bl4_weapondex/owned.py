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

"Per-character Kairosdex" (progress.Mode): each character also gets its own record, kept in
settings/weapondex_owned_chars.json ({"version": 1, "chars": {<character id>: {"items": [...],
"since": <unix time>}}}). It starts (and "Reset this character's legendaries" restarts it) as what
the character carries: its live backpack, equipped gear included, never the shared bank. Then it
grows with pickups (celebrate.py), the backpack walk when the dex opens, and the character's own
save, only from saves written after the record started (an older save could hold items it no
longer carries). Records are only written while the option is on, and kept while it's off. The
account record above is kept up to date either way.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging

from . import data, itemkey, progress

FILE = Path(SETTINGS_DIR) / "weapondex_owned.json"
VERSION = 3
CHARS_FILE = Path(SETTINGS_DIR) / "weapondex_owned_chars.json"
CHARS_VERSION = 1
START_CHECK_S = 1.0  # how often to look for a character that has no record yet (option on)
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
    # Per character (replaced whole on change, so other threads always read a complete one)
    chars: dict[str, frozenset[str]] = {}  # noqa: RUF012
    since: dict[str, float] = {}  # noqa: RUF012 - when each character's record (re)started
    next_start_check = 0.0


def _key_map() -> dict[tuple[int, int], str]:
    return {(item["ti"], item["si"]): key for key, item in data.dex()["items"].items() if "ti" in item and "si" in item}


def load() -> None:
    if S.loaded:
        return
    with S.lock:  # the save worker and the game thread may both get here first
        if not S.loaded:
            _load()
            _load_chars()
            S.loaded = True


def _move_aside(path: Path, ex: Exception) -> None:
    bad = path.with_name(f"{path.stem}.bad-{int(time.time())}.json")
    try:
        path.replace(bad)
    except OSError:
        # Can't move it (locked?): keep a copy instead, the next save writes over the file
        try:
            shutil.copyfile(path, bad)
        except OSError as copy_ex:
            logging.error(f"[Kairosdex] {path.name} was unreadable ({ex}) and couldn't be kept aside ({copy_ex})")
            return
    logging.error(f"[Kairosdex] {path.name} was unreadable ({ex}); moved aside to {bad.name}")


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
        _move_aside(FILE, ex)


def _load_chars() -> None:
    try:
        saved = json.loads(CHARS_FILE.read_text(encoding="utf-8"))
        records = saved["chars"]
        if not isinstance(records, dict):
            raise ValueError("no character list")  # noqa: TRY301
        chars, since = {}, {}
        for char, record in records.items():
            if isinstance(char, str) and isinstance(record, dict) and isinstance(record.get("items"), list):
                chars[char] = frozenset(k for k in record["items"] if isinstance(k, str))
                since[char] = float(record.get("since") or 0)
        S.chars, S.since = chars, since
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as ex:
        _move_aside(CHARS_FILE, ex)


def items(char: str | None = None) -> frozenset[str]:
    """The account's legendaries, or one character's own record (empty until it has started)."""
    load()
    return S.items if char is None else S.chars.get(char, frozenset())


def _keys(serials: list[str]) -> set[str]:
    keys = _key_map()
    found = set()
    for serial in serials:
        if (ids := itemkey.type_and_comp(serial)) is not None and (key := keys.get(ids)):
            found.add(key)
    return found


def add_serials(serials: list[str], char: str | None = None) -> set[str]:
    """
    Records the legendaries among these serials for the account, and with `char` for that
    character too (if its record has started). Any thread. Returns the keys that were new: to the
    account, or with `char`, to that character's record.
    """
    load()
    found = _keys(serials)
    mine: set[str] = set()
    with S.lock:
        new = found - S.items
        if new:
            S.items = frozenset(S.items | new)
            S.version += 1
            # Saved under the lock: two threads saving at once could land the older list last
            _save(sorted(S.items))
        total = len(S.items)
        if char is not None and char in S.chars:
            mine = found - S.chars[char]
            if mine:
                S.chars = {**S.chars, char: S.chars[char] | mine}
                S.version += 1
                _save_chars()
    if new:
        logging.info(f"[Kairosdex] Obtained: {len(new)} more legendaries recorded ({total} in all)")
    if mine:
        logging.info(f"[Kairosdex] Obtained on this character: {len(mine)} more ({len(S.chars[char])} in all)")
    return new if char is None else mine


def start_char(char: str, serials: list[str]) -> int:
    """(Re)starts a character's own record as the legendaries among `serials` (what it carries)."""
    add_serials(serials)  # the account had them too
    keys = _keys(serials)
    with S.lock:
        S.chars = {**S.chars, char: frozenset(keys)}
        S.since = {**S.since, char: time.time()}
        S.version += 1
        _save_chars()
    logging.info(f"[Kairosdex] This character's legendaries start with the {len(keys)} it carries")
    return len(keys)


def reset_char(char: str) -> None:
    """Drops a character's record: tick starts it again from what it carries (within a second)."""
    load()
    with S.lock:
        S.chars = {c: v for c, v in S.chars.items() if c != char}
        S.since = {c: v for c, v in S.since.items() if c != char}
        S.version += 1
        _save_chars()
    S.next_start_check = 0.0


def add_from_save(char: str, serials: list[str], saved_at: float) -> bool:
    """
    A character's save (backpack and equipped; the save worker): added to its own record, if the
    option is on, the record has started and the save was written after that. True if it grew.
    """
    if not progress.Mode.per_char():
        return False
    load()
    since = S.since.get(char) if char in S.chars else None
    if since is None or saved_at <= since:
        return False
    return bool(add_serials(serials, char))


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
        logging.info(f"[Kairosdex] Obtained: rebuilt from the saves, {len(gone)} not held taken out ({len(S.items)} left)")
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


def _write(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as ex:
        logging.error(f"[Kairosdex] Couldn't save {path.name}: {ex}")


def _save(keys: list[str]) -> None:
    _write(FILE, {"version": VERSION, "items": keys})


def _save_chars() -> None:
    """Under S.lock."""
    records = {c: {"items": sorted(keys), "since": round(S.since.get(c, 0.0), 2)} for c, keys in S.chars.items()}
    _write(CHARS_FILE, {"version": CHARS_VERSION, "chars": records})


# region The live backpack


def scan_backpack(pc: Any, start: str | None = None) -> None:
    """
    Starts reading the backpack's serials (spread over frames by tick). With `start` (a character
    id), they (re)start that character's own record instead of being added.
    """
    state = getattr(pc, "PlayerState", None) if pc is not None else None
    if S.walk is None and state is not None:
        S.walk = _walk(state._get_address(), start)  # noqa: SLF001


def _start_check() -> None:
    """Option on and a character with no record yet: start one from what it carries."""
    now = time.monotonic()
    if now < S.next_start_check:
        return
    S.next_start_check = now + START_CHECK_S
    if not progress.Mode.per_char():
        return
    load()
    char = progress.character()
    if char is not None and char not in S.chars:
        scan_backpack(get_pc(possibly_loading=True), start=char)


def tick() -> None:
    if S.walk is None:
        _start_check()
        if S.walk is None:
            return
    try:
        next(S.walk)
    except StopIteration:
        S.walk = None
    except Exception as ex:  # noqa: BLE001 - the dex works without it
        logging.dev_warning(f"[Kairosdex] backpack read failed: {ex!r}")
        S.walk = None


def serial_of(item: Any) -> str | None:
    """The serial text of a backpack slot's InventoryItem, or None (an empty slot, unreadable)."""
    from . import stats  # noqa: PLC0415

    if item.Handle.Handle == -1:
        return None
    identity = item.Item.data.Identity._get_address()  # noqa: SLF001
    head = stats._read(identity + SERIAL_PTR, SERIAL_LEN - SERIAL_PTR + 8)  # noqa: SLF001
    if head is None:
        return None
    pointer = int.from_bytes(head[:8], "little")
    length = int.from_bytes(head[SERIAL_LEN - SERIAL_PTR :], "little")
    if 2 < length <= 4096 and (text := stats._read(pointer, length)):  # noqa: SLF001
        return text.split(b"\0", 1)[0].decode("ascii", "replace")
    return None


def _slots(state_address: int) -> Any:
    """The backpack's slots, if the player is still the one the walk started on (else None)."""
    pc = get_pc(possibly_loading=True)
    state = getattr(pc, "PlayerState", None) if pc is not None else None
    if state is None or state._get_address() != state_address:  # noqa: SLF001
        return None
    return state.BackpackItems.items


def _walk(state_address: int, start: str | None) -> Any:
    serials = []
    first = 0
    while True:
        # Looked up again every frame: never hold the array across frames (a world change frees it)
        slots = _slots(state_address)
        if slots is None:
            return  # a new world or character: dropped
        count = len(slots)
        if first >= count:
            break
        for i in range(first, min(count, first + SLOTS_PER_TICK)):
            if serial := serial_of(slots[i].InventoryItem):
                serials.append(serial)
        first += SLOTS_PER_TICK
        yield
    if start is None:
        add_serials(serials, progress.scope())
    elif serials:  # an empty backpack is more likely still loading: tried again in a second
        start_char(start, serials)


# endregion
