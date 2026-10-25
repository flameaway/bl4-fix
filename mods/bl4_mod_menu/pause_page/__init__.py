"""
The pause menu page with our loader, which adds MODS to the pause menu.

BL4's menus are HTML pages (Coherent Gameface) stored in the game's paks, and a pak in
`Paks/~mods/` can replace any of them. We ship one replaced page, menu_pause.html, made from the
game's own copy by tools/gen_mod_menu_page.py: it only adds a <script> for
uiresources/bl4modmenu/loader.js. The menu itself is in loose files the loader pulls in and
reloads when they change (see ../ui and ../web.py), so updating it needs no restart. Paks are only
read at startup, so the page itself (and the first install) applies the next time the game starts.

To never undo a game update, the pak is only installed while the game's own menu_pause.html is
still exactly the copy ours was made from (vanilla.json). If a patch changes it, or another mod
also replaces the pause menu, the pak is removed and the HTML menu stays off until the mod is
updated; F7 still opens the basic UMG menu.

The game reads a pak's index once at startup and keeps reading file data at those offsets, but
doesn't keep the file open the whole time, so a pak changed under a running game gets read at the
old offsets (garbage on screen). So an existing pak is only ever changed after the game exits.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread.
"""

from __future__ import annotations

import json
import subprocess
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
PAK_NAME = "pakchunk0-Windows_4245_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
PAGE = "menu_pause/menu_pause.html"

# Whether this game session already started the helper that applies staged pak changes
_helper_started = False


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


def build_pak() -> bytes:
    return pak.build_pak("../../../" + UI_PREFIX, {PAGE: (ASSETS / "menu_pause.html").read_bytes()}, PAK_NAME)


def _staged(target: Path) -> tuple[Path, Path]:
    """`<pak>.next` (replace it with this) and `<pak>.remove` (delete it), applied after exit."""
    return target.with_name(target.name + ".next"), target.with_name(target.name + ".remove")


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _apply_after_exit(target: Path) -> bool:
    """
    Starts (once per game session) a hidden helper that waits until no game is running, then
    applies whatever is staged next to `target` at that point, so a later change of mind this
    session still counts. Returns whether the helper runs.
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
        # The game runs in a job that doesn't allow breaking away, but the helper still outlives
        # a normal game exit (verified by the respec mod, which does the same)
        subprocess.Popen(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607
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
    path.with_suffix(".tmp").unlink(missing_ok=True)  # staged by older versions
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Nothing mounted it yet, so it's safe to put in place now
        replacement.unlink(missing_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return "MODS added to the pause menu: it shows up the next time the game starts (F7 works now)."
    if path.read_bytes() == data:
        replacement.unlink(missing_ok=True)
        return "MODS is in the pause menu."
    replacement.write_bytes(data)
    if _apply_after_exit(path):
        return "Pause menu page updated: the new one loads the next time the game starts."
    return "Couldn't update the pause menu page while the game is running."


def _remove(path: Path, reason: str) -> str:
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return reason
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f"{reason} The normal pause menu comes back the next time the game starts."
    return f"{reason} Couldn't remove {path.name} while the game is running."


def installed(game_dir: Path) -> bool:
    return pak_path(game_dir).exists()


def sync(game_dir: Path) -> str:
    """Installs our pak if the installed game still matches it, or removes it. Returns a status line."""
    ours = pak_path(game_dir)
    paks_dir = ours.parent.parent
    expected = json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))["sha1"]
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + PAGE)
    if game_copy is None or game_copy.sha1 != expected:
        return _remove(
            ours,
            "MODS in the pause menu is paused: this game version changed the pause menu, so the mod"
            " needs an update. F7 still opens the basic menu.",
        )

    others = [
        other.name
        for other in sorted(ours.parent.glob("*.pak"))
        if other.name.lower() != ours.name.lower() and pak.winning_entry([other], UI_PREFIX + PAGE) is not None
    ]
    if others:
        return _remove(
            ours,
            f"MODS in the pause menu is paused: {', '.join(others)} in Paks/~mods also changes the pause"
            " menu. F7 still opens the basic menu.",
        )

    return _install(ours, build_pak())
