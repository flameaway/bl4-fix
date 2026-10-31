"""
The teleport prompt on the challenges menu's info card.

The challenges menu lives in the game's menu page. The BL4 UI Link (uilink/, shared by our mods;
no Mod Menu needed) runs ui/challenges.js there and re-runs it whenever it changes; until the
link's pak loads (the first start after installing), the BL4 Mod Menu's own link carries the
script if it's installed. The script knows which challenges have a boss to go to
(our state lists them), adds "[T] Teleport to Core Observer" to the hovered challenge's info card
and tells us which one is hovered. No key events reach page scripts, so the key is read from
Windows here, and only counts while the script says a teleport challenge is hovered. Then
travel.py makes the trip right away, from inside the menu, like the item cards' Big Encore travel.
On the same map we then close the menu (the link's Esc, again only while the menu is still up:
one too many would open the pause menu). A fast travel to another map needs no closing: its loading
screen takes the menus down. If the trip can't start, the menu stays open and the card says why.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import health, travel, uilink

KEY = "uvhteleport"
UI_DIR = "OakGame/Content/uiresources/bl4uvhtp"
SCRIPT_URL = "coui://uiresources/bl4uvhtp/challenges.js"
UI_FILES = ("challenges.js", "challenges.css")
PUBLISH_S = 1.0
# The script repeats what's hovered a few times a second; older than this and the card is gone
HOVER_FRESH_S = 1.0
# Closing the menu after a teleport. The script reports at least once a second while the menu page
# runs (it stops once the game's menus close): if it still reports this long after an Esc (less
# the margin, for reports already on their way), the menu is still up and gets another Esc
ESC_AGAIN_S = 1.3
ESC_MARGIN_S = 0.3
ESC_MAX = 3


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    # The page files come from a source folder (dev junction), not a packed .sdkmod; set at register
    from_source = False
    # The bosses as the page gets them (bosses.json doesn't change while the game runs)
    page_bosses: list[dict[str, Any]] | None = None
    # The challenge the script last said is hovered (a bosses.json entry)
    hover: dict[str, Any] | None = None
    hover_at = 0.0
    # When the script last reported (any report)
    page_seen = 0.0
    key_down = True
    # Closing the menu after a teleport: {"presses", "at"}
    closing: dict[str, Any] | None = None
    slow_at = 0.0  # the next health / publish look (tick)
    # The last refusal, shown on the card: {"id", "title", "text"}
    note: dict[str, Any] = {"id": 0}  # noqa: RUF012
    # Set by __init__
    key: Callable[[], str | None] = lambda: None


def _changed() -> None:
    uilink.changed(KEY)


def state() -> dict[str, Any]:
    key = S.key()
    if S.page_bosses is None:
        S.page_bosses = [{k: b[k] for k in ("title", "boss", "place", "uvh")} for b in travel.bosses()]
    return {
        "key": key or "",
        "key_text": uilink.display_name(key) if key else "",
        "bosses": S.page_bosses,
        "busy": travel.busy(),
        "note": S.note,
    }


def act(action: dict[str, Any]) -> None:
    if action.get("do") != "hover":
        return
    S.page_seen = time.monotonic()
    title = action.get("title")
    boss = travel.find(str(title)) if title else None
    if boss is not S.hover:
        S.hover = boss
        _changed()
    S.hover_at = time.monotonic()


def _close_menu() -> None:
    """Esc until the game's menu is gone (one too many would open the pause menu)."""
    closing = S.closing
    if closing is None:
        return
    now = time.monotonic()
    if closing["at"] is not None:
        if now - closing["at"] < ESC_AGAIN_S:
            return
        if S.page_seen < closing["at"] + ESC_MARGIN_S:
            S.closing = None  # closed
            return
    if closing["presses"] >= ESC_MAX:
        S.closing = None
        return
    uilink.press_escape()
    closing["presses"] += 1
    closing["at"] = now


def _refuse(boss: dict[str, Any], text: str) -> None:
    S.note = {"id": S.note["id"] + 1, "title": boss["title"], "text": text}
    logging.info(f"[UVH Teleport] {text}")
    _changed()


# health and the page's files are looked at this often (they have their own slower clocks)
SLOW_S = 0.25


def tick() -> None:
    """Every frame: the link, the key, and closing the menu after a trip; health and the page's files every SLOW_S."""
    uilink.tick()
    now = time.monotonic()
    if now >= S.slow_at:
        S.slow_at = now + SLOW_S
        health.tick()
        publish()
    if S.closing is not None:
        _close_menu()
    if travel.S.hop is not None and (result := travel.take_hop_result()) is not None:
        hopped, reply = result
        if reply.get("ok"):
            logging.info(f"[UVH Teleport] At {hopped['boss']} ({hopped['place']}), moved by the host")
            if time.monotonic() - S.page_seen < ESC_MARGIN_S + 1.0:  # not after the console command
                S.closing = {"presses": 0, "at": None}
        elif reply.get("error") == "no room":
            _refuse(hopped, f"No room to stand next to {hopped['boss']}'s Big Encore machine; follow the waypoint.")
        else:
            _refuse(hopped, f"The host couldn't move you ({reply.get('error')}); follow the waypoint.")
    boss = S.hover
    if boss is None or now - S.hover_at > HOVER_FRESH_S:
        # Read the key only while a teleport challenge is hovered. Otherwise it counts as held, so
        # a press needs a release first (a key's first read after a pause also says held)
        S.key_down = True
        return
    down = uilink.key_down(S.key())
    pressed = down and not S.key_down
    S.key_down = down
    if not pressed:
        return
    try:
        there = travel.start(boss)
    except travel.Refused as ex:
        _refuse(boss, str(ex))
        return
    if there:
        S.closing = {"presses": 0, "at": None}
    S.hover = None
    _changed()


def register(game_dir: Path) -> None:
    S.game_dir = game_dir
    publish(force=True)
    uilink.register(KEY, SCRIPT_URL, state, act, views=(uilink.MENUS,), mod="UVH Boss Teleport")


def unregister() -> None:
    S.hover = None
    uilink.unregister(KEY)
    uilink.stop()


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4uvhtp/, touching only files that changed."""
    if not force and not S.from_source:
        # A packed .sdkmod can't change while the game runs: copied once, at register. Checked
        # before anything else: resources.files() on a packed mod opens its zip (every second, 0.7 ms)
        return
    now = time.monotonic()
    if S.game_dir is None or (not force and (now - S.published_at < PUBLISH_S or not uilink.polling(KEY))):
        return  # dev: only while the menu page runs the script (it re-reads the files itself)
    S.published_at = now
    root = resources.files(__package__)
    from_source = S.from_source = isinstance(root, Path)
    if not force and not from_source:
        return
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
            logging.dev_warning(f"[UVH Teleport] couldn't publish {name}: {ex}")
