"""
Removes BL4 Mod Menu's old title screen page pak (pakchunk0-Windows_4247_P.pak), retired in 0.2.0.

Until 0.1.x the Mod Menu shipped the game's menu_main/menu_main.html with one extra <script> for
uiresources/bl4modmenu/main_loader.js in its own pak. The BL4 UI Link (../uilink, shared by our
mods) loads it now (../ui/link.js) without replacing any game page, so that pak only clashed with
other mods that replace the title screen page.

The game reads a pak's index once at startup and keeps reading file data at those offsets, so an
existing pak is only ever removed after the game exits: a `.remove` marker next to it and a
hidden helper that deletes it once no game runs. The setup app runs this too (installer/core.py
PAGE_MODULES); with no game running the helper deletes it at once.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PAK_NAME = "pakchunk0-Windows_4247_P.pak"

# Whether this game session already started the helper that applies the staged removal
_helper_started = False


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


def _staged(target: Path) -> tuple[Path, Path]:
    """`<pak>.next` (an old staged replacement) and `<pak>.remove` (delete it), applied after exit."""
    return target.with_name(target.name + ".next"), target.with_name(target.name + ".remove")


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _apply_after_exit(target: Path) -> bool:
    """Starts (once per game session) a hidden helper that deletes `target` once no game runs."""
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
        # Wait for every copy of the game, not our own PID
        "while (Get-Process -Name Borderlands4 -ErrorAction SilentlyContinue) { Start-Sleep 2 }; "
        f"for ($i = 0; $i -lt 30; $i++) {{ try {{ {apply}; break }} catch {{ Start-Sleep 1 }} }}"
    )
    try:
        # The game's job forbids breaking away, but the helper still outlives a normal game exit
        subprocess.Popen(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        return False
    _helper_started = True
    return True


def installed(game_dir: Path) -> bool:
    return pak_path(game_dir).exists()


def sync(game_dir: Path) -> str:
    """Removes the old pak (after the game exits if it's there). Returns a status line ("" = nothing to do)."""
    ours = pak_path(game_dir)
    replacement, removal = _staged(ours)
    replacement.unlink(missing_ok=True)
    ours.with_suffix(".tmp").unlink(missing_ok=True)
    if not ours.exists():
        removal.unlink(missing_ok=True)
        return ""
    removal.write_bytes(b"")
    if _apply_after_exit(ours):
        return f"The old title screen page ({PAK_NAME}) goes when the game exits: the UI Link adds MODS now."
    return f"Couldn't remove the old title screen page ({PAK_NAME}) while the game is running."
