"""
Detailed item cards: skill names, ranks, numbers and descriptions on class mods, names on
secondary stats, and properly rendered banner/flavour text.

BL4's menus are HTML pages (Coherent Gameface). Everything we draw comes from item_cards.js and
item_cards.css, loose files in uiresources/bl4direct/. Since 0.2.0 the BL4 UI Link (../uilink,
shared by our mods) runs the script in every game page; the script adds to the game's own card at
runtime what our copy of item_card.html used to add (a box for the skill list, the stat names,
markup rendering). That copy lived in a pak (pakchunk0-Windows_4242_P.pak), which clashed with any
other mod replacing the item card; it's removed now.

Paks are only read at startup: the game keeps each pak's index from then on and reads file data
at those offsets, so a pak changed or deleted while the game runs can show garbage (verified).
So the old pak is never touched while the game runs: a `.remove` marker next to it and a small
hidden helper delete it once the game has closed. Until then (that one session) the old page
still loads the script too; it runs as one copy either way.

To never draw over a game update, the script only runs while the game's own item_card.html is
still exactly the copy it was written against (vanilla.json, `page_current`). If a patch changes
it, the feature stays off ("Needs update") until the mod is updated.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread. The setup
app imports it too (installer/core.py PAGE_MODULES), so keep it plain Python 3.11.
"""

from __future__ import annotations

import json
import os
import subprocess
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
# True when running from a source folder (a dev junction) rather than a packed .sdkmod, whose
# files can't change while the game runs
FROM_SOURCE = isinstance(ASSETS, Path)
# The old page pak (until 0.1.x), removed after the game exits
PAK_NAME = "pakchunk0-Windows_4242_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
CARD_PAGE = "item_card/item_card.html"
# Our own files, kept as loose files: (path in the game's uiresources folder, asset name). They
# must not also be in a pak, since a pak copy would win over the loose one. The link runs the
# script (SCRIPT_URL).
LOOSE_FILES = [("bl4direct/item_cards.css", "item_cards.css"), ("bl4direct/item_cards.js", "item_cards.js")]
# Generated game data the cards read, published only while the game data it came from is unchanged:
# (path, asset, key of its source fingerprints in vanilla.json)
LOOSE_DATA = [("bl4direct/drops.json", "drops.json", "drop_data")]
# The player's card options (size, fit to screen), which the script polls
SETTINGS_PATH = "bl4direct/settings.json"
SCRIPT_URL = "coui://uiresources/bl4direct/item_cards.js"

_helper_started = False
_data_ok: dict[str, bool] = {}
# Whether the game's own item card is the one the script was written against: None until sync()
# checked (read by the shared health package's probe, through the status line)
page_current: bool | None = None


def _vanilla() -> dict:
    return json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))


def pak_path(game_dir: Path) -> Path:
    return game_dir / "OakGame" / "Content" / "Paks" / "~mods" / PAK_NAME


def publish_files(game_dir: Path, enabled: bool = True) -> None:
    """Writes (or removes) our loose script, stylesheet and data; only touches files that differ."""
    files = [(path, asset, True) for path, asset in LOOSE_FILES]
    files += [(path, asset, data_current(game_dir, key)) for path, asset, key in LOOSE_DATA]
    for path, asset, wanted in files:
        target = game_dir / UI_PREFIX / path
        if not (enabled and wanted):
            target.unlink(missing_ok=True)
            continue
        data = (ASSETS / asset).read_bytes()
        try:
            if target.read_bytes() == data:
                continue
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)


def dev_stamp(game_dir: Path) -> tuple:
    """
    Dev (FROM_SOURCE): size and mtime of every published file's source and target. The once-a-second
    republish only runs publish_files when this changed: it read ~200 KB and compared it every second,
    ~2.7 ms on the game thread (2026-10-29); this is two directory listings (~0.4 ms).
    """
    files = []
    for path, asset in [*LOOSE_FILES, *((p, a) for p, a, _ in LOOSE_DATA)]:
        files += [Path(str(ASSETS / asset)), game_dir / UI_PREFIX / path]
    return _files_stamp(files)


def _files_stamp(files: list[Path]) -> tuple:
    """(mtime, size) of each file, None if missing: one directory listing per folder (a stat is
    ~0.16 ms here, a listing ~0.2 ms; measured in game 2026-10-29)."""
    folders: dict[Path, dict[str, tuple[int, int]]] = {}
    for folder in {file.parent for file in files}:
        try:
            with os.scandir(folder) as entries:
                folders[folder] = {e.name: (e.stat().st_mtime_ns, e.stat().st_size) for e in entries}
        except OSError:
            folders[folder] = {}
    return tuple(folders[file.parent].get(file.name) for file in files)


def write_settings(game_dir: Path, settings: dict | None) -> None:
    """Hands the card options to the script; None removes them (the script then uses defaults)."""
    target = game_dir / UI_PREFIX / SETTINGS_PATH
    if settings is None:
        target.unlink(missing_ok=True)
        return
    # "dev": the script re-reads its own files for hot reload only from a source folder (a packed
    # .sdkmod can't change while the game runs; that was ~130 KB read every 2 s per page)
    text = json.dumps({**settings, "dev": FROM_SOURCE}, sort_keys=True)
    try:
        if target.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def _staged(target: Path) -> tuple[Path, Path]:
    return target.with_name(target.name + ".next"), target.with_name(target.name + ".remove")


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def apply_after_exit(target: Path) -> None:
    """Starts (once per game session) a hidden helper that applies staged changes after we exit."""
    global _helper_started
    if _helper_started:
        return
    replacement, removal = _staged(target)
    apply = (
        f"if (Test-Path -LiteralPath {_ps_quote(removal)}) {{ "
        f"Remove-Item -LiteralPath {_ps_quote(target)} -Force -ErrorAction Stop; "
        f"Remove-Item -LiteralPath {_ps_quote(removal)}, {_ps_quote(replacement)} -Force -ErrorAction SilentlyContinue }} "
        f"elseif (Test-Path -LiteralPath {_ps_quote(replacement)}) {{ "
        f"Move-Item -LiteralPath {_ps_quote(replacement)} -Destination {_ps_quote(target)} -Force -ErrorAction Stop }}"
    )
    # Wait for every copy of the game, not just this process: dev tools stage changes too
    script = (
        "while (Get-Process -Name Borderlands4 -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }; "
        f"for ($i = 0; $i -lt 30; $i++) {{ try {{ {apply}; break }} catch {{ Start-Sleep 1 }} }}"
    )
    # The game runs in a job that doesn't allow breaking away, but the helper still outlives a
    # normal game exit (verified by the respec mod, which does the same)
    subprocess.Popen(  # noqa: S603
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    _helper_started = True


def _retire(target: Path) -> str:
    """Removes the old page pak (after the game exits if it's there). Returns a note for the status line."""
    replacement, removal = _staged(target)
    replacement.unlink(missing_ok=True)
    target.with_name(target.name + ".tmp").unlink(missing_ok=True)
    if not target.exists():
        removal.unlink(missing_ok=True)
        return ""
    removal.write_bytes(b"")
    apply_after_exit(target)
    return f" The old item card page ({target.name}) goes when the game exits."


def sync(game_dir: Path, enabled: bool) -> str:
    """
    Publishes (or removes) our loose files, checks the game's item card and retires the old pak.
    Returns a status line; `page_current` says whether the script may run.
    """
    global page_current
    retired = _retire(pak_path(game_dir))
    if enabled:
        check_data(game_dir)
    publish_files(game_dir, enabled)
    if not enabled:
        page_current = None
        return "Detailed item cards are off." + retired

    # The game's own copy (Paks/*.pak, not ~mods): another mod replacing the item card is fine,
    # the script draws into whatever card the page has
    paks_dir = game_dir / "OakGame" / "Content" / "Paks"
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + CARD_PAGE)
    page_current = game_copy is not None and game_copy.sha1 == _vanilla()["sha1"]
    if not page_current:
        return (
            "Detailed item cards paused: this game version changed the item card, so they need a"
            " mod update." + retired
        )
    return "Detailed item cards are on." + retired


def check_data(game_dir: Path) -> None:
    """
    Works out data_current for every kind of generated data. That reads all the game's paks (a
    fraction of a second), so it's done when the mod is enabled rather than in the game's tick.
    """
    for key, value in _vanilla().items():
        if isinstance(value, dict):
            data_current(game_dir, key)


def data_current(game_dir: Path, key: str, check: bool = True) -> bool:
    """
    Whether the game data a generated file came from is unchanged (vanilla.json[key] lists the
    source files and their SHA1s). Checked once per session; with `check` False, a kind not
    checked yet counts as changed.
    """
    if key not in _data_ok:
        if not check:
            return False
        paks = sorted((game_dir / "OakGame" / "Content" / "Paks").glob("*.pak"))
        prints = _vanilla().get(key) or {}
        _data_ok[key] = bool(prints) and all(
            (entry := pak.winning_entry(paks, path)) is not None and entry.sha1 == sha1
            for path, sha1 in prints.items()
        )
    return _data_ok[key]


def skill_data_current(game_dir: Path, check: bool = True) -> bool:
    return data_current(game_dir, "skill_data", check)
