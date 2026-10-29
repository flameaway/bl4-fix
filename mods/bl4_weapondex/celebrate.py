"""
Celebrations: the game's own big HUD banner (celebrate_page/ draws it, in the HUD page through
the BL4 UI Link: link key LINK_KEY) when a boss is beaten for
the first time on this account, or a legendary or pearl is obtained for the first time (with
"Per-character Kairosdex" on: the first time for the character being played).

- Bosses: __init__.beaten() asks first_kill() before it records the win, then calls boss().
- Items: the backpack is checked twice a second for items that weren't in it before. One raw read
  of every slot's item handle (the slot layout is learned once per world from the reflected
  fields, and checked against them), so a check costs a few hundredths of a millisecond. A new
  handle's serial says which item it is; a dex item never obtained before (owned.py) is recorded
  and celebrated. What's in the backpack when the check starts (a world load, the mod starting) is
  the baseline and never celebrated, so a world load or a hot reload can't replay anything.

Local only: each player sees their own banners, host or client (the backpack replicates to its
owner).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging

from . import celebrate_page, data, owned, progress, savegame, stats, uilink

CHECK_S = 0.5
MAX_NEW_PER_CHECK = 8  # a full backpack showing up at once (a load) is a baseline, not 90 banners
PUBLISH_S = 1.0  # dev: our page files are copied again this often when they change
UI_DIR = "OakGame/Content/uiresources/bl4weapondex"
ICON_BOSS = "coui://uiresources/_shared/assets/ico_ui_art_discovery/ico_menu_target_dummies.png"
ICON_ITEM = "coui://uiresources/_shared/assets/ico_ui_art_challenges/ico_challenge_big_loot.png"
ICON_PEARL = "common://uiresources/dlc_rarity_pips/assets/ico_misc_pearl.png"
LABEL = "KAIROSDEX"
# The loader, run in the HUD page by the BL4 UI Link while celebrations are on and the game's
# banner page is the one it was written for (celebrate_page.page_current)
LINK_KEY = "kdxcelebrate"
LINK_SCRIPT = "coui://uiresources/bl4weapondex/celebrate.js"


class S:
    game_dir: Path | None = None
    # Set by __init__: whether the "Celebrations" option is on
    enabled: Callable[[], bool] = lambda: False  # noqa: E731
    sync_lock = threading.Lock()
    sync_wanted = 0  # bumped per sync request: only the latest one runs
    next_check = 0.0
    published_at = 0.0
    published: tuple[Any, ...] = ()  # (size, mtime) of the page files last copied (dev)
    # The backpack: (PlayerState, first slot, slot count) the layout below was learned for
    layout: tuple[int, int, int] | None = None
    stride = 0  # bytes from one slot to the next
    offset = 0  # the item handle's offset in a slot
    width = 0  # the handle's size in bytes; 0 = the raw read didn't check out (reflected reads)
    handles: frozenset[int] | None = None  # the items in it at the last check; None = take a baseline


def setup(game_dir: Path, enabled: Callable[[], bool]) -> None:
    S.game_dir, S.enabled = game_dir, enabled


def sync(on: bool, wait: bool = False) -> None:
    """
    Installs or removes the banner page (a pak, read at the next start) and our loose files. On a
    worker thread (the pak check reads the game's pak indexes), unless `wait`; the latest request
    wins. Turning off is quick (small files only), so disabling the mod waits for it.
    """
    game_dir = S.game_dir
    if game_dir is None:
        return
    S.sync_wanted += 1
    ticket = S.sync_wanted

    def run() -> None:
        with S.sync_lock:
            if ticket != S.sync_wanted:
                return  # a later request replaces this one
            try:
                logging.info(f"[Kairosdex] {celebrate_page.sync(game_dir, on)}")
            except Exception as ex:  # noqa: BLE001 - the dex works without it
                logging.error(f"[Kairosdex] Celebrations: couldn't set up the banner page: {ex!r}")
            _link(on and celebrate_page.page_current is True)

    if wait:
        run()
    else:
        threading.Thread(target=run, name="bl4_weapondex celebrate sync", daemon=True).start()


def _link(on: bool) -> None:
    """Has the link run the loader in the HUD page (or tear it down there)."""
    if on:
        uilink.register(LINK_KEY, LINK_SCRIPT, dict, lambda _action: None, views=(uilink.HUD,), mod="Kairosdex")
    else:
        uilink.unregister(LINK_KEY)
        uilink.stop()  # the link's server goes once no script of any mod is left


def forget() -> None:
    S.layout, S.handles = None, None


# region Events


def _picture(folder: str, key: str) -> str:
    """The baked picture's URL (cache-busted by its time), or "" if there's none yet."""
    if S.game_dir is None:
        return ""
    try:
        stamp = int((S.game_dir / UI_DIR / folder / f"{key}.png").stat().st_mtime)
    except OSError:
        return ""
    return f"coui://uiresources/bl4weapondex/{folder}/{key}.png?v={stamp}"


def _post(event: dict[str, Any]) -> None:
    if S.game_dir is None:
        return
    # Unique across game sessions and hot reloads: the page remembers ids it has shown
    event["id"] = str(time.time_ns())
    try:
        celebrate_page.post(S.game_dir, event)
    except OSError as ex:
        logging.error(f"[Kairosdex] Celebrations: couldn't write the banner: {ex!r}")


def _beaten(char: str | None) -> set[str]:
    """Bosses beaten on the account, or by `char` alone ("Per-character Kairosdex")."""
    kills = progress.defeated(char) if char is not None else progress.defeated_anywhere()
    return set(kills) | set(savegame.beaten(char))


def first_kill(boss_id: str) -> bool:
    """
    Whether no character on this account (or, per character, this one) has beaten this boss yet,
    as far as the dex knows.
    """
    return boss_id not in _beaten(progress.scope())


def boss(boss_id: str) -> None:
    if not S.enabled():
        return
    bosses = data.dex()["bosses"]
    record = next((b for b in bosses if b["id"] == boss_id), {})
    beaten = _beaten(progress.scope()) & {b["id"] for b in bosses}
    _post({
        "kind": "boss",
        "verb": "FIRST KILL",
        "name": data.boss_name(boss_id),
        "sub": record.get("area") or "",
        "label": LABEL,
        "amount": f"{len(beaten)} / {len(bosses)}",
        "picture": _picture("bosses", boss_id),
        "icon": ICON_BOSS,
    })


def item(key: str) -> None:
    """A dex item obtained for the first time (already recorded in owned.py)."""
    if not S.enabled():
        return
    items = data.dex()["items"]
    record = items.get(key)
    if record is None:
        return
    pearl = record.get("rarity") == "Pearlescent"
    sub = " ".join(part for part in (record.get("maker"), record.get("type")) if part)
    _post({
        "kind": "pearl" if pearl else "legendary",
        "verb": "NEW PEARLESCENT" if pearl else "NEW LEGENDARY",
        "name": record.get("name") or key,
        "sub": sub,
        "label": LABEL,
        "amount": f"{len(owned.items(progress.scope()) & items.keys())} / {len(items)}",
        "picture": _picture("items", key),
        "icon": ICON_PEARL if pearl else ICON_ITEM,
    })


def test(kind: str, name: str = "") -> str:
    """Dev: shows a banner now (`dex celebrate boss|legendary|pearl [name]`). Records nothing."""
    if kind == "boss":
        boss_id = data.find_boss(name) if name else data.dex()["bosses"][0]["id"]
        if boss_id is None:
            return f"No boss called {name!r}."
        enabled, S.enabled = S.enabled, lambda: True
        try:
            boss(boss_id)
        finally:
            S.enabled = enabled
        return f"Showing {data.boss_name(boss_id)}."
    items = data.dex()["items"]
    want_pearl = kind == "pearl"
    words = name.lower()
    for key, record in items.items():
        if (record.get("rarity") == "Pearlescent") == want_pearl and words in (record.get("name") or "").lower():
            enabled, S.enabled = S.enabled, lambda: True
            try:
                item(key)
            finally:
                S.enabled = enabled
            return f"Showing {record.get('name')}."
    return f"No {'pearl' if want_pearl else 'legendary'} called {name!r}."


# endregion
# region The backpack


def tick() -> None:
    if S.game_dir is None or not S.enabled():
        S.handles = None
        return
    now = time.monotonic()
    if now < S.next_check:
        return
    S.next_check = now + CHECK_S
    if celebrate_page.FROM_SOURCE and celebrate_page.page_current and now - S.published_at >= PUBLISH_S:
        S.published_at = now
        _publish_dev()
    try:
        _check()
    except Exception as ex:  # noqa: BLE001 - never break the tick; start over next time
        forget()
        logging.dev_warning(f"[Kairosdex] backpack check: {ex!r}")


def _publish_dev() -> None:
    """From a source folder: copy our page files again when they change (hot reload)."""
    files = [celebrate_page.ASSETS / asset for _, asset in celebrate_page.LOOSE_FILES]
    stamp = tuple((f.stat().st_size, f.stat().st_mtime_ns) for f in files)
    if stamp != S.published and S.game_dir is not None:
        S.published = stamp
        celebrate_page.write_loose_files(S.game_dir)


def _learn(slots: Any, count: int, first: int) -> None:
    """Where each slot's item handle is, checked against the reflected values of every slot."""
    S.stride = slots[1]._get_address() - first if count > 1 else 0  # noqa: SLF001
    S.offset = slots[0].InventoryItem.Handle._get_address() - first  # noqa: SLF001
    S.width = 0
    if count > 1 and S.stride <= 0:
        return
    reflected = [int(slots[i].InventoryItem.Handle.Handle) for i in range(count)]
    raw = stats._read(first, S.stride * (count - 1) + S.offset + 8) if count > 1 else stats._read(first, S.offset + 8)  # noqa: SLF001
    if raw is None:
        return
    for width in (4, 8):
        values = [int.from_bytes(raw[i * S.stride + S.offset : i * S.stride + S.offset + width], "little", signed=True) for i in range(count)]
        if values == reflected:
            S.width = width
            return
    logging.dev_warning("[Kairosdex] backpack: raw handles don't match, reading them one by one")


def _handles(slots: Any, count: int, first: int) -> list[int] | None:
    if S.width:
        raw = stats._read(first + S.offset, S.stride * (count - 1) + S.width)  # noqa: SLF001
        if raw is None:
            return None
        return [int.from_bytes(raw[i * S.stride : i * S.stride + S.width], "little", signed=True) for i in range(count)]
    # Fallback: reflected reads (slower, still only twice a second)
    return [int(slots[i].InventoryItem.Handle.Handle) for i in range(count)]


def _check() -> None:
    pc = get_pc(possibly_loading=True)
    state = getattr(pc, "PlayerState", None) if pc is not None else None
    if state is None:
        forget()
        return
    slots = state.BackpackItems.items
    count = len(slots)
    if count == 0:
        forget()
        return
    first = slots[0]._get_address()  # noqa: SLF001
    layout = (state._get_address(), first, count)  # noqa: SLF001
    if layout != S.layout:
        # A new world or character: a new baseline. The same PlayerState with the array grown or
        # moved is a pickup into a full backpack (the array has no empty slots then): keep the
        # handles, or the baseline would swallow the new item
        if S.layout is None or S.layout[0] != layout[0]:
            S.handles = None
        S.layout = layout
        _learn(slots, count, first)
    handles = _handles(slots, count, first)
    if handles is None:
        return
    present = frozenset(h for h in handles if h != -1)
    if S.handles is None:
        S.handles = present
        return
    new = present - S.handles
    S.handles = present
    if not new or len(new) > MAX_NEW_PER_CHECK:
        return
    for index, handle in enumerate(handles):
        if handle in new:
            _new_item(slots[index], handle)


def _new_item(slot: Any, handle: int) -> None:
    item_ = slot.InventoryItem
    if int(item_.Handle.Handle) != handle:  # the raw read was off: learn the layout again
        forget()
        return
    serial = owned.serial_of(item_)
    if not serial:
        return
    # Per character: new to this character's record (none while it hasn't started)
    for key in owned.add_serials([serial], progress.scope()):
        logging.info(f"[Kairosdex] First time: {(data.dex()['items'].get(key) or {}).get('name', key)}")
        item(key)


# endregion
