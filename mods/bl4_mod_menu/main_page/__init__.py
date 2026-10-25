"""
The title screen's main menu page with our loader, which adds MODS under Options (with a "!" when
mod updates are waiting) and runs the same MODS screen as the pause menu (../ui, ../web.py).

Same scheme as pause_page: tools/gen_mod_menu_page.py takes the game's menu_main/menu_main.html
and adds one <script> for uiresources/bl4modmenu/main_loader.js; the pak (its own number, so the
pause menu page is unaffected) is only installed while the game's copy is exactly the one ours was
made from (vanilla.json), and an existing pak is only changed after the game exits. Paks load at
startup, so the entry appears the next time the game starts.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread.
"""

from __future__ import annotations

import json
import subprocess
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
PAK_NAME = "pakchunk0-Windows_4247_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
PAGE = "menu_main/menu_main.html"

# Whether this game session already started the helper that applies staged pak changes
_helper_started = False


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


def build_pak() -> bytes:
    return pak.build_pak("../../../" + UI_PREFIX, {PAGE: (ASSETS / "menu_main.html").read_bytes()}, PAK_NAME)


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
        return "MODS added to the title screen: it shows up the next time the game starts."
    if path.read_bytes() == data:
        replacement.unlink(missing_ok=True)
        return "MODS is on the title screen."
    replacement.write_bytes(data)
    if _apply_after_exit(path):
        return "Title screen page updated: the new one loads the next time the game starts."
    return "Couldn't update the title screen page while the game is running."


def _remove(path: Path, reason: str) -> str:
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return reason
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f"{reason} The normal title screen comes back the next time the game starts."
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
            "MODS on the title screen is paused: this game version changed the main menu, so the mod"
            " needs an update. F7 still opens the menu.",
        )

    others = [
        other.name
        for other in sorted(ours.parent.glob("*.pak"))
        if other.name.lower() != ours.name.lower() and pak.winning_entry([other], UI_PREFIX + PAGE) is not None
    ]
    if others:
        return _remove(
            ours,
            f"MODS on the title screen is paused: {', '.join(others)} in Paks/~mods also changes the main"
            " menu. F7 still opens the menu.",
        )

    return _install(ours, build_pak())
