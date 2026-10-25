"""
Detailed item cards: skill names, ranks, numbers and descriptions on class mods, names on
secondary stats, and properly rendered banner/flavour text.

BL4's menus are HTML pages (Coherent Gameface) stored in the game's paks, and a pak in
`Paks/~mods/` can replace any of them. Our pak holds one page, item_card.html, made from the game's
own copy by tools/gen_item_cards.py with a few small additions. Everything else we draw comes from
item_cards.js and item_cards.css, which we keep as loose files in uiresources/bl4direct/: the UI
reads those from disk, and the script reloads itself and the styles when they change, so updating
them needs no restart (only the page in the pak does).

Paks are only read at startup: the game keeps each pak's index from then on and reads file data
at those offsets, but doesn't hold the file open the whole time. A pak replaced while the game runs
can therefore go through and then be read at the old offsets, which shows garbage (verified). So
this never touches an existing pak while the game runs: changes are staged next to it
(`<pak>.next` to replace it, `<pak>.remove` to delete it) and a small hidden helper applies them
once the game has closed. Creating the pak when there's none yet is safe straight away.

To never undo a game update, the pak is only installed while the game's own item_card.html is
still exactly the copy ours was made from (vanilla.json). If a patch changes it, or another mod
also replaces the item card, the pak goes away and the feature stays off until the mod is updated.

Plain Python only (file I/O, no unreal calls), so this is safe to run from any thread.
"""

from __future__ import annotations

import json
import subprocess
from importlib import resources
from pathlib import Path

from .. import pak

ASSETS = resources.files(__name__)
# True when running from a source folder (a dev junction) rather than a packed .sdkmod, whose
# files can't change while the game runs
FROM_SOURCE = isinstance(ASSETS, Path)
# A high patch number so it wins over the game's paks
PAK_NAME = "pakchunk0-Windows_4242_P.pak"
UI_PREFIX = "OakGame/Content/uiresources/"
CARD_PAGE = "item_card/item_card.html"
# Our own files, kept as loose files: (path in the game's uiresources folder, asset name). They
# must not also be in a pak, since a pak copy would win over the loose one.
LOOSE_FILES = [("bl4direct/item_cards.css", "item_cards.css"), ("bl4direct/item_cards.js", "item_cards.js")]
# Generated game data the cards read, published only while the game data it came from is unchanged:
# (path, asset, key of its source fingerprints in vanilla.json)
LOOSE_DATA = [("bl4direct/drops.json", "drops.json", "drop_data")]
# The player's card options (size, fit to screen), which the script polls
SETTINGS_PATH = "bl4direct/settings.json"

_helper_started = False
_data_ok: dict[str, bool] = {}


def _vanilla() -> dict:
    return json.loads((ASSETS / "vanilla.json").read_text(encoding="utf-8"))


def build_pak() -> bytes:
    files = {CARD_PAGE: (ASSETS / "item_card.html").read_bytes()}
    return pak.build_pak("../../../" + UI_PREFIX, files, PAK_NAME)


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


def _install(target: Path, data: bytes) -> str:
    replacement, removal = _staged(target)
    removal.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        replacement.unlink(missing_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return "Detailed item cards installed: restart the game to see them."
    if target.read_bytes() == data:
        replacement.unlink(missing_ok=True)
        return "Detailed item cards are on."
    # Never touch a pak the game may have loaded: swap it after the game closes
    replacement.write_bytes(data)
    apply_after_exit(target)
    return "Detailed item cards updated: restart the game to see the changes."


def _remove(target: Path, reason: str) -> str:
    replacement, removal = _staged(target)
    replacement.unlink(missing_ok=True)
    if not target.exists():
        removal.unlink(missing_ok=True)
        return reason
    removal.write_bytes(b"")
    apply_after_exit(target)
    # The running game keeps what it loaded at startup either way
    return f"{reason} The game's normal cards come back the next time it starts."


def sync(game_dir: Path, enabled: bool) -> str:
    """Installs or removes our pak to match `enabled` and the installed game. Returns a status line."""
    ours = pak_path(game_dir)
    if enabled:
        check_data(game_dir)
    publish_files(game_dir, enabled)
    if not enabled:
        return _remove(ours, "Detailed item cards are off.")

    paks_dir = ours.parent.parent
    game_copy = pak.winning_entry(sorted(paks_dir.glob("*.pak")), UI_PREFIX + CARD_PAGE)
    if game_copy is None or game_copy.sha1 != _vanilla()["sha1"]:
        return _remove(
            ours,
            "Detailed item cards paused: this game version changed the item card, so they need a"
            " mod update.",
        )

    others = [
        other.name
        for other in sorted(ours.parent.glob("*.pak"))
        if other.name.lower() != ours.name.lower()
        and pak.winning_entry([other], UI_PREFIX + CARD_PAGE) is not None
    ]
    if others:
        return _remove(
            ours,
            f"Detailed item cards paused: {', '.join(others)} in Paks/~mods also changes item cards.",
        )

    return _install(ours, build_pak())


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
