"""
The Carnage Meter's boss fight pop-up: a results panel on the HUD when a boss dies (each player's
damage to it, the MVP, their top source), gone after a few seconds. Nothing to click: the full
report is on the Boss Report tab of the Carnage Report (F9).

The HUD page is HTML, reached through one widget the game always loads there, the loot feed
(loot_feed/loot_feed.html). We ship that page from a pak of ours (tools/gen_carnage_popup.py: the
game's copy plus one <script> for our loader). Everything else is loose files in
uiresources/bl4carnage/: popup.js (the loader, fixed), popup_main.js and popup.css. The loader
polls popup.json, which holds the latest events and a stamp of our files: a changed stamp reloads
the script and styles, so they update without a restart. Paks are only read at startup, so the
page itself (and the first install) applies the next time the game starts.

A copy of the Kairosdex's celebrate_page (bl4_weapondex/celebrate_page/__init__.py), which follows
Item Cards' staging: to never undo a game update, the pak is only installed while the game's own
page is still exactly the copy ours was made from (vanilla.json). If a patch changes it, or another
mod also replaces the page, the pak is removed and the pop-up stays off until the mod is updated.

Plain Python only (file I/O, no unreal calls, Python 3.11 for the installer), so this is safe to
run from any thread.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import zlib
from importlib import resources
from pathlib import Path
from typing import Any

from .. import pak

ASSETS = resources.files(__name__)
PAK_NAME = "pakchunk0-Windows_4249_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
PAGE = "loot_feed/loot_feed.html"
# Our own new files, written loose: (path in the game's uiresources folder, asset name)
LOOSE_FILES = [
    ("bl4carnage/popup.js", "popup.js"),
    ("bl4carnage/popup_main.js", "popup_main.js"),
    ("bl4carnage/popup.css", "popup.css"),
]
# What the loader polls: {"stamp": <our files' crc>, "events": [...]}
STATE_PATH = "bl4carnage/popup.json"
# True when running from a source folder (a dev junction) rather than a packed .sdkmod, whose
# files can't change while the game runs
FROM_SOURCE = isinstance(ASSETS, Path)
NAME = "The boss fight pop-up"


class S:
    stamp = ""  # crc of the loose files as last written
    events: list[dict[str, Any]] = []  # noqa: RUF012 - the latest events, newest last
    lock = threading.Lock()  # the game thread posts events, a worker may sync


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


def build_pak() -> bytes:
    return pak.build_pak("../../../" + UI_PREFIX, {PAGE: (ASSETS / "loot_feed.html").read_bytes()}, PAK_NAME)


def _write_if_changed(path: Path, data: bytes) -> None:
    try:
        if path.read_bytes() == data:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _write_state(game_dir: Path) -> None:
    text = json.dumps({"stamp": S.stamp, "events": S.events}, separators=(",", ":"))
    _write_if_changed(game_dir / UI_PREFIX / STATE_PATH, text.encode("utf-8"))


def write_loose_files(game_dir: Path) -> None:
    """Copies our script and styles (only files that changed) and moves the stamp if they did."""
    datas = []
    for path, asset in LOOSE_FILES:
        datas.append((ASSETS / asset).read_bytes())
        _write_if_changed(game_dir / UI_PREFIX / path, datas[-1])
    stamp = f"{zlib.crc32(b''.join(datas)):08x}"
    with S.lock:
        if stamp != S.stamp or not (game_dir / UI_PREFIX / STATE_PATH).exists():
            S.stamp = stamp
            _write_state(game_dir)


def post(game_dir: Path, event: dict[str, Any], keep: int = 3) -> None:
    """Adds an event for the page (it shows the ones it hasn't seen yet, if they're fresh)."""
    with S.lock:
        S.events = [*S.events, {**event, "t": round(time.time(), 2)}][-keep:]
        _write_state(game_dir)


# What the script and styles become while we're off. The page (ours until the next start) only
# reloads files it can read, so a deleted script would keep the running copy going; an empty one
# makes the loader dispose it.
OFF_FILES = {
    "bl4carnage/popup_main.js": b"// The Carnage Meter's boss fight pop-up is off.\n",
    "bl4carnage/popup.css": b"/* The Carnage Meter's boss fight pop-up is off. */\n",
}


def remove_loose_files(game_dir: Path) -> None:
    for path, data in OFF_FILES.items():
        target = game_dir / UI_PREFIX / path
        if target.exists():
            _write_if_changed(target, data)
    with S.lock:
        S.stamp, S.events = "off", []
        if (game_dir / UI_PREFIX / STATE_PATH).exists():
            _write_state(game_dir)


# Whether this game session already started the helper that applies staged pak changes
_helper_started = False


def _staged(target: Path) -> tuple[Path, Path]:
    """`<pak>.next` (replace it with this) and `<pak>.remove` (delete it), applied after exit."""
    return target.with_name(target.name + ".next"), target.with_name(target.name + ".remove")


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _apply_after_exit(target: Path) -> bool:
    """
    Starts (once per game session) a hidden helper that waits until no game is running, then
    applies whatever is staged next to `target` at that point (Easy Respec's helper, see
    bl4_respec/skills_page). Returns whether the helper runs.
    """
    global _helper_started
    if _helper_started:
        return True
    replacement, removal = _staged(target)
    apply = (
        f"if (Test-Path -LiteralPath {_ps_quote(removal)}) {{ "
        f"Remove-Item -LiteralPath {_ps_quote(target)} -Force -ErrorAction Stop; "
        f"Remove-Item -LiteralPath {_ps_quote(removal)}, {_ps_quote(replacement)} -Force -ErrorAction SilentlyContinue }} "
        f"elseif (Test-Path -LiteralPath {_ps_quote(replacement)}) {{ "
        f"Move-Item -LiteralPath {_ps_quote(replacement)} -Destination {_ps_quote(target)} -Force -ErrorAction Stop }}"
    )
    script = (
        # Wait for every copy of the game, not our own PID: a dev tool can call this from outside it
        "while (Get-Process -Name Borderlands4 -ErrorAction SilentlyContinue) { Start-Sleep 2 }; "
        f"for ($i = 0; $i -lt 30; $i++) {{ try {{ {apply}; break }} catch {{ Start-Sleep 1 }} }}"
    )
    try:
        # The game runs in a job that doesn't allow breaking away, so no CREATE_BREAKAWAY_FROM_JOB;
        # the helper still outlives a normal game exit
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        return False
    _helper_started = True
    return True


def _install(path: Path, data: bytes) -> str:
    replacement, removal = _staged(path)
    removal.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Nothing mounted it yet, so it's safe to put in place now
        replacement.unlink(missing_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return f"{NAME} installed: restart the game once to see it."
    if path.read_bytes() == data:
        replacement.unlink(missing_ok=True)
        return f"{NAME} is on."
    replacement.write_bytes(data)
    if _apply_after_exit(path):
        return f"{NAME} updated: the new page loads the next time the game starts."
    return f"{NAME}: couldn't update its page while the game is running."


def _remove(path: Path, reason: str) -> str:
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return reason
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f"{reason} The game's own loot feed page comes back the next time the game starts."
    return f"{reason} Couldn't remove {path.name} while the game is running."


def sync(game_dir: Path, enabled: bool) -> str:
    """Installs or removes our pak to match `enabled` and the installed game. Returns a status line."""
    ours = pak_path(game_dir)
    if not enabled:
        remove_loose_files(game_dir)
        return _remove(ours, f"{NAME} is off.")

    paks_dir = ours.parent.parent
    expected = json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))["sha1"]
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + PAGE)
    if game_copy is None or game_copy.sha1 != expected:
        remove_loose_files(game_dir)
        return _remove(ours, f"{NAME} paused: this game version changed the HUD's loot feed page, so the mod needs an update.")

    others = [
        other.name
        for other in sorted(ours.parent.glob("*.pak"))
        if other.name.lower() != ours.name.lower() and pak.winning_entry([other], UI_PREFIX + PAGE) is not None
    ]
    if others:
        remove_loose_files(game_dir)
        return _remove(ours, f"{NAME} paused: {', '.join(others)} in Paks/~mods also changes the HUD's loot feed page.")

    write_loose_files(game_dir)
    return _install(ours, build_pak())
