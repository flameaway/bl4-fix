"""
Screens other mods add to the HTML mod menu's page.

The mod menu's page (the game's menu page, where the pause menu lives) is the one place our HTML
runs inside the game's menus, and it's reached through this mod's shim pak. So another mod that
wants a screen there doesn't ship a pak of its own: it registers a page script here.

    import sys
    if (exts := sys.modules.get("bl4_mod_menu.extensions")) is not None:
        exts.register("weapondex", "coui://uiresources/bl4weapondex/dex.js", get_state, on_action)

- The page loads every registered script (ui/extensions.js does, and re-runs a script whenever its
  file changes, so it can be edited while the game runs).
- The script polls `GET /ext?k=<key>&v=<rev>` for `{"menus": bool, "rev": n, "state": get_state()}`,
  or just `{"menus": bool, "same": n}` while the state is still revision `v` (ui/extensions.js
  keeps the last state and parses it again, so a script sees a fresh object either way). get_state runs
  on the game thread, only when a poll asks for it: each GET waits (up to WAIT_S, normally one
  frame) for the next tick, which rebuilds the state if changed(key) was called or the last build
  is FRESH_S old. Nobody polling = no builds. It may read the game; keep it small, and put large
  static data in a loose file the script fetches itself.
- The script posts `{"op": "ext", "ext": <key>, ...}` to /action; on_action gets that dict on the
  game thread.

Plain Python, no game access of its own: web.py calls handle_get() from its server thread, and
apply() and tick() from the game thread.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from unrealsdk import logging

# A script that polled within this long counts as running (the page sleeps while no menu shows)
POLLING_S = 3.0
# A poll gets the state built up to this long ago, unless changed() said it moved on since
FRESH_S = 0.25
# How long a poll waits for the game thread to answer it (normally the next frame). Past this it
# gets the last snapshot (a loading screen, a long hitch)
WAIT_S = 0.2
# State revisions: start from the clock so a page that kept a revision from an older server (hot
# reload, a new game) can't match one of ours by chance
_revs = itertools.count(int(time.time() * 1000))


@dataclass
class Extension:
    key: str
    script: str
    state: Callable[[], Any]
    act: Callable[[dict[str, Any]], None]
    # json of the last state() (b"" = never built)
    state_json: bytes = b""
    # (revision, json): set together, so the server thread never pairs one with the other's
    snap: tuple[int, bytes] = (0, b"")
    built_at: float = 0.0
    dirty: bool = True
    last_poll: float = 0.0
    # The script says its screen is showing (it polls with open=1)
    page_open: bool = False
    # Polls waiting for the game thread: tickets handed out (server thread) and the last one the
    # game thread answered. Both change under _cond.
    asked: int = 0
    answered: int = 0


_lock = threading.Lock()
_cond = threading.Condition(_lock)
# What the game thread last saw; put in every answer, so it's current even for a cached state
_menus_up = False
# Kept through a hot reload of this module (importlib.reload re-runs it in the same namespace), so
# the mods that registered don't have to register again. Carried over into this version's class.
_extensions: dict[str, Extension] = globals().get("_extensions") or {}
for _key, _old in list(_extensions.items()):
    _new = Extension(_old.key, _old.script, _old.state, _old.act)
    _new.last_poll, _new.page_open = getattr(_old, "last_poll", 0.0), getattr(_old, "page_open", False)
    _extensions[_key] = _new


def register(key: str, script: str, state: Callable[[], Any], act: Callable[[dict[str, Any]], None]) -> None:
    """Adds (or replaces, e.g. after a hot reload) a page script. script is a coui:// URL."""
    with _lock:
        old = _extensions.get(key)
        ext = Extension(key, script, state, act)
        if old is not None:
            ext.last_poll, ext.page_open = old.last_poll, old.page_open
        _extensions[key] = ext


def unregister(key: str) -> None:
    with _lock:
        _extensions.pop(key, None)


def changed(key: str) -> None:
    """The state changed: the next poll gets a fresh build, however recent the last one is."""
    ext = _extensions.get(key)
    if ext is not None:
        ext.dirty = True


def polling(key: str, within: float = POLLING_S) -> bool:
    """
    The page is running this extension's script right now (one of the game's menus is up): it
    polled within this many seconds. Pass about the script's own poll interval for an answer
    that goes false soon after the menus close.
    """
    ext = _extensions.get(key)
    return ext is not None and time.monotonic() - ext.last_poll < within


def page_open(key: str, within: float = POLLING_S) -> bool:
    """The script last said its screen is showing (only meaningful while polling())."""
    ext = _extensions.get(key)
    return ext is not None and ext.page_open and polling(key, within)


# region Server thread


def handle_get(path: str) -> bytes:
    """GET /ext: the registered scripts ({key: url}); with k=<key>, that extension's snapshot."""
    query = parse_qs(path.partition("?")[2])
    key = (query.get("k") or [""])[0]
    if not key:
        with _lock:
            listing = {k: e.script for k, e in _extensions.items()}
        return json.dumps(listing).encode("utf-8")
    ext = _extensions.get(key)
    if ext is None:
        return b"null"
    ext.last_poll = time.monotonic()
    ext.page_open = (query.get("open") or ["0"])[0] == "1"
    # Wait for the game thread's next tick: it applies actions posted before this poll, then
    # rebuilds the state if it's stale. The game thread never waits for us.
    with _cond:
        ext.asked += 1
        ticket = ext.asked
        _cond.wait_for(lambda: ext.answered >= ticket, WAIT_S)
    rev, state = ext.snap
    if not state:
        return b"{}"
    menus = b"true" if _menus_up else b"false"
    # Unchanged since the page's copy: say so instead of sending it again. Reroll polls every 60 ms
    # and the dex every 100 ms; the game's page kept ~24 of those full answers a minute in memory
    # that was never given back (~600 MB in a session, 2026-10-24)
    if (query.get("v") or [""])[0] == str(rev):
        return b'{"menus":' + menus + b',"same":' + str(rev).encode() + b"}"
    # "state" last: the page cuts its text out of the answer as is
    return b'{"menus":' + menus + b',"rev":' + str(rev).encode() + b',"state":' + state + b"}"


# endregion
# region Game thread


def apply(action: dict[str, Any]) -> None:
    ext = _extensions.get(str(action.get("ext")))
    if ext is None:
        return
    try:
        ext.act(action)
    except Exception as ex:  # noqa: BLE001 - one broken extension shouldn't break the menu
        logging.error(f"[Mod Menu] {ext.key} failed: {ex!r}")
        logging.exception(ex)
    ext.dirty = True


def _build(ext: Extension, now: float) -> None:
    ext.dirty = False
    ext.built_at = now
    try:
        state = ext.state()
        text = json.dumps(state, separators=(",", ":"), default=str).encode("utf-8")
        if text != ext.state_json:
            ext.snap = (next(_revs), text)
        ext.state_json = text
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Mod Menu] {ext.key} state failed: {ex!r}")


def tick(menus_up: bool) -> None:
    """
    Call every frame, after the actions: answers the polls that came in since the last frame,
    rebuilding an extension's state only when a poll asked and it changed or is FRESH_S old.
    """
    global _menus_up  # noqa: PLW0603
    _menus_up = menus_up
    now = time.monotonic()
    with _lock:
        exts = list(_extensions.values())
    answered: list[tuple[Extension, int]] = []
    for ext in exts:
        asked = ext.asked
        if asked == ext.answered:
            continue
        if ext.dirty or not ext.state_json or now - ext.built_at >= FRESH_S:
            _build(ext, now)
        answered.append((ext, asked))
    if answered:
        with _cond:
            for ext, asked in answered:
                ext.answered = asked
            _cond.notify_all()


# endregion
