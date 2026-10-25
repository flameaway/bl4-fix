"""
BL4 Direct setup & friends app.

With no arguments, opens the GUI. Command line use (from a terminal):

    BL4DirectSetup.exe --status
    BL4DirectSetup.exe --install [--game DIR] [--skip-intro on|off] [--mods all|none|defaults|ID,ID...]
    BL4DirectSetup.exe --add-mod ID[,ID...] | --remove-mod ID[,ID...]
    BL4DirectSetup.exe --list-mods
    BL4DirectSetup.exe --skip-intro on|off
    BL4DirectSetup.exe --uninstall [--remove-sdk]
    BL4DirectSetup.exe --add-friend NAME IP [--port N]
    BL4DirectSetup.exe --remove-friend NAME
    BL4DirectSetup.exe --host-port N | --allow-lan on|off
    BL4DirectSetup.exe --apply-firewall
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

    game = _pick_game(args.game)
    config = core.load_config(game)
    changed_config = False

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
    if args.install or mods is not None or skip_intro is not None:
        # Skip Intro is a mod now: switching it installs or removes it like the others
        core.install(game, skip_intro=skip_intro, mods=mods)
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

    if changed_config:
        config.save(core.friends_file(game))
        print(f"Saved {core.friends_file(game)}")
    if changed_config or args.apply_firewall:
        core.apply_firewall(game)
    actions = ("install", "uninstall", "apply_firewall", "skip_intro", "mods", "add_mod", "remove_mod")
    if args.status or not any(vars(args)[k] for k in actions) and not changed_config:
        st = core.status(game)
        print("\n".join(st.lines()))
        print(f"Friends: {', '.join(f'{f.name} {f.address}:{f.port}' for f in config.friends) or 'none'}")
        return 0 if st.ready else 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="BL4DirectSetup")
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
    parser.add_argument("--skip-intro", choices=["on", "off"], help="logo movies off (on) or back (off)")
    parser.add_argument("--mods", help="the optional mods to have: all, none, defaults or a comma list of ids")
    parser.add_argument("--add-mod", metavar="IDS", help="install these mods (comma list), keep the rest")
    parser.add_argument("--remove-mod", metavar="IDS", help="remove these mods (comma list), keep the rest")
    parser.add_argument("--list-mods", action="store_true", help="every mod this app can install")

    if len(sys.argv) == 1:
        _run_gui()
        return 0

    _attach_console()
    try:
        code = cli(parser.parse_args())
    except Exception as ex:  # noqa: BLE001 - a windowed exe would show a raw traceback dialog
        print(f"Error: {ex!r}")
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
