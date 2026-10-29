"""
The Skills page's right-click respec: our loose page files, and the check that they still fit the
game's Skills menu.

The scripts and stylesheet are loose files in uiresources/bl4respec/: the UI reads loose files there
straight from disk. respec.js is a small loader that pulls in respec_main.js and respec.css and
reloads them whenever they change, so updating them needs no restart. Since 0.2.0 the BL4 UI Link
(../uilink, shared by our mods) runs respec.js in the menu page, without replacing any game page.

Until 0.1.x the loader got there through our own copy of the game's menu_skills.html in a pak
(pakchunk0-Windows_4244_P.pak), which clashed with any other mod replacing the Skills page. sync()
now only removes that pak: the game reads a pak's index once at startup and keeps reading file data
at those offsets, so an existing pak is only removed after the game exits (a `.remove` marker and a
hidden helper; with no game running, as in the setup app, at once).

The script is written against the game's own Skills menu: `page_current` says whether the game
still ships exactly the copy it was checked with (vanilla.json, tools/gen_respec_page.py). After a
game update that changed it the mod steps aside until it's updated (health shows "Needs update").

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread, and the
setup app imports it (installer/core.py PAGE_MODULES): keep it plain Python 3.11.
"""

from __future__ import annotations

import json
import subprocess
import zlib
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
# The old Skills page pak (until 0.1.x), removed after exit
PAK_NAME = "pakchunk0-Windows_4244_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
PAGE = "menu_skills/menu_skills.html"
# Our own new files, written loose: (path in the game's uiresources folder, asset name)
LOOSE_FILES = [
    ("bl4respec/respec.js", "respec.js"),
    ("bl4respec/respec_main.js", "respec_main.js"),
    ("bl4respec/respec.css", "respec.css"),
]
# True when running from a source folder (a dev junction) rather than a packed .sdkmod, whose
# files can't change while the game runs
FROM_SOURCE = isinstance(ASSETS, Path)
# Also loose: the file the page polls for right clicks; see SignalFile in __init__.py
SIGNAL_PATH = "bl4respec/right_click.txt"
# Rewritten after the loose files change: the loader polls this, not the files (it read ~20 KB a
# second for as long as the Skills page lived)
STAMP_PATH = "bl4respec/stamp.txt"


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


def signal_path(game_dir: Path) -> Path:
    return game_dir / UI_PREFIX / SIGNAL_PATH


# Whether the game's own Skills page is still the one respec_main.js was checked with: None until
# sync() looked (read by the health probes, shared/health)
page_current: bool | None = None


def _write_if_changed(path: Path, data: bytes) -> None:
    try:
        if path.read_bytes() == data:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_loose_files(game_dir: Path) -> None:
    datas = []
    for path, asset in LOOSE_FILES:
        datas.append((ASSETS / asset).read_bytes())
        _write_if_changed(game_dir / UI_PREFIX / path, datas[-1])
    _write_if_changed(game_dir / UI_PREFIX / STAMP_PATH, f"{zlib.crc32(b''.join(datas)):08x}".encode())


# What the script and styles become while we're off. A loader running from the old pak (until the
# next start) only reloads files it can read, so a deleted script would keep the running copy
# going; an empty one makes the loader dispose it. Through the link, unregistering tears it down.
OFF_FILES = {
    "bl4respec/respec_main.js": b"// Easy Respec is off.\n",
    "bl4respec/respec.css": b"/* Easy Respec is off. */\n",
}


def remove_loose_files(game_dir: Path) -> None:
    """Stops what the page is running (see OFF_FILES) and removes the right click signal."""
    for path, data in OFF_FILES.items():
        target = game_dir / UI_PREFIX / path
        if target.exists():
            _write_if_changed(target, data)
    if (stamp := game_dir / UI_PREFIX / STAMP_PATH).exists():
        _write_if_changed(stamp, b"off")
    (game_dir / UI_PREFIX / SIGNAL_PATH).unlink(missing_ok=True)


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
    applies whatever is staged next to `target` at that point. Returns whether the helper runs.

    The game reads a pak's index once at startup and keeps reading file data at those offsets, but
    it doesn't keep the file open the whole time (a replace failed with "Access is denied" at
    startup and went through a few minutes later, verified). A pak changed under a running game
    gets read at the old offsets: garbage on screen. So existing paks only change after exit, and
    the helper reads the staged files only then, so later changes of mind this session still count.
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
        # The game runs in a job that doesn't allow breaking away (verified), so no
        # CREATE_BREAKAWAY_FROM_JOB; the helper still outlives a normal game exit
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        return False
    _helper_started = True
    return True


def _retire(path: Path) -> str:
    """Removes the old Skills page pak (after the game exits if it's there). Returns a note, "" if none."""
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return ""
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f" The old Skills page pak ({path.name}) goes when the game exits."
    return f" Couldn't remove the old {path.name} while the game is running."


def check_page(game_dir: Path) -> bool:
    """Whether the game's own Skills page (its paks, not ~mods) is the copy vanilla.json pins."""
    global page_current
    expected = json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))["sha1"]
    paks_dir = game_dir / "OakGame" / "Content" / "Paks"
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + PAGE)
    page_current = game_copy is not None and game_copy.sha1 == expected
    return page_current


def sync(game_dir: Path, enabled: bool) -> str:
    """
    Publishes (or stops) our loose page files to match `enabled` and the installed game, and
    removes the old pak. The caller registers respec.js with the link while `page_current`.
    Returns a status line.
    """
    note = _retire(pak_path(game_dir))
    if not enabled:
        remove_loose_files(game_dir)
        return "Easy Respec is off." + note
    if not check_page(game_dir):
        remove_loose_files(game_dir)
        return "Easy Respec paused: this game version changed the Skills menu, so the mod needs an update." + note
    write_loose_files(game_dir)
    return "Easy Respec is on: right-click a skill to take a point out." + note
