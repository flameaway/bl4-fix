"""
The Skills page with right-click respec.

BL4's menus are HTML pages (Coherent Gameface) stored in the game's paks, and a pak in
`Paks/~mods/` can replace any of them. We ship one replaced page, menu_skills.html, made from the
game's own copy by tools/gen_respec_page.py, which only adds our script and stylesheet.

The scripts and stylesheet are loose files in uiresources/bl4respec/ rather than pak entries: the
UI reads loose files there straight from disk. The page loads respec.js, a small loader that pulls
in respec_main.js and respec.css and reloads them whenever they change, so updating them needs no
restart. Paks are only read at startup, so the page itself (and the first install) applies the next
time the game starts.

To never undo a game update, the pak is only installed while the game's own menu_skills.html is
still exactly the copy ours was made from (vanilla.json). If a patch changes it, or another mod
also replaces the Skills page, the pak is removed and the feature stays off until the mod is
updated.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread.
"""

from __future__ import annotations

import json
import subprocess
import zlib
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
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


def build_pak() -> bytes:
    return pak.build_pak("../../../" + UI_PREFIX, {PAGE: (ASSETS / "menu_skills.html").read_bytes()}, PAK_NAME)


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


# What the script and styles become while we're off. The page (ours until the next start) only
# reloads files it can read, so a deleted script would keep the running copy going; an empty one
# makes the loader dispose it.
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


def _install(path: Path, data: bytes) -> str:
    replacement, removal = _staged(path)
    removal.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)  # staged by older versions
    if not path.exists():
        # Nothing mounted it yet, so it's safe to put in place now
        replacement.unlink(missing_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return "Easy Respec installed: restart the game once to use it."
    if path.read_bytes() == data:
        replacement.unlink(missing_ok=True)
        return "Easy Respec is on: right-click a skill to take a point out."
    replacement.write_bytes(data)
    if _apply_after_exit(path):
        return "Easy Respec updated: the new Skills page loads the next time the game starts."
    return "Easy Respec: couldn't update the Skills page while the game is running."


def _remove(path: Path, reason: str) -> str:
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return reason
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f"{reason} The normal Skills page comes back the next time the game starts."
    return f"{reason} Couldn't remove {path.name} while the game is running."


def sync(game_dir: Path, enabled: bool) -> str:
    """Installs or removes our pak to match `enabled` and the installed game. Returns a status line."""
    ours = pak_path(game_dir)
    if not enabled:
        remove_loose_files(game_dir)
        return _remove(ours, "Easy Respec is off.")

    paks_dir = ours.parent.parent
    expected = json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))["sha1"]
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + PAGE)
    if game_copy is None or game_copy.sha1 != expected:
        return _remove(
            ours,
            "Easy Respec paused: this game version changed the Skills menu, so the mod needs an update.",
        )

    others = [
        other.name
        for other in sorted(ours.parent.glob("*.pak"))
        if other.name.lower() != ours.name.lower()
        and pak.winning_entry([other], UI_PREFIX + PAGE) is not None
    ]
    if others:
        return _remove(ours, f"Easy Respec paused: {', '.join(others)} in Paks/~mods also changes the Skills menu.")

    write_loose_files(game_dir)
    return _install(ours, build_pak())
