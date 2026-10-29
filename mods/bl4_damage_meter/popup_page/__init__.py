"""
The Carnage Meter's boss fight pop-up: a results panel on the HUD when a boss dies (each player's
damage to it, the MVP, their top source), gone after a few seconds. Nothing to click: the full
report is on the Boss Report tab of the Carnage Report (F9).

Our script gets into the HUD page through the BL4 UI Link (../uilink, key "carnagepopup", view HUD;
__init__._sync_popup registers it), so no page of the game's is replaced. Everything is loose files
in uiresources/bl4carnage/: popup.js (the loader, fixed), popup_main.js and popup.css. The loader
polls popup.json, which holds the latest events and a stamp of our files: a changed stamp reloads
the script and styles, so they update without a restart. The panel goes on the HUD page's body.

Until 0.2.0 a pak of ours (PAK_NAME, the game's loot feed page plus one <script> for the loader)
loaded it. sync() removes that pak, after the game exits (a copy of the Kairosdex's
celebrate_page, bl4_weapondex/celebrate_page/__init__.py). The pin of the game's loot feed page
(vanilla.json, tools/gen_carnage_popup.py) stays: `page_current` (None until checked) is False once
a game update changed it, and the pop-up stays off until the mod is updated.

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
# For the status lines
WHAT = "loot feed"
CHANGED = "the HUD's loot feed page"
ON = "The boss fight pop-up is on."
OFF = "The boss fight pop-up is off."
NAME = "The boss fight pop-up"


class S:
    stamp = ""  # crc of the loose files as last written
    events: list[dict[str, Any]] = []  # noqa: RUF012 - the latest events, newest last
    lock = threading.Lock()  # the game thread posts events, a worker may sync


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


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


# Whether the game's own page is still the copy our script was written against: None until checked
page_current: bool | None = None
# Whether this game session already started the helper that applies the staged pak removal
_helper_started = False


def _staged(target: Path) -> tuple[Path, Path]:
    """`<pak>.next` (an old staged replacement) and `<pak>.remove` (delete it), applied after exit."""
    return target.with_name(target.name + ".next"), target.with_name(target.name + ".remove")


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _apply_after_exit(target: Path) -> bool:
    """
    Starts (once per game session) a hidden helper that waits until no game is running, then
    deletes `target` if it's still marked for removal (Easy Respec's helper, see
    bl4_respec/skills_page). Returns whether the helper runs.
    """
    global _helper_started
    if _helper_started:
        return True
    replacement, removal = _staged(target)
    apply = (
        f"if (Test-Path -LiteralPath {_ps_quote(removal)}) {{ "
        f"Remove-Item -LiteralPath {_ps_quote(target)} -Force -ErrorAction Stop; "
        f"Remove-Item -LiteralPath {_ps_quote(removal)}, {_ps_quote(replacement)} -Force -ErrorAction SilentlyContinue }}"
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


def retire_pak(game_dir: Path) -> str:
    """Removes our old page pak (after the game exits if it's there). Returns a note ("" = none)."""
    path = pak_path(game_dir)
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return ""
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f" The old {WHAT} page ({PAK_NAME}) is removed when the game exits (the UI Link replaces it)."
    return f" Couldn't remove the old {WHAT} page ({PAK_NAME}) while the game is running."


def check_page(game_dir: Path) -> bool:
    """Whether the game's own page (its paks, not ~mods) is the copy our script was written for."""
    expected = json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))["sha1"]
    paks_dir = game_dir / "OakGame" / "Content" / "Paks"
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + PAGE)
    return game_copy is not None and game_copy.sha1 == expected


def sync(game_dir: Path, enabled: bool) -> str:
    """
    Removes the old pak, checks the game's page and writes (or blanks) our loose files to match
    `enabled`. Returns a status line; `page_current` says whether the link should run our script.
    """
    global page_current
    retired = retire_pak(game_dir)
    if not enabled:
        page_current = None
        remove_loose_files(game_dir)
        return f"{OFF}{retired}"
    page_current = check_page(game_dir)
    if not page_current:
        remove_loose_files(game_dir)
        return f"{NAME} paused: this game version changed {CHANGED}, so the mod needs an update.{retired}"
    write_loose_files(game_dir)
    return f"{ON}{retired}"
