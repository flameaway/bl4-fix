"""
The Collection: every legendary and pearl the account has had, by its exact serial (its roll), with
the character that got it and when.

Where entries come from (each serial is recorded once; the first sighting decides its character):
- a pickup (celebrate.py's backpack check, twice a second): the exact time, "pickup";
- the live backpack when the dex opens (owned.py's walk): what the check missed, "backpack";
- the game's saves (savegame.py's worker): each character's backpack and equipped gear, and the
  profile's shared bank (character BANK), "save", timed at the save's date (it was had by then).
  The first save read after the Collection starts is the baseline: what was had before Kairosdex
  tracked anything (time 0, "before").
Lost loot isn't collected (owned.py: drops left on the ground, and test spawns on a modding setup).
Once recorded, an entry stays, whatever happens to the item after (sold, scrapped, moved).

Kept in settings/weapondex_collection.json:
    {"version": 1, "started": <unix>, "baselined": bool, "chars": {<id>: {"name", "class"}},
     "items": {<serial>: {"k": <dex item key>, "c": <character id or "bank">, "t": <unix or 0>,
                          "s": <source>, "l": <level>}}}
The page reads a compact copy (uiresources/bl4weapondex/collection.json, without the serials),
rewritten by a worker thread when something changes.

Cards and pictures are made only for the entry the page shows (inspect): the serial is decoded into
an item identity of our own with the game's own functions, exactly as a roll is (roll.py), and its
picture is the game's own inventory thumbnail of that serial (thumbs.shoot). Nothing is written to
disk for them: the game's thumbnail manager keeps the render targets (it drops old ones past ~100),
and we remember the last KEEP_PICTURES pictures and KEEP_CARDS cards.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import shutil
import threading
import time
from collections import OrderedDict
from ctypes import wintypes
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging

from . import data, identity, itemkey, roll, stats, thumbs

FILE = Path(SETTINGS_DIR) / "weapondex_collection.json"
VERSION = 1
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
    items: dict[str, dict[str, Any]] = {}  # noqa: RUF012 - serial -> record; replaced whole on change
    chars: dict[str, dict[str, str]] = {}  # noqa: RUF012 - character id -> {"name", "class"}
    by_id: dict[str, str] = {}  # noqa: RUF012 - page id -> serial
    version = 0  # bumped on every change (the page re-reads collection.json)
    game_dir: Path | None = None
    wake = threading.Event()
    writer: threading.Thread | None = None
    # The entry the page shows, and its card / picture (game thread)
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
        try:
            saved = json.loads(FILE.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("items"), dict):
                raise ValueError("no item list")  # noqa: TRY301
            items = {s: r for s, r in saved["items"].items()
                     if isinstance(s, str) and isinstance(r, dict) and isinstance(r.get("k"), str)}  # fmt: skip
            chars = saved.get("chars") if isinstance(saved.get("chars"), dict) else {}
            S.items = items
            S.chars = {c: v for c, v in chars.items() if isinstance(c, str) and isinstance(v, dict)}
            S.started = float(saved.get("started") or time.time())
            S.baselined = bool(saved.get("baselined"))
        except FileNotFoundError:
            S.started = time.time()  # tracking starts now: the first save read is the baseline
            S.baselined = False
        except (OSError, ValueError, TypeError) as ex:
            _move_aside(ex)
            S.started, S.baselined = time.time(), False
        S.by_id = {page_id(s): s for s in S.items}
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


def _keys() -> dict[tuple[int, int], str]:
    return {(item["ti"], item["si"]): key for key, item in data.dex()["items"].items() if "ti" in item and "si" in item}


def add(serials: list[str], char: str | None, source: str, when: float | None = None) -> int:
    """
    Records the dex items among `serials` that aren't recorded yet, as had by `char` (a character id,
    BANK, or None: unknown) at `when` (None: now). Any thread. Returns how many were new.
    """
    load()
    keys = _keys()
    fresh: dict[str, dict[str, Any]] = {}
    for serial in serials:
        if not serial or len(serial) > MAX_SERIAL or serial in S.items or serial in fresh:
            continue
        info = itemkey.read(serial)
        if info is None or (key := keys.get((info["type"], info["comp"]))) is None:
            continue
        fresh[serial] = {"k": key, "c": char or "", "t": round(time.time() if when is None else when), "s": source,
                         "l": int(info["level"])}  # fmt: skip
    if not fresh:
        return 0
    with S.lock:
        fresh = {s: r for s, r in fresh.items() if s not in S.items}  # another thread may have got here first
        if not fresh:
            return 0
        S.items = {**S.items, **fresh}
        S.by_id = {**S.by_id, **{page_id(s): s for s in fresh}}
        S.version += 1
    _kick()
    if source != "before":
        names = ", ".join(sorted({(data.dex()["items"].get(r["k"]) or {}).get("name", r["k"]) for r in fresh.values()}))
        logging.info(f"[Kairosdex] Collection: {len(fresh)} new ({names})")
    return len(fresh)


def from_saves(chars: list[tuple[str, str, str, list[str], float]], bank: list[str], bank_at: float) -> None:
    """
    The save worker's read: every character's (id, name, class, held serials, save date) and the
    bank's serials. The first read after the Collection starts records everything as "before".
    """
    load()
    names = {char: {"name": name, "class": cls} for char, name, cls, _, _ in chars if name}
    if any(S.chars.get(c) != v for c, v in names.items()):
        with S.lock:
            S.chars = {**S.chars, **names}
            S.version += 1
        _kick()
    first = not S.baselined

    def one(serials: list[str], char: str, saved_at: float) -> None:
        # A save written before tracking began only holds what was had before it
        before = first or saved_at <= S.started
        add(serials, char, "before" if before else "save", 0 if before else saved_at)

    for char, _, _, serials, saved_at in chars:
        one(serials, char, saved_at)
    one(bank, BANK, bank_at)
    if first:
        with S.lock:
            S.baselined = True
            S.version += 1
        _kick()
        logging.info(f"[Kairosdex] Collection started: {len(S.items)} legendaries and pearls already had")


def from_pickup(serial: str, char: str | None) -> None:
    add([serial], char, "pickup")


def from_backpack(serials: list[str], char: str | None) -> None:
    # Before the first save read, what the backpack holds was had before tracking began
    add(serials, char, "backpack" if S.baselined else "before", None if S.baselined else 0)


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
            version, items, chars = S.version, S.items, S.chars
            started, baselined = S.started, S.baselined
        if version == written and S.game_dir is not None and (S.game_dir / PAGE_FILE).is_file():
            continue
        try:
            _write(FILE, {"version": VERSION, "started": round(started, 2), "baselined": baselined,
                          "chars": chars, "items": items}, indent=1)  # fmt: skip
            if S.game_dir is not None:
                rows = [[page_id(s), r["k"], r.get("c", ""), r.get("t", 0), r.get("l", 1), r.get("s", "")]
                        for s, r in items.items()]  # fmt: skip
                names = {c: v.get("name", "") for c, v in chars.items()}
                _write(S.game_dir / PAGE_FILE, {"v": version, "chars": names, "items": rows})
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
    """The page shows this entry (a page id): its card is made if it isn't cached."""
    load()
    S.shown = entry if entry in S.by_id else None
    if S.shown and S.shown not in S.cards and not S.status.get(S.shown):
        S.queue = [q for q in S.queue if q != S.shown] + [S.shown]
        S.status[S.shown] = "reading"


def serial_of(entry: str | None) -> str | None:
    return S.by_id.get(entry) if entry else None


def key_of(entry: str | None) -> str | None:
    """The dex item key of a Collection entry."""
    record = S.items.get(S.by_id.get(entry or "") or "")
    return record["k"] if record else None


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
    record = S.items.get(serial or "")
    if serial is None or record is None:
        return "This item isn't in the Collection any more."
    key = record["k"]
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
