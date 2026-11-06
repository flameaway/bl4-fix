"""
The Collection: every legendary and pearl the account has had, one record per copy (two copies can
have the very same serial: identical rolls are common, 39 legendaries had 2+ identical copies in one
account's saves), with who holds it, when it came, and when it went (sold, dropped) if it did.

Where copies come from:
- the live backpack (celebrate.py's check, twice a second; live_changes), by serial and count
  (copies with the same serial are alike):
  - an item leaving the backpack while the bank gains its serial in the same check is a move;
  - an item leaving any other way (sold, dropped, scrapped) marks one copy gone;
  - an item arriving brings back the copy with its serial that went most recently (dropped and
    picked up again), else it's a new copy (bought back from a vendor it has another serial: new);
- the live backpack when a world loads (live_baseline): its counts are the character's truth;
- the game's saves (savegame.py's worker; from_saves): each character's backpack and equipped gear,
  and the profile's shared bank (holder BANK), by count. A holder's save older than our last live
  change to it is skipped (the live record is newer). Fewer copies than recorded: the rest are gone;
  more: copies gone most recently come back first (a move between holders), then new copies
  ("save", timed at the save's date). The first save read after the Collection starts is the
  baseline: what was had before Kairosdex tracked anything (time 0, "before").
Lost loot isn't collected (owned.py: drops left on the ground, and test spawns on a modding setup).

Kept in settings/weapondex_collection.json:
    {"version": 2, "started": <unix>, "baselined": bool, "recount": bool, "next": <next copy id>,
     "chars": {<id>: {"name", "class", "level"}},
     "copies": {<copy id>: {"s": <serial>, "k": <dex item key>, "c": <character id or "bank">,
                            "t": <unix or 0>, "src": <source>, "l": <level>, "g": <unix gone, 0: held>}}}
(version 1 kept one record per serial: read as one copy each, and recounted by the next save read.)
The page reads a compact copy (uiresources/bl4weapondex/collection.json, without the serials),
rewritten by a worker thread when something changes.

Cards and pictures are made only for the serial the page shows (inspect; a page "serial id", shared
by identical copies): the serial is decoded into an item identity of our own with the game's own
functions, exactly as a roll is (roll.py), and its picture is the game's own inventory thumbnail of
that serial (thumbs.shoot). Nothing is written to disk for them: the game's thumbnail manager keeps
the render targets (it drops old ones past ~100), and we remember the last KEEP_PICTURES pictures
and KEEP_CARDS cards.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import shutil
import threading
import time
from collections import Counter, OrderedDict
from ctypes import wintypes
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging

from . import data, identity, itemkey, roll, stats, thumbs

FILE = Path(SETTINGS_DIR) / "weapondex_collection.json"
VERSION = 2
PAGE_FILE = "OakGame/Content/uiresources/bl4weapondex/collection.json"
BANK = "bank"
PICTURE_PREFIX = "own:"  # thumbs.shoot keys of Collection pictures (roll.tick hands them here)
KEEP_CARDS = 40
KEEP_PICTURES = 16
PICTURE_RETRY_S = 15.0  # the manager dropped our picture (or never made it): asked again after this
WRITE_DELAY_S = 0.5  # changes close together (a save read) go out in one write
WRITER_IDLE_S = 10.0  # an idle writer ends (a hot reload starts a new one)
MAX_SERIAL = 1024



class S:
    lock = threading.RLock()
    loaded = False
    started = 0.0
    baselined = False
    recount = False  # read from a version 1 file: the next save read recounts every holder
    copies: dict[str, dict[str, Any]] = {}  # noqa: RUF012 - copy id -> record
    by_serial: dict[str, list[str]] = {}  # noqa: RUF012 - serial -> its copies' ids
    next_id = 1
    chars: dict[str, dict[str, Any]] = {}  # noqa: RUF012 - character id -> {"name", "class", "level"}
    by_id: dict[str, str] = {}  # noqa: RUF012 - page serial id -> serial
    keys: dict[str, str | None] = {}  # noqa: RUF012 - serial -> dex item key (None: not a dex item)
    version = 0  # bumped on every change (the page re-reads collection.json)
    # The session's live knowledge (game thread, under the lock)
    touched: dict[str, float] = {}  # noqa: RUF012 - holder -> when we last changed it live
    # Takes under way (transfer.py): [serial, from holder, to character, until, recorded]: its
    # arrival in the backpack is that holder's copy moving, not a new one
    moves: list[list[Any]] = []  # noqa: RUF012
    game_dir: Path | None = None
    wake = threading.Event()
    writer: threading.Thread | None = None
    # The serial the page shows, and its card / picture (game thread)
    shown: str | None = None
    queue: list[str] = []  # noqa: RUF012 - page ids whose cards are to be made
    cards: OrderedDict[str, dict[str, Any]] = OrderedDict()
    status: dict[str, str] = {}  # noqa: RUF012 - page id -> "" / "reading" / an error
    pictures: OrderedDict[str, dict[str, Any]] = OrderedDict()
    asked: dict[str, float] = {}  # noqa: RUF012 - page id -> when its picture was last asked for
    on_change: Any = None


def page_id(serial: str) -> str:
    return hashlib.sha1(serial.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - an id, not security


def setup(game_dir: Path) -> None:
    S.game_dir = game_dir
    load()
    _kick()  # the page's copy, even if nothing changes this session


# region The record


def load() -> None:
    if S.loaded:
        return
    with S.lock:
        if S.loaded:
            return
        copies: dict[str, dict[str, Any]] = {}
        try:
            saved = json.loads(FILE.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("not a record")  # noqa: TRY301
            if isinstance(saved.get("copies"), dict):
                copies = {str(i): r for i, r in saved["copies"].items()
                          if isinstance(r, dict) and isinstance(r.get("s"), str) and isinstance(r.get("k"), str)}  # fmt: skip
                S.recount = bool(saved.get("recount"))
            elif isinstance(saved.get("items"), dict):
                # Version 1: a record per serial. One copy each, recounted by the next save read
                for serial, r in saved["items"].items():
                    if isinstance(serial, str) and isinstance(r, dict) and isinstance(r.get("k"), str):
                        copies[str(len(copies) + 1)] = {"s": serial, "k": r["k"], "c": r.get("c", ""), "t": r.get("t", 0),
                                                        "src": r.get("s", ""), "l": r.get("l", 1), "g": 0}  # fmt: skip
                S.recount = True
            else:
                raise ValueError("no copies")  # noqa: TRY301
            chars = saved.get("chars") if isinstance(saved.get("chars"), dict) else {}
            S.chars = {c: v for c, v in chars.items() if isinstance(c, str) and isinstance(v, dict)}
            S.started = float(saved.get("started") or time.time())
            S.baselined = bool(saved.get("baselined"))
        except FileNotFoundError:
            S.started = time.time()  # tracking starts now: the first save read is the baseline
            S.baselined = False
        except (OSError, ValueError, TypeError) as ex:
            _move_aside(ex)
            S.started, S.baselined = time.time(), False
        S.copies = copies
        S.next_id = max((int(i) for i in copies if i.isdigit()), default=0) + 1
        S.by_serial = {}
        for cid, r in copies.items():
            S.by_serial.setdefault(r["s"], []).append(cid)
            S.keys[r["s"]] = r["k"]
        S.by_id = {page_id(s): s for s in S.by_serial}
        S.loaded = True


def _move_aside(ex: Exception) -> None:
    bad = FILE.with_name(f"{FILE.stem}.bad-{int(time.time())}.json")
    try:
        FILE.replace(bad)
    except OSError:
        try:
            shutil.copyfile(FILE, bad)
        except OSError as copy_ex:
            logging.error(f"[Kairosdex] {FILE.name} was unreadable ({ex}) and couldn't be kept aside ({copy_ex})")
            return
    logging.error(f"[Kairosdex] {FILE.name} was unreadable ({ex}); moved aside to {bad.name}")


def key_of_serial(serial: str) -> str | None:
    """The dex item a serial is (None: not one of the dex's legendaries or pearls). Any thread."""
    if serial in S.keys:
        return S.keys[serial]
    key = None
    if serial and len(serial) <= MAX_SERIAL and (info := itemkey.read(serial)) is not None:
        key = data.item_by_comp().get((info["type"], info["comp"]))
    S.keys[serial] = key
    return key


def _new(serial: str, key: str, holder: str, when: float, source: str) -> str:
    """A new copy (under the lock)."""
    info = itemkey.read(serial)
    cid = str(S.next_id)
    S.next_id += 1
    S.copies[cid] = {"s": serial, "k": key, "c": holder, "t": round(when), "src": source,
                     "l": int(info["level"]) if info else 1, "g": 0}  # fmt: skip
    if serial not in S.by_serial:
        S.by_serial[serial] = []
        S.by_id[page_id(serial)] = serial
    S.by_serial[serial].append(cid)
    return cid


def _held(serial: str, holder: str) -> list[str]:
    """The copies of `serial` `holder` holds now, newest first (under the lock)."""
    ids = [c for c in S.by_serial.get(serial, ()) if S.copies[c]["g"] == 0 and S.copies[c]["c"] == holder]
    return sorted(ids, key=lambda c: -int(c))


def _changed_record(fresh: list[str], gone: int, back: int) -> None:
    S.version += 1
    _kick()
    if fresh:
        names = ", ".join(sorted({(data.dex()["items"].get(S.copies[c]["k"]) or {}).get("name", "?") for c in fresh}))
        logging.info(f"[Kairosdex] Collection: {len(fresh)} new ({names})")
    if gone or back:
        logging.info(f"[Kairosdex] Collection: {gone} no longer held, {back} back")


def _reconcile(holders: dict[str, tuple[Counter[str], float]], before: bool) -> None:
    """
    Makes each holder's held copies match what it holds now (serial -> count, as of a time), by
    count. Under the lock. Losses first everywhere, then gains take back the copies gone most
    recently (an item moved between holders), then new copies.
    """
    fresh: list[str] = []
    gone = back = 0
    gains: list[tuple[str, str, float, int]] = []
    for holder, (counts, when) in holders.items():
        serials = set(counts) | {r["s"] for r in S.copies.values() if r["c"] == holder and r["g"] == 0}
        for serial in serials:
            if key_of_serial(serial) is None:
                continue
            held = _held(serial, holder)
            have = counts.get(serial, 0)
            for cid in held[have:]:
                S.copies[cid]["g"] = round(max(when, 1))
                gone += 1
            if have > len(held):
                gains.append((holder, serial, when, have - len(held)))
    for holder, serial, when, more in gains:
        returning = sorted((c for c in S.by_serial.get(serial, ()) if S.copies[c]["g"]), key=lambda c: -S.copies[c]["g"])
        for cid in returning[:more]:
            S.copies[cid].update(c=holder, g=0)
            back += 1
        for _ in range(more - len(returning[:more])):
            fresh.append(_new(serial, S.keys[serial] or "", holder, 0 if before else when, "before" if before else "save"))
    if fresh or gone or back:
        _changed_record(fresh, gone, back)


def _who(info: dict[str, Any] | None) -> tuple[Any, ...] | None:
    return (info["name"], info["class"], info["level"]) if info else None


def _by_played(chars: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(chars, key=lambda c: -chars[c].get("played", 0))


def from_saves(chars: list[tuple[str, str, str, int, list[str], float, float]], bank: list[str], bank_at: float) -> None:
    """
    The save worker's read: every character's (id, name, class, level, held serials, save file's
    date, last played) and the bank's serials. The first read after the Collection starts records
    everything as "before".
    """
    load()
    # "played": when the game last played it (the save's own last_played_timestamp), for the page's
    # owner order and "1h ago". The character being played saves often: its date alone isn't a
    # change (the page shows it as "Now"); a new order is (a switch)
    names = {char: {"name": name, "class": cls, "level": level, "played": round(played)}
             for char, name, cls, level, _, _, played in chars if name}  # fmt: skip
    with S.lock:
        merged = {**S.chars, **names}
        if any(_who(S.chars.get(c)) != _who(v) for c, v in names.items()) or _by_played(S.chars) != _by_played(merged):
            S.chars = merged
            S.version += 1
            _kick()
        else:
            S.chars = merged  # (written with the next change)
        first = not S.baselined or S.recount
        holders = {char: (Counter(serials), saved_at) for char, _, _, _, serials, saved_at, _ in chars}
        holders[BANK] = (Counter(bank), bank_at)
        # A save older than what we saw happen live is out of date for that holder
        holders = {h: v for h, v in holders.items() if v[1] >= S.touched.get(h, 0.0)}
        # A save written before tracking began only holds what was had before it
        _reconcile({h: v for h, v in holders.items() if first or v[1] <= S.started}, before=True)
        _reconcile({h: v for h, v in holders.items() if not (first or v[1] <= S.started)}, before=False)
        if first:
            was_recount = S.recount
            S.baselined, S.recount = True, False
            S.version += 1
            _kick()
            held = sum(1 for r in S.copies.values() if r["g"] == 0)
            logging.info(f"[Kairosdex] Collection {'recounted' if was_recount else 'started'}: {held} copies held")


def live_baseline(char: str, serials: list[str]) -> None:
    """A world loaded: the character's backpack as it is. Its counts are the character's truth."""
    load()
    if not char:
        return
    mine = [s for s in serials if key_of_serial(s)]
    with S.lock:
        now = time.time()
        _reconcile({char: (Counter(mine), now)}, before=not S.baselined or S.recount)
        S.touched[char] = now


MOVE_S = 30.0  # a Take's item arrives within a second; past this its expectation is dropped


def expect_move(serial: str, source: str, char: str) -> None:
    """A Take is about to give `char` one copy of `serial` from `source` (a character or BANK)."""
    with S.lock:
        S.moves.append([serial, source, char, time.time() + MOVE_S, False])


def cancel_move(serial: str, source: str, char: str) -> None:
    """The Take failed before its item arrived."""
    with S.lock:
        S.moves = [m for m in S.moves if m[:3] != [serial, source, char] or m[4]]


def moved(serial: str, source: str, char: str) -> None:
    """
    A Take's item is in `char`'s backpack: the source's copy is now `char`'s, at once. (Left to the
    live check, the save read the Take starts next could come first: it saw the source's copy
    missing from its save, marked it gone, and the arrival then made a new copy.)
    """
    load()
    with S.lock:
        now = time.time()
        for m in S.moves:
            if m[:3] == [serial, source, char] and not m[4]:
                m[4] = True  # (kept until the live check sees the arrival: that's this copy)
                _move(serial, source, char, now)
                return


def _move(serial: str, source: str, char: str, now: float) -> None:
    """One of `source`'s copies of `serial` to `char` (under the lock)."""
    S.touched[source] = S.touched[char] = now
    cid = next(iter(_held(serial, source)), None)
    if cid is None:
        cid = _latest_gone(serial)  # (the source's copy was already marked gone)
    if cid is None:
        return
    S.copies[cid].update(c=char, g=0)
    S.version += 1
    _kick()


def _take_arrival(serial: str, char: str, now: float) -> str | None:
    """A Take's arrival seen by the live check: recorded (here, or already by moved()). Its source."""
    S.moves = [m for m in S.moves if m[3] > now]
    for i, m in enumerate(S.moves):
        if m[0] == serial and m[2] == char:
            del S.moves[i]
            if not m[4]:
                _move(serial, m[1], char, now)
            return m[1]
    return None


def _latest_gone(serial: str) -> str | None:
    """The copy with this serial that went most recently (under the lock), or None."""
    gone = [c for c in S.by_serial.get(serial, ()) if S.copies[c]["g"]]
    return max(gone, key=lambda c: S.copies[c]["g"]) if gone else None


def live_changes(char: str, left: list[str], arrived: list[str], banked: Counter[str], unbanked: int) -> list[str]:
    """
    The backpack changed: the serials that left it and came in, with the serials that came into the
    bank in the same check and how many items left the bank. Copies with the same serial are alike
    (the user's rule, 2026-11-05: "if they have it they have it"), so only counts matter: one
    leaving marks one held copy gone, one arriving brings back the one gone most recently (dropped
    and picked up again: the game gives it a new InstanceId, so that can't tell), else it's new.
    Bought back from a vendor an item has another serial: a new copy, the sold one stays gone.
    Returns the serials of new copies (for the first-time banner).
    """
    load()
    if not char:
        return []
    banked = Counter({s: n for s, n in banked.items() if key_of_serial(s)})
    new_serials: list[str] = []
    fresh: list[str] = []
    gone = back = 0
    with S.lock:
        now = time.time()
        moved = False
        for serial in left:
            cid = next(iter(_held(serial, char)), None) if key_of_serial(serial) else None
            if cid is None:
                continue
            if banked[serial] > 0:  # into the bank
                banked[serial] -= 1
                S.copies[cid]["c"] = BANK
                moved = True
            else:  # sold, dropped, scrapped
                S.copies[cid]["g"] = round(now)
                gone += 1
        for serial in arrived:
            key = key_of_serial(serial)
            if not key:
                continue
            if (source := _take_arrival(serial, char, now)) is not None:  # a Take: that copy moved
                if source == BANK and unbanked > 0:
                    unbanked -= 1  # (the bank's remove, in this same check)
                moved = True
            elif unbanked > 0 and (cid := next(iter(_held(serial, BANK)), None)) is not None:
                unbanked -= 1
                S.copies[cid]["c"] = char
                moved = True
            elif (cid := _latest_gone(serial)) is not None:  # the same item, back
                S.copies[cid].update(c=char, g=0)
                back += 1
            else:  # a new item
                fresh.append(_new(serial, key, char, now, "pickup"))
                new_serials.append(serial)
        if fresh or gone or back or moved:
            S.touched[char] = now
            if moved:
                S.touched[BANK] = now
            _changed_record(fresh, gone, back)
    return new_serials


def character_name(char: str | None) -> str | None:
    return (S.chars.get(char) or {}).get("name") if char else None


# endregion
# region Writing (a worker thread)


def _kick() -> None:
    S.wake.set()
    writer = S.writer
    if writer is None or not writer.is_alive():
        S.writer = threading.Thread(target=_write_loop, name="weapondex-collection", daemon=True)
        S.writer.start()


def _write_loop() -> None:
    written = -1
    while True:
        if not S.wake.wait(WRITER_IDLE_S):
            return
        time.sleep(WRITE_DELAY_S)  # let a burst of changes (a save read) settle
        S.wake.clear()
        with S.lock:
            version, chars = S.version, dict(S.chars)
            copies = {c: dict(r) for c, r in S.copies.items()}
            started, baselined, recount, next_id = S.started, S.baselined, S.recount, S.next_id
        if version == written and S.game_dir is not None and (S.game_dir / PAGE_FILE).is_file():
            continue
        try:
            _write(FILE, {"version": VERSION, "started": round(started, 2), "baselined": baselined, "recount": recount,
                          "next": next_id, "chars": chars, "copies": copies}, indent=1)  # fmt: skip
            if S.game_dir is not None:
                # [copy id, item key, holder, got, level, source, gone, serial id]
                rows = [[c, r["k"], r.get("c", ""), r.get("t", 0), r.get("l", 1), r.get("src", ""), r.get("g", 0),
                         page_id(r["s"])] for c, r in copies.items()]  # fmt: skip
                _write(S.game_dir / PAGE_FILE, {"v": version, "chars": chars, "items": rows})
            written = version
        except OSError as ex:
            logging.error(f"[Kairosdex] Couldn't save the Collection: {ex}")


def _write(path: Path, payload: dict[str, Any], indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, indent=indent, separators=None if indent else (",", ":")), encoding="utf-8")
    tmp.replace(path)


# endregion
# region The shown entry: its card and picture (game thread)


def show(entry: str | None) -> None:
    """The page shows this serial (a page serial id): its card is made if it isn't cached."""
    load()
    S.shown = entry if entry in S.by_id else None
    if S.shown and S.shown not in S.cards and not S.status.get(S.shown):
        S.queue = [q for q in S.queue if q != S.shown] + [S.shown]
        S.status[S.shown] = "reading"


def serial_of(entry: str | None) -> str | None:
    return S.by_id.get(entry) if entry else None


def key_of(entry: str | None) -> str | None:
    """The dex item key of a page serial id."""
    serial = S.by_id.get(entry or "")
    return key_of_serial(serial) if serial else None


def state() -> dict[str, Any]:
    """What the page needs: the list's version, the character played, the shown entry's card."""
    from . import progress  # noqa: PLC0415

    shown = S.shown
    out: dict[str, Any] = {"v": S.version, "char": progress.character(), "shown": None}
    if shown:
        picture = thumbs.follow(S.pictures.get(shown))
        if picture is None:
            S.pictures.pop(shown, None)
            if S.cards.get(shown) and time.monotonic() - S.asked.get(shown, -1e9) > PICTURE_RETRY_S:
                _shoot(shown)  # dropped by the manager (past ~100) or never made: asked again
        out["shown"] = {
            "id": shown,
            "status": S.status.get(shown, ""),
            "card": S.cards.get(shown, {}).get("card"),
            "elements": S.cards.get(shown, {}).get("elements") or [],
            "picture": picture["path"] if picture else None,
            "serial": S.by_id.get(shown),
        }
    return out


def picture_done(key: str, picture: dict[str, Any]) -> None:
    """thumbs.tick_shots finished one of ours (roll.tick hands it here)."""
    entry = key[len(PICTURE_PREFIX):]
    S.pictures[entry] = picture
    S.pictures.move_to_end(entry)
    while len(S.pictures) > KEEP_PICTURES:
        S.pictures.popitem(last=False)
    _changed()


def _shoot(entry: str) -> None:
    S.asked[entry] = time.monotonic()
    if (serial := S.by_id.get(entry)) is not None:
        thumbs.shoot(PICTURE_PREFIX + entry, serial)


def _changed() -> None:
    if S.on_change:
        S.on_change()


def busy() -> bool:
    return bool(S.queue)


def tick() -> None:
    """One card per frame at most (each is ~1-3 ms: the same work as a roll)."""
    if not S.queue:
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        return
    if not roll.warm(pc):
        return  # the stats data and attribute definitions, a piece per tick (shared with rolls)
    entry = S.queue.pop(0)
    try:
        result = _card(entry, pc)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Kairosdex] Collection card: {ex!r}")
        result = f"Couldn't read this item ({ex})."
    if isinstance(result, str):
        S.status[entry] = result
    else:
        S.status[entry] = ""
        S.cards[entry] = result
        S.cards.move_to_end(entry)
        while len(S.cards) > KEEP_CARDS:
            S.cards.popitem(last=False)
        if entry == S.shown and entry not in S.pictures:
            _shoot(entry)
    _changed()


def _card(entry: str, pc: Any) -> dict[str, Any] | str:
    serial = S.by_id.get(entry)
    key = key_of_serial(serial) if serial else None
    if serial is None or key is None:
        return "This item isn't in the Collection any more."
    item = data.dex()["items"].get(key)
    info = itemkey.read(serial)
    if item is None or info is None:
        return "This item isn't in this version of the dex."
    if not stats.resolve_defs(pc):
        return "Couldn't read item stats in this game version."
    try:
        source = identity.source(pc)
        ident = identity.Identity(source)
    except identity.Unavailable as ex:
        return f"Item stats are off for this game version ({ex})."
    try:
        if not ident.load(serial):
            return "The game didn't accept this serial."
        values = stats.read_values(ident.address)
        if values is None:
            return "This item had no stats."
        # Parts written as text (class mods' special stats) are matched up by their text, as for a roll
        parts: list[tuple[int, int | str]] = [(0, text) for text in info["texts"]]
        game = roll.Game(pc.Pawn)
        result, _ = roll._result(key, item, ident.address, values, game, int(info["level"]), parts)  # noqa: SLF001
    finally:
        ident.destroy()
    return result


def forget_cards() -> None:
    """Mod disabled: nothing in the world to clean up, only what we remember."""
    S.queue.clear()
    S.cards.clear()
    S.status.clear()
    S.pictures.clear()


# endregion
# region Copying a serial


def copy(entry: str | None) -> bool:
    """Puts an entry's serial on the Windows clipboard (a worker thread: the clipboard may be busy)."""
    serial = serial_of(entry)
    if serial is None:
        return False
    threading.Thread(target=_clipboard, args=(serial,), name="weapondex-copy", daemon=True).start()
    return True


def _clipboard(text: str) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]  # fmt: skip
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    data_ = (text + "\0").encode("utf-16-le")
    # The clipboard needs an owner window: opened with none, SetClipboardData fails after EmptyClipboard
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    if not owner:
        logging.warning("[Kairosdex] Couldn't copy the serial (no window for the clipboard).")
        return
    try:
        _fill_clipboard(user32, kernel32, owner, data_)
    finally:
        user32.DestroyWindow(owner)


def _fill_clipboard(user32: Any, kernel32: Any, owner: int, data_: bytes) -> None:
    for _ in range(10):
        if user32.OpenClipboard(owner):
            break
        time.sleep(0.05)
    else:
        logging.warning("[Kairosdex] The clipboard is busy: the serial wasn't copied.")
        return
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data_))  # GMEM_MOVEABLE
        if not handle:
            return
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return
        ctypes.memmove(pointer, data_, len(data_))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT; the clipboard owns it on success
            kernel32.GlobalFree(handle)
    finally:
        user32.CloseClipboard()


# endregion
