"""
BL4 Fix setup & friends app.

With no arguments, opens the GUI. Command line use (from a terminal):

    BL4FixSetup.exe --status
    BL4FixSetup.exe --install [--game DIR] [--skip-intro on|off] [--mods all|none|defaults|ID,ID...]
    BL4FixSetup.exe --add-mod ID[,ID...] | --remove-mod ID[,ID...]
    BL4FixSetup.exe --list-mods
    BL4FixSetup.exe --skip-intro on|off
    BL4FixSetup.exe --uninstall [--remove-sdk]
    BL4FixSetup.exe --add-friend NAME IP [--port N]
    BL4FixSetup.exe --remove-friend NAME
    BL4FixSetup.exe --host-port N | --allow-lan on|off
    BL4FixSetup.exe --firewall on|off    (off: no firewall rule; with --install, before the first one)
    BL4FixSetup.exe --remove-firewall    (just take the firewall rule away)

The app runs without admin rights. The firewall rule runs Windows' netsh elevated (its UAC prompt
reads "Network Command Shell"); the VC++ runtime has its own prompt; a game folder this user can't
write to runs an elevated copy of the app: `--log-file` is how that copy hands its output back
(core._run_self_elevated).
    BL4FixSetup.exe --apply-firewall
    BL4FixSetup.exe --prepare-pages    (the installed mods' shim paks + page scripts, see core)

Disk Space (slim.py; never part of --install):
    BL4FixSetup.exe --disk-scan [--keep LANGS]    (what it would free; changes nothing)
    BL4FixSetup.exe --disk-free --keep LANGS|all [--old-patches on|off]

Safe mode (for when a mod breaks the game; never while it runs):
    BL4FixSetup.exe --safe-mode on|off|status    (other modders' mods to sdk_mods/.bl4_safe_mode and back)

Bug reports (the Mod Menu's report.py; works with the game closed or running):
    BL4FixSetup.exe --bug-report [DIR]    (zip into DIR, default sdk_mods/.bug_reports)
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path


def _attach_console() -> None:
    """The exe is built windowed; reconnect stdout to the parent terminal for CLI use."""
    if sys.stdout is not None:
        return
    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stderr = sys.stdout
    else:
        import io  # noqa: PLC0415

        sys.stdout = sys.stderr = io.StringIO()


def _pick_game(arg: str | None) -> Path:
    import core  # noqa: PLC0415

    if arg:
        return Path(arg)
    found = core.find_game_dirs()
    if not found:
        raise SystemExit("Couldn't find Borderlands 4 - pass --game <install folder>.")
    return found[0]


def _mod_ids(text: str) -> set[str]:
    import catalog  # noqa: PLC0415

    match text.strip().lower():
        case "all":
            return set(catalog.BY_ID)
        case "none":
            return set()
        case "defaults":
            return set(catalog.DEFAULTS)
    ids = {part.strip() for part in text.split(",") if part.strip()}
    # Accept "dialog_skipper" for "bl4_dialog_skipper"
    return {i if i in catalog.BY_ID else f"bl4_{i}" for i in ids}


def cli(args: argparse.Namespace) -> int:
    import catalog  # noqa: PLC0415
    import core  # noqa: PLC0415
    import shared  # noqa: PLC0415

    if args.remove_firewall:
        core.remove_firewall_rule()
        return 0
    game = _pick_game(args.game)
    if not core.is_game_dir(game):
        # Never write a friends file (or act on the firewall) for a folder that isn't the game
        raise SystemExit(f"{game} doesn't look like a Borderlands 4 install")
    config = core.load_config(game)
    changed_config = False

    if args.prepare_pages:
        if core.game_running():
            raise SystemExit("Close Borderlands 4 first.")
        core.prepare_pages(game, core.installed_mods(game))
        return 0

    if args.disk_scan or args.disk_free:
        return _disk_space(game, args)

    if args.bug_report is not None:
        path, url = core.bug_report(game, Path(args.bug_report) if args.bug_report else None)
        print(f"Report: {path}\nAttach it to a new issue: {url}")
        return 0

    if args.safe_mode:
        return _safe_mode(game, args.safe_mode)

    if args.list_mods:
        installed = core.installed_mods(game)
        for info in catalog.MODS:
            mark = "x" if info.id in installed else " "
            print(f"[{mark}] {info.id:24} {info.name}{' (always)' if info.required else ''}: {info.blurb}")
        return 0

    mods = _mod_ids(args.mods) if args.mods else None
    if args.add_mod or args.remove_mod:
        mods = set(core.installed_mods(game) if mods is None else mods)
        mods |= _mod_ids(args.add_mod or "")
        mods -= _mod_ids(args.remove_mod or "")
    skip_intro = None if args.skip_intro is None else args.skip_intro == "on"
    installing = bool(args.install or mods is not None or skip_intro is not None)
    if installing:
        use_firewall = None if args.firewall is None else args.firewall == "on"
        # Skip Intro is a mod now: switching it installs or removes it like the others
        core.install(game, skip_intro=skip_intro, mods=mods, use_firewall=use_firewall)
        config = core.load_config(game)  # install may have saved the firewall choice
    if args.uninstall:
        core.uninstall(game, remove_sdk=args.remove_sdk)
    if args.add_friend:
        name, address = args.add_friend
        if (normalized := shared.normalize_ipv4(address)) is None:
            raise SystemExit(f"'{address}' isn't an IPv4 address")
        address = normalized
        config.friends = [f for f in config.friends if f.name.lower() != name.lower()]
        config.friends.append(shared.Friend(name, address, args.port or shared.DEFAULT_PORT))
        changed_config = True
    if args.remove_friend:
        config.friends = [f for f in config.friends if f.name.lower() != args.remove_friend.lower()]
        changed_config = True
    if args.host_port:
        config.host_port = args.host_port
        changed_config = True
    if args.allow_lan:
        config.allow_lan = args.allow_lan == "on"
        changed_config = True
    if args.firewall and not installing:
        config.firewall = args.firewall == "on"
        changed_config = True

    if changed_config:
        config.save(core.friends_file(game))
        print(f"Saved {core.friends_file(game)}")
    if changed_config or args.apply_firewall:
        core.apply_firewall(game)
    actions = ("install", "uninstall", "apply_firewall", "firewall", "remove_firewall", "skip_intro", "mods", "add_mod", "remove_mod")
    if args.status or not any(vars(args)[k] for k in actions) and not changed_config:
        st = core.status(game)
        print("\n".join(st.lines()))
        print(f"Friends: {', '.join(f'{f.name} {f.address}:{f.port}' for f in config.friends) or 'none'}")
        return 0 if st.ready else 1
    return 0


def _safe_mode(game: Path, what: str) -> int:
    """--safe-mode on|off|status: other modders' mods set aside in sdk_mods/.bl4_safe_mode (core)."""
    import core  # noqa: PLC0415

    if what != "status":
        result = core.set_safe_mode(game, what == "on")
        for name, reason in result["skipped"]:
            print(f"  skipped {name}: {reason}")
    st = core.safe_mode_status(game)
    if st["on"]:
        print(f"Safe mode: on, {len(st['aside'])} mod{'' if len(st['aside']) == 1 else 's'} set aside"
              + (f" ({', '.join(st['aside'])})" if st["aside"] else ""))
        if st["others"]:
            print(f"  added since (loads; turn it on again to set aside): {', '.join(st['others'])}")
    else:
        print(f"Safe mode: off, {len(st['others'])} mod{'' if len(st['others']) == 1 else 's'} by other modders in sdk_mods"
              + (f" ({', '.join(st['others'])})" if st["others"] else ""))
    if st["left"]:
        print(f"  still in {core.SAFE_MODE_REL.as_posix()} (sdk_mods has the same name): {', '.join(st['left'])}")
    return 0


def _disk_space(game: Path, args: argparse.Namespace) -> int:
    """--disk-scan / --disk-free: unused voice languages and old patch data (slim.py)."""
    import core  # noqa: PLC0415
    import slim  # noqa: PLC0415

    if args.disk_free:
        if not args.keep:
            raise SystemExit("--disk-free needs --keep: the voice languages to keep (a comma list), or all")
        if core.game_running():
            raise SystemExit("Close Borderlands 4 first.")
        if slim.steam_busy(game):
            raise SystemExit("Steam is updating or checking the game. Let it finish first.")
    scanner = slim.Scanner(game)
    installed = sorted(scanner.voices)
    if not args.keep:
        keep = scanner.default_keep()
    elif args.keep.strip().lower() == "all":
        keep = installed
    else:
        keep = list(dict.fromkeys(slim.match_language(k, installed) for k in args.keep.split(",") if k.strip()))
    plan = scanner.plan(keep, args.old_patches != "off")
    print(f"Game on disk now: {slim.gb(scanner.on_disk)}")
    for name in installed:
        print(f"  {name:21} {slim.gb(scanner.voice_sizes[name]):>10}  {'keep' if name in plan.keep else 'remove'}")
    print(f"Voice languages to remove: {slim.gb(plan.voice_bytes)} ({len(plan.remove)} files)")
    if args.old_patches == "off":
        print("Old patch copies: left alone")
    elif not scanner.unit:
        print("Old patch copies: this drive can't free part of a file (needs NTFS and a few MB free)")
    else:
        print(f"Old patch copies: {slim.gb(plan.old_bytes)} in {len(plan.todo)} files")
    if not args.disk_free or not plan.total:
        return 0
    if not scanner.writable and not core.is_admin() and not core.NO_ELEVATE:
        argv = ["--disk-free", "--keep", ",".join(plan.keep), "--old-patches", "on" if plan.old else "off", "--game", str(game)]
        core._run_self_elevated(argv)  # noqa: SLF001
        return 0
    freed = scanner.apply(plan)
    print(f"Done. Freed {slim.gb(freed)}. Undo: Steam > Properties > Installed Files > Verify integrity.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="BL4FixSetup")
    parser.add_argument("--game", help="Borderlands 4 install folder (auto-detected otherwise)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--remove-sdk", action="store_true")
    parser.add_argument("--add-friend", nargs=2, metavar=("NAME", "IP"))
    parser.add_argument("--port", type=int, help="with --add-friend: the friend's host port")
    parser.add_argument("--remove-friend", metavar="NAME")
    parser.add_argument("--host-port", type=int)
    parser.add_argument("--allow-lan", choices=["on", "off"])
    parser.add_argument("--apply-firewall", action="store_true")
    parser.add_argument("--firewall", choices=["on", "off"], help="the hosting firewall rule (off: never touch the firewall)")
    parser.add_argument("--remove-firewall", action="store_true", help="remove the hosting firewall rule")
    parser.add_argument("--log-file", help=argparse.SUPPRESS)  # an elevated copy's output, for the app that started it
    parser.add_argument("--skip-intro", choices=["on", "off"], help="logo movies off (on) or back (off)")
    parser.add_argument("--mods", help="the optional mods to have: all, none, defaults or a comma list of ids")
    parser.add_argument("--add-mod", metavar="IDS", help="install these mods (comma list), keep the rest")
    parser.add_argument("--remove-mod", metavar="IDS", help="remove these mods (comma list), keep the rest")
    parser.add_argument("--list-mods", action="store_true", help="every mod this app can install")
    parser.add_argument("--prepare-pages", action="store_true", help="set up the installed mods' game pages now")
    parser.add_argument("--disk-scan", action="store_true", help="what Disk Space would free (changes nothing)")
    parser.add_argument("--disk-free", action="store_true", help="remove unused voice languages / old patch data")
    parser.add_argument("--keep", metavar="LANGS", help="with --disk-*: voice languages to keep (comma list, or all)")
    parser.add_argument("--old-patches", choices=["on", "off"], default="on", help="with --disk-*: free old patch data")
    parser.add_argument("--safe-mode", choices=["on", "off", "status"],
                        help="set other modders' mods aside (on), put them back (off), or show it")
    parser.add_argument("--bug-report", nargs="?", const="", metavar="DIR",
                        help="write a bug report zip (default: sdk_mods/.bug_reports) and print the issue link")

    if len(sys.argv) == 1:
        _run_gui()
        return 0

    _attach_console()
    args = parser.parse_args()
    if args.log_file:
        # An elevated copy started by core._run_self_elevated: output goes where that app reads it
        import core  # noqa: PLC0415

        sys.stdout = sys.stderr = open(args.log_file, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        core.NO_ELEVATE = True
    try:
        code = cli(args)
    except Exception as ex:  # noqa: BLE001 - a windowed exe would show a raw traceback dialog
        print(f"Error: {ex}" if args.log_file else f"Error: {ex!r}")
        code = 2
    except SystemExit as ex:
        if not args.log_file or isinstance(ex.code, int | None):
            raise
        print(f"Error: {ex.code}")
        code = 2
    _save_silent_output(code)
    return code


def _run_gui() -> None:
    """The HTML window (pywebview on WebView2); the plain Tk window where WebView2 is missing."""
    try:
        import webgui  # noqa: PLC0415

        use_web = webgui.webview2_available()
    except ImportError:
        use_web = False
    if use_web:
        webgui.run()
        return
    import gui  # noqa: PLC0415

    gui.run()


def _save_silent_output(code: int) -> None:
    """When run without a console (e.g. elevated from the game), keep the output for debugging."""
    import io  # noqa: PLC0415
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    if not isinstance(sys.stdout, io.StringIO):
        return
    log_dir = Path(os.environ.get("LOCALAPPDATA", ".")) / "BL4Direct"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "last_run.log").write_text(
        f"{time.ctime()}  args={sys.argv[1:]}  exit={code}\n{sys.stdout.getvalue()}",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
