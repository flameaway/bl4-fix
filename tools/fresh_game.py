"""
Makes Borderlands 4 look like a fresh copy (no mods, no mod settings, default game settings), and
puts everything back afterwards. For testing the first-run experience: the setup app on a clean
game, Kairosdex with no pictures, every mod's defaults.

    python tools/fresh_game.py status
    python tools/fresh_game.py fresh [--keep-game-settings] [--keep-dlss]
    python tools/fresh_game.py restore

Nothing is deleted. `fresh` moves each item into a stash folder next to the game
(steamapps/common/Borderlands 4 (fresh test stash)/) and writes stash.json as it goes, so a crash
halfway still restores. What it moves, when present:

  game folder   sdk_mods/ (the SDK's Python side, every mod, mod settings, friends, bug reports),
                OakGame/Binaries/Win64/dsound.dll and Plugins/ (the SDK),
                OakGame/Content/Paks/~mods/ (the UI Link pak, any other pak mods),
                OakGame/Content/uiresources/bl4*/ (every mod's pages, incl. Kairosdex's boss and
                item pictures in bl4weapondex/), the DLSS-G proxy (version.dll, dlssg_sm86*,
                dlssg_upgrade_*/; --keep-dlss leaves it)
  Documents     My Games/Borderlands 4/Saved/Config (the game's own settings; --keep-game-settings
                leaves it) and Saved/BL4Fix Save Backups. Save games are never touched.
  app data      %LOCALAPPDATA%/Programs/BL4 Fix (the installed setup app), %LOCALAPPDATA%/BL4Direct
                (its log), the Start menu shortcut
  movies        Skip Intro's parked logo movies (*.bk2.skipped) get their names back

`restore` first sweeps whatever the test created in those places (a test install's sdk_mods, new
pictures, the game's regenerated Config...) into the stash's test-leftovers-<time>/ folder, then
puts the originals back and parks the logo movies again if they were parked. Delete the stash
folder by hand once you no longer want those leftovers.

Not touched: the Windows Firewall rule (needs admin; `status` says whether it exists), save games.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))
import core  # noqa: E402  # find_game_dirs, game_running, firewall
import movies  # noqa: E402  # the installer's copy of Skip Intro's movies.py

STASH_NAME = "Borderlands 4 (fresh test stash)"
RECORD = "stash.json"
FORMAT = 1


# region Where things are


def _documents() -> Path:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL, wherever it is
    return Path(buf.value) if buf.value else Path.home() / "Documents"


def _targets(game_dir: Path, keep_game_settings: bool, keep_dlss: bool) -> list[tuple[str, Path]]:
    """(group, path) for every place `fresh` empties. Globs are expanded against what's there now."""
    win64 = game_dir / "OakGame/Binaries/Win64"
    out: list[tuple[str, Path]] = [
        ("mods", game_dir / "sdk_mods"),
        ("sdk", win64 / "dsound.dll"),
        ("sdk", win64 / "Plugins"),
        ("mods", game_dir / "OakGame/Content/Paks/~mods"),
    ]
    ui = game_dir / "OakGame/Content/uiresources"
    if ui.is_dir():
        out += [("mods", p) for p in sorted(ui.iterdir()) if p.name.lower().startswith("bl4")]
    if not keep_dlss:
        out += [("dlss", win64 / "version.dll"), ("dlss", win64 / "dlssg_sm86"), ("dlss", win64 / "dlssg_sm86.ini")]
        out += [("dlss", p) for p in sorted(game_dir.glob("dlssg_upgrade_*"))]
    saved = _documents() / "My Games/Borderlands 4/Saved"
    if not keep_game_settings:
        out.append(("game settings", saved / "Config"))
    out.append(("mods", saved / "BL4Fix Save Backups"))
    if local := os.environ.get("LOCALAPPDATA"):
        out += [("app", Path(local) / "Programs/BL4 Fix"), ("app", Path(local) / "BL4Direct")]
    if (link := core._shortcut_path()) is not None:
        out += [("app", link.parent / name) for name in (link.name, *core.OLD_SHORTCUT_NAMES)]
    return out


def _stash_dir(game_dir: Path) -> Path:
    return game_dir.parent / STASH_NAME


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _load(stash: Path) -> dict | None:
    try:
        return json.loads((stash / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(stash: Path, record: dict) -> None:
    tmp = stash / (RECORD + ".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, stash / RECORD)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


# endregion
# region Commands


def _check_closed() -> None:
    if core.game_running():
        sys.exit("Close Borderlands 4 first.")


def _firewall_note() -> str:
    try:
        rule = core.firewall.rule_summary()
    except Exception:  # noqa: BLE001
        return ""
    if rule is None:
        return "Firewall: no BL4 Direct rule (already like a fresh PC)."
    return "Firewall: the BL4 Direct rule is still there (left alone, it needs admin; the setup app's Uninstall removes it)."


def cmd_fresh(game_dir: Path, args: argparse.Namespace) -> None:
    _check_closed()
    stash = _stash_dir(game_dir)
    if _load(stash) is not None:
        sys.exit(f"Already fresh: your mods are in {stash}. Run `restore` first.")
    stash.mkdir(exist_ok=True)
    items_dir = stash / "items"
    items_dir.mkdir(exist_ok=True)
    record: dict = {"format": FORMAT, "time": int(time.time()), "game": str(game_dir), "moved": [], "movies_parked": []}
    _save(stash, record)

    # Logo movies first: get their names back (Skip Intro parked them)
    record["movies_parked"] = [n for n in movies.LOGOS if _exists(movies._paths(game_dir, n)[1])]
    restored = movies.restore(game_dir)
    _save(stash, record)
    if restored:
        print(f"  movies         put the logo movies back ({', '.join(restored)})")

    failed = []
    for index, (group, path) in enumerate(_targets(game_dir, args.keep_game_settings, args.keep_dlss)):
        if not _exists(path):
            continue
        dest = items_dir / f"{index:02d}-{path.name}"
        size = _size(path)
        try:
            shutil.move(str(path), str(dest))
        except OSError as ex:
            failed.append(f"{path}: {ex}")
            continue
        record["moved"].append({"group": group, "from": str(path), "stash": dest.name})
        _save(stash, record)
        print(f"  {group:<14} {path}  ({_mb(size)})")

    print(f"\nMoved {len(record['moved'])} items into {stash}")
    if failed:
        print("Couldn't move (something has them open? close the setup app too):")
        for line in failed:
            print("  " + line)
    if note := _firewall_note():
        print(note)
    print("The game is now a fresh copy. `python tools/fresh_game.py restore` puts everything back.")


def cmd_restore(game_dir: Path, _args: argparse.Namespace) -> None:
    _check_closed()
    stash = _stash_dir(game_dir)
    record = _load(stash)
    if record is None:
        sys.exit(f"Nothing to restore: no {stash / RECORD}.")
    items_dir = stash / "items"

    # 1. Sweep what the test made in the same places, so the originals go back onto a clean slate
    leftovers = stash / f"test-leftovers-{time.strftime('%Y%m%d-%H%M%S')}"
    swept = 0
    keep_settings = not any(m["group"] == "game settings" for m in record["moved"])
    keep_dlss = not any(m["group"] == "dlss" for m in record["moved"])
    for index, (_group, path) in enumerate(_targets(game_dir, keep_settings, keep_dlss)):
        if _exists(path):
            leftovers.mkdir(exist_ok=True)
            try:
                shutil.move(str(path), str(leftovers / f"{index:02d}-{path.name}"))
            except OSError as ex:
                sys.exit(f"Couldn't clear {path} ({ex}). Close whatever has it open and run restore again.")
            swept += 1
    if swept:
        print(f"Moved {swept} things the test made into {leftovers}")

    # 2. Originals back
    failed, back = [], []
    for item in record["moved"]:
        src, dest = items_dir / item["stash"], Path(item["from"])
        if not _exists(src):
            failed.append(f"{dest}: missing from the stash")
            continue
        if _exists(dest):
            failed.append(f"{dest}: something is there again (left the stashed copy in {src})")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as ex:
            failed.append(f"{dest}: {ex}")
            continue
        back.append(item)
        print(f"  {item['group']:<14} {dest}")

    # 3. Logo movies parked again if they were
    if record.get("movies_parked"):
        movies.skip(game_dir)
        print("  movies         parked the logo movies again (Skip Intro)")
    else:
        movies.restore(game_dir)

    if failed:
        record["moved"] = [m for m in record["moved"] if m not in back]
        _save(stash, record)
        print("\nNot restored (the stash and its record are kept; fix these and run restore again):")
        for line in failed:
            print("  " + line)
        sys.exit(1)

    (stash / RECORD).unlink()
    with_leftovers = any(p.name.startswith("test-leftovers-") for p in stash.iterdir())
    shutil.rmtree(items_dir, ignore_errors=True)
    if not with_leftovers:
        shutil.rmtree(stash, ignore_errors=True)
    print(f"\nRestored {len(back)} items: your mods and settings are back.")
    if with_leftovers:
        print(f"The test's own files are in {stash}: delete that folder when you don't need them.")


def cmd_status(game_dir: Path, _args: argparse.Namespace) -> None:
    stash = _stash_dir(game_dir)
    record = _load(stash)
    print(f"Game: {game_dir}")
    if record is not None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("time", 0)))
        print(f"FRESH since {when}: {len(record['moved'])} items in {stash}")
        for item in record["moved"]:
            print(f"  {item['group']:<14} {item['from']}")
    else:
        print("Modded (not fresh). `fresh` would move:")
    moved = (record or {}).get("moved", [])
    keep_settings = record is not None and not any(m["group"] == "game settings" for m in moved)
    keep_dlss = record is not None and not any(m["group"] == "dlss" for m in moved)
    present = [(g, p) for g, p in _targets(game_dir, keep_settings, keep_dlss) if _exists(p)]
    if record is not None and present:
        print("Made since going fresh (restore sweeps these into the stash):")
    for group, path in present:
        print(f"  {group:<14} {path}")
    parked = [n for n in movies.LOGOS if _exists(movies._paths(game_dir, n)[1])]
    if parked:
        print(f"  movies         logo movies parked by Skip Intro: {', '.join(parked)}")
    if note := _firewall_note():
        print(note)


# endregion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--game", type=Path, help="the Borderlands 4 folder (found automatically)")
    sub = parser.add_subparsers(dest="command", required=True)
    fresh = sub.add_parser("fresh", help="move every mod and setting aside")
    fresh.add_argument("--keep-game-settings", action="store_true", help="leave the game's own Saved/Config")
    fresh.add_argument("--keep-dlss", action="store_true", help="leave the DLSS-G proxy (version.dll, dlssg_sm86)")
    sub.add_parser("restore", help="put everything back")
    sub.add_parser("status", help="show what's where")
    args = parser.parse_args()

    if args.game is None:
        found = core.find_game_dirs()
        if not found:
            sys.exit("Couldn't find Borderlands 4: pass --game <folder>.")
        args.game = found[0]
    if not core.is_game_dir(args.game):
        sys.exit(f"Not a Borderlands 4 folder: {args.game}")
    {"fresh": cmd_fresh, "restore": cmd_restore, "status": cmd_status}[args.command](args.game, args)


if __name__ == "__main__":
    main()
