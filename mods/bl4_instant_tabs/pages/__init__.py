"""
Checks that the game's inventory and map pages are still the ones our fade skip was made for, and
removes our old copies of them.

The fade skip (../ui/fade.js, loaded by ../ui/loader.js) runs in the ECHO menu's page; all ECHO
menu pages share one JS context there. Since 0.2.0 the BL4 UI Link (../uilink, shared by our mods)
runs loader.js in that page, without replacing any game page. Until 0.1.x it got there through our
own copies of menu_equip.html and menu_bigmap.html in a pak (pakchunk0-Windows_4246_P.pak), which
clashed with any other mod replacing those pages. sync() now only removes that pak: the game reads
a pak's index once at startup and keeps reading file data at those offsets, so an existing pak is
only removed after the game exits (a `.remove` marker and a hidden helper; with no game running,
as in the setup app, at once).

`page_current` says whether the game still ships exactly the copies of both pages the script was
checked with (vanilla.json, tools/gen_instant_tabs_pages.py). After a game update that changed
either, the fade skip steps aside until the mod is updated (health shows "Needs update"). The tab
delay and the other code patches don't depend on any of this.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread, and the
setup app imports it (installer/core.py PAGE_MODULES): keep it plain Python 3.11.
"""

from __future__ import annotations

import json
import subprocess
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
# The old pages pak (until 0.1.x), removed after exit
PAK_NAME = "pakchunk0-Windows_4246_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"

# Whether the game's own pages are still the ones fade.js was checked with: None until sync()
# looked (read by the health probes, shared/health)
page_current: bool | None = None

# Whether this game session already started the helper that applies staged pak changes
_helper_started = False


def _vanilla() -> dict[str, str]:
    """Each page the fade skip was checked with -> the SHA1 of the game's copy."""
    data = json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))
    return {page: info["sha1"] for page, info in data["pages"].items()}


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


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


def _retire(path: Path) -> str:
    """Removes the old pages pak (after the game exits if it's there). Returns a note, "" if none."""
    replacement, removal = _staged(path)
    replacement.unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    if not path.exists():
        removal.unlink(missing_ok=True)
        return ""
    removal.write_bytes(b"")
    if _apply_after_exit(path):
        return f" The old pages pak ({path.name}) goes when the game exits."
    return f" Couldn't remove the old {path.name} while the game is running."


def check_pages(game_dir: Path) -> str | None:
    """The first page of ours the game changed (its paks, not ~mods), or None if both are current."""
    global page_current
    game_paks = sorted((game_dir / "OakGame" / "Content" / "Paks").glob("*.pak"))
    changed = None
    for page, sha1 in _vanilla().items():
        game_copy = pak.winning_entry(game_paks, UI_PREFIX + page)
        if game_copy is None or game_copy.sha1 != sha1:
            changed = page
            break
    page_current = changed is None
    return changed


def sync(game_dir: Path) -> str:
    """
    Removes the old pak and checks the game's pages; the caller runs the fade skip through the
    link while `page_current`. Returns a status line.
    """
    note = _retire(pak_path(game_dir))
    changed = check_pages(game_dir)
    if changed is not None:
        return (
            f"Page fade skip paused: this game version changed {changed}, so the mod needs an update."
            " Tabs still switch without the delay." + note
        )
    return "Page fade skip is on." + note
