"""
The five build squares in the inventory, under the Reward Center button.

The inventory lives in the game's menu page. The BL4 UI Link (uilink/, shared by our mods; no
Mod Menu needed) runs ui/builds.js there and re-runs it whenever it changes; the script polls our
state (what's in each square) and posts clicks back here. Until the link's pak loads (the first
start after installing), the BL4 Mod Menu's own link carries the script if it's installed.
Nothing to restart for: edit ui/builds.js or builds.css and the page picks it up within a couple
of seconds.

Clicks: a saved square loads its build, an empty one saves into it; the small buttons under a
square save over it, name it or clear it. When saving, the script also sends each equipped item's
name as the game's own item card shows it (the game names items natively; nothing reflected
does), so a square can list "Hellwalker" rather than a serial.

Right-click a square to save over it: no right-button events reach page scripts, so we read the
button from Windows (like the respec mod) while the script says the mouse is on a square, and
send a prompt in the state; the page asks "Save over Build N?" and saves if you say yes.

Naming: "Name" starts typing (keyboard.py reads the keys from Windows and keeps them from the
game); the text goes to the page in the state, Enter keeps it, Esc cancels, and so does closing
the menu.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import builds, health, keyboard, uilink

KEY = "builds"
UI_DIR = "OakGame/Content/uiresources/bl4builds"
SCRIPT_URL = "coui://uiresources/bl4builds/builds.js"
UI_FILES = ("builds.js", "builds.css")
PUBLISH_S = 1.0


class S:
    game_dir: Path | None = None
    published_at = 0.0
    published: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - file -> (size, mtime) last copied
    toast: dict[str, Any] = {"id": 0}  # noqa: RUF012
    # Set by __init__: whether loading includes skills / specializations
    options: Callable[[], tuple[bool, bool]] = lambda: (True, True)
    # The build being named, and its typing
    naming: int = 0
    typing: keyboard.Typing | None = None
    # The square the mouse is on (the script says), and the save-over prompts sent for right-clicks
    hover: int = 0
    prompt: dict[str, Any] = {"id": 0, "n": 0}  # noqa: RUF012
    right_down = True


def _changed() -> None:
    uilink.changed(KEY)


def say(title: str, details: list[str] | None = None, *, error: bool = False) -> None:
    S.toast = {"id": S.toast["id"] + 1, "title": title, "details": details or [], "error": error}
    (logging.error if error else logging.info)(f"[Build Presets] {title} {' '.join(details or [])}".strip())
    _changed()


def state() -> dict[str, Any]:
    """What the page shows. Cheap: the squares come from builds' cache (see builds.watch)."""
    naming = {"n": S.naming, "text": S.typing.text} if S.typing is not None else None
    return {"blocked": builds.blocked(cached=True), "squares": builds.summaries(cached=True), "toast": S.toast,
            "naming": naming, "prompt": S.prompt}


# region Naming


def start_naming(n: int) -> None:
    stop_naming(keep=False)
    char = builds.game.character()
    build = builds.presets.get(char, n) if char else None
    if build is None:
        return
    # Starts its hook on its own thread; a hook Windows refused shows up in _poll_naming
    typing = keyboard.Typing(build.get("name") or "", builds.MAX_NAME)
    S.naming, S.typing = n, typing
    _changed()


def stop_naming(*, keep: bool) -> None:
    typing, n = S.typing, S.naming
    S.typing, S.naming = None, 0
    if typing is None:
        return
    typing.stop()
    if keep:
        try:
            say(builds.rename(n, typing.text))
        except builds.Refused as ex:
            say(str(ex), error=True)
    _changed()


def _poll_naming() -> None:
    typing = S.typing
    if typing is None:
        return
    if typing.failed:
        stop_naming(keep=False)
        say("Couldn't read the keyboard for typing; use `build rename` in the console.", error=True)
        return
    # The menu closed (the page stopped polling): don't keep swallowing keys
    if not uilink.polling(KEY):
        stop_naming(keep=False)
        return
    if typing.poll():
        _changed()
    if typing.done is not None:
        stop_naming(keep=typing.done)


# endregion


VK_RBUTTON = 0x02


def _poll_right_click() -> None:
    """A right-click on a square (pressed, not held) asks the page to confirm saving over it."""
    # Read only while the script says the mouse is on a square. Off a square the button counts as
    # held, so a press needs a release first (a key's first read after a pause also says held)
    if not S.hover:
        S.right_down = True
        return
    down = uilink.vk_down(VK_RBUTTON)
    pressed = down and not S.right_down
    S.right_down = down
    if not pressed or not S.hover or S.typing is not None or not uilink.polling(KEY):
        return
    S.prompt = {"id": S.prompt["id"] + 1, "n": S.hover}
    _changed()


def act(action: dict[str, Any]) -> None:
    if action.get("do") == "hover":
        n = action.get("n")
        S.hover = n if isinstance(n, int) and 1 <= n <= builds.presets.SLOTS else 0
        return
    if action.get("do") == "name_end":
        stop_naming(keep=bool(action.get("keep")))
        return
    n = action.get("n")
    if not isinstance(n, int) or not 1 <= n <= builds.presets.SLOTS:
        return
    try:
        match action.get("do"):
            case "save":
                labels = action.get("labels")
                say(builds.save(n, labels if isinstance(labels, dict) else None))
            case "load":
                skills, specs = S.options()
                headline, details = builds.load(n, skills=skills, specializations=specs,
                                                on_done=lambda h, d, e: say(h, d, error=e))  # fmt: skip
                say(headline, details)
            case "clear":
                say(builds.clear(n))
            case "name":
                start_naming(n)
            case _:
                return
    except builds.Refused as ex:
        say(str(ex), error=True)


def register(game_dir: Path) -> None:
    S.game_dir = game_dir
    publish(force=True)
    uilink.register(KEY, SCRIPT_URL, state, act, views=(uilink.MENUS,), mod="Build Presets")


def unregister() -> None:
    stop_naming(keep=False)
    S.hover = 0
    uilink.unregister(KEY)
    uilink.stop()


def tick() -> None:
    """Every frame: the link, the page's files, the squares' cache, the typing."""
    uilink.tick()
    health.tick()
    publish()
    if builds.watch():
        _changed()
    _poll_naming()
    _poll_right_click()


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4builds/, touching only files that changed."""
    now = time.monotonic()
    if S.game_dir is None or (not force and now - S.published_at < PUBLISH_S):
        return
    S.published_at = now
    root = resources.files(__package__)
    from_source = isinstance(root, Path)
    if not force and not from_source:
        return  # a packed .sdkmod can't change while the game runs: copied once, at register
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
            logging.dev_warning(f"[Build Presets] couldn't publish {name}: {ex}")
