"""
Direct IP co-op for Borderlands 4.

BL4 normally routes co-op through Gearbox's SHiFT networking (ShiftNetDriver). The game also ships
Unreal's SteamNetDriver, which talks plain UDP whenever it's given an IP address instead of a Steam
ID, so we swap the game net driver to that and use the stock `open` travel commands:

- host: reload the current map as a listen server (`?listen?bIsLanMatch?port=N`)
- join: `open <ip>:<port>`

The swap only affects net drivers created afterwards, and is undone with `bl4direct off` (or by
disabling the mod), which puts normal SHiFT online play back.

Friends and the host port live in a JSON file shared with the setup app. The firewall rule that
limits who can reach a hosted game is made by the setup app, and updated from here (UAC) when the
friends change.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import socket
import time
from typing import Any

import unrealsdk
from mods_base import (
    ENGINE,
    SETTINGS_DIR,
    ButtonOption,
    DropdownOption,
    KeybindOption,
    build_mod,
    command,
    get_pc,
    hook,
)
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

# coop loads here too, so a host running only BL4 Direct still answers friends
from . import arrival, buddies, coop, frametick, health, page, smooth, ui  # noqa: F401
from .shared import (
    DEFAULT_PORT,
    FRIENDS_FILE_NAME,
    MOD_VERSION,
    Config,
    Friend,
    NETSH,
    compat_code,
    find_rules,
    is_ipv4,
    netsh_command,
    normalize_ipv4,
)

__version__ = MOD_VERSION
__version_info__ = tuple(int(x) for x in MOD_VERSION.split("."))

GAME_NET_DRIVER = "GameNetDriver"
DIRECT_DRIVER = "/Script/OnlineSubsystemSteam.SteamNetDriver"
FRIENDS_FILE = SETTINGS_DIR / FRIENDS_FILE_NAME
GAME_DIR = SETTINGS_DIR.parent.parent
NO_FRIENDS = "(no friends added - use the BL4 Direct app)"

# The driver class the game had configured before we swapped it, or None if not swapped
_original_driver: str | None = None


def log(msg: str) -> None:
    logging.info(f"[BL4 Direct] {msg}")


def log_error(msg: str) -> None:
    logging.error(f"[BL4 Direct] {msg}")


def _game_driver_def() -> Any:
    for definition in ENGINE.NetDriverDefinitions:
        if definition.DefName == GAME_NET_DRIVER:
            return definition
    raise RuntimeError(f"no {GAME_NET_DRIVER} net driver definition found")


def enable_direct() -> None:
    """Makes newly created game net drivers use plain UDP sockets."""
    global _original_driver
    definition = _game_driver_def()
    if _original_driver is None:
        _original_driver = str(definition.DriverClassName)
    definition.DriverClassName = DIRECT_DRIVER


def restore_driver_setting() -> None:
    """
    Puts the game's own (SHiFT) driver back for net drivers created from now on.

    An already-running server keeps the driver it has, so this is safe to call while hosting: it
    only means a later hard map change (e.g. a solo fast travel) goes through the game's normal
    path, which our driver made hang (verified). The watchdog then re-hosts.
    """
    global _original_driver
    if _original_driver is not None:
        _game_driver_def().DriverClassName = _original_driver
        _original_driver = None


def disable_direct() -> None:
    """Back to normal SHiFT online play: forget any hosting intent and restore the driver."""
    global _hosting_port
    _hosting_port = None
    restore_driver_setting()


def direct_active() -> bool:
    """True while hosting (or meaning to) or connected through our driver."""
    return _original_driver is not None or _hosting_port is not None


def console(cmd: str) -> None:
    pc = get_pc()
    unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject.ExecuteConsoleCommand(pc, cmd, pc)


def _is_vpn_ip(ip: str) -> bool:
    first, second = (int(x) for x in ip.split(".")[:2])
    # Tailscale/ZeroTier style CGNAT range (100.64.0.0/10), and Hamachi's 25.0.0.0/8
    return (first == 100 and 64 <= second < 128) or first == 25


def local_ips() -> list[str]:
    """The LAN address of the default route, plus any VPN addresses friends could join through."""
    ips: list[str] = []
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        # Connecting a UDP socket sends nothing, it just picks the outgoing interface
        sock.connect(("8.8.8.8", 80))
        ips.append(sock.getsockname()[0])
    with contextlib.suppress(OSError):
        ips += [
            ip
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]
            if _is_vpn_ip(ip) and ip not in ips
        ]
    return ips


def _is_client(world: Any) -> bool:
    """True while this game is connected to someone else's server."""
    driver = world.NetDriver
    return world.AuthorityGameMode is None or (driver is not None and driver.ServerConnection is not None)


def host(port: int | None = None, *, auto: bool = False) -> None:
    global _auto_rehosts
    config = Config.load(FRIENDS_FILE)
    port = port or config.host_port

    pc = get_pc()
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None or pc is None or pc.Pawn is None:
        log_error("Load into your game first, then host.")
        return
    if _is_client(world):
        log_error("You're in a friend's game - leave it first, then host.")
        return
    if not auto:
        _auto_rehosts = 0

    # Keep the game mode's own travel options (e.g. which station to spawn at), but replace any
    # networking ones with ours
    options = ""
    if (game_mode := world.AuthorityGameMode) is not None:
        options = re.sub(
            r"\?(listen|bIsLanMatch|port=\d+)",
            "",
            str(game_mode.OptionsString),
            flags=re.I,
        )

    global _hosting_port, _last_auto_rehost, _reloading_from, _reload_started
    enable_direct()
    _hosting_port = port
    _last_auto_rehost = _reload_started = time.monotonic()  # the reload mustn't trip the watchdog
    _reloading_from = WeakPointer(world)
    console(f"open {world.Name}?listen?bIsLanMatch?port={port}{options}")

    log(f"Hosting on UDP port {port} - reloading the map...")
    _arrived.clear()
    _prepare_arrival_fix()
    for ip in local_ips():
        log(f"  Same network / VPN friends join:  bl4join {ip}:{port}")
    log("  Internet friends join with your public IP (shown in the BL4 Direct app).")
    if not config.friends:
        log("  No friends are allowlisted, so only your local network can connect.")


def stop_hosting() -> None:
    """Reloads the current map as a normal (SHiFT) game, which drops any connected friends."""
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    disable_direct()
    if world is None:
        return
    options = ""
    if (game_mode := world.AuthorityGameMode) is not None:
        options = re.sub(r"\?(listen|bIsLanMatch|port=\d+)", "", str(game_mode.OptionsString), flags=re.I)
    log("Stopped hosting - reloading the map.")
    console(f"open {world.Name}{options}")


def leave() -> None:
    """Disconnects from a friend's game, back to the main menu."""
    disable_direct()
    console("disconnect")


# region Hosting watchdog
#
# When the host fast-travels with nobody connected, BL4 does a hard map change through Entry_P
# (with friends connected it's a seamless travel that keeps the server, verified). With our Steam
# driver configured, that hard travel either came back with nothing bound to the port or hung on
# loading (both verified, root cause unconfirmed).
#
# So hosting works like this:
# - Host: swap in our driver, reload as a listen server on the port.
# - Once the port is confirmed bound, put the game's SHiFT driver setting back. The running server
#   keeps our driver; any later hard travel uses the game's normal path.
# - After any map load, if the player still wants to host but nothing is bound to the port, host
#   again. Hosting intent lasts until Stop Hosting / Leave / "Don't resume" in the panel.

_hosting_port: int | None = None  # set while the player wants to be hosting
_last_auto_rehost = 0.0
_watch_world: Any = None  # WeakPointer to the world we last checked
# The world host() reloads away from. Until it's gone the port still belongs to the old server, so
# the watchdog must not take "port bound" as hosting being up: it would put the SHiFT driver
# setting back before the new map creates its driver, and the reload comes up on SHiFT (verified)
_reloading_from: Any = None
_reload_started = 0.0
RELOAD_TIMEOUT = 60.0
_watch_since = 0.0
_watch_checked = False
_last_check = 0.0
WATCHDOG_SETTLE = 8.0  # seconds after a map is playable before the first port check
WATCHDOG_INTERVAL = 10.0  # then keep checking this often: the socket can also die mid-game
WATCHDOG_COOLDOWN = 60.0  # never auto-rehost more often than this
MAX_AUTO_REHOSTS = 3  # in a row without the port ever coming up, then give up
_auto_rehosts = 0
# Set once a join got us into a friend's game, so leaving it (to the menu or solo) puts the game's
# own driver setting back. A failed join is caught by the error hooks instead.
_client_seen = False


def port_is_free(port: int) -> bool:
    """True if nothing on this PC is bound to the UDP port (Windows refuses a second bind)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
        return True


def _client_tick() -> None:
    global _client_seen
    if _hosting_port is not None or _original_driver is None:
        _client_seen = False
        return
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return
    driver = world.NetDriver
    if driver is not None and driver.ServerConnection is not None:
        _client_seen = True
    elif _client_seen and world.AuthorityGameMode is not None:
        # Back in a world of our own (main menu, solo): the friend's game is over
        _client_seen = False
        log("Left the direct game - back to normal online play.")
        restore_driver_setting()


def _watchdog_tick() -> None:
    global _watch_world, _watch_since, _watch_checked, _last_check, _last_auto_rehost
    global _reloading_from, _auto_rehosts
    if _hosting_port is None:
        return
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    pc = get_pc(possibly_loading=True)
    if world is None or pc is None or pc.Pawn is None:
        return
    if _is_client(world):
        # In someone else's game (e.g. a Steam invite): our port being free means nothing here
        return
    now = time.monotonic()
    if _reloading_from is not None:
        if _reloading_from() == world and now - _reload_started < RELOAD_TIMEOUT:
            return
        _reloading_from = None
    if _watch_world is None or _watch_world() != world:
        _watch_world = WeakPointer(world)
        _watch_since = now
        _watch_checked = False
        return
    if now - _watch_since < WATCHDOG_SETTLE:
        return
    if _watch_checked and now - _last_check < WATCHDOG_INTERVAL:
        return
    _watch_checked = True
    _last_check = now

    if not port_is_free(_hosting_port):
        # Listening as intended. Hand the driver setting back to the game so a later solo fast
        # travel takes the normal path; this server keeps our driver.
        _auto_rehosts = 0
        restore_driver_setting()
        return
    # The socket can close without any map change too, e.g. when this account signs into SHiFT
    # somewhere else and the game drops its online session (verified)
    if now - _last_auto_rehost < WATCHDOG_COOLDOWN:
        return
    if _auto_rehosts >= MAX_AUTO_REHOSTS:
        log_error(f"Hosting never came up on port {_hosting_port} after {_auto_rehosts} tries - stopped"
                  " trying. Press Host to try again.")
        _auto_rehosts = 0
        disable_direct()
        return
    _auto_rehosts += 1
    _last_auto_rehost = now
    log(f"Hosting stopped listening on port {_hosting_port} - restarting it.")
    host(_hosting_port, auto=True)


@frametick.tick("bl4_direct.watchdog", every=1.0)
def hosting_watchdog() -> None:
    """Once a second (shared frametick)."""
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        _client_tick()
        _watchdog_tick()
    except Exception as ex:  # noqa: BLE001 - never let the watchdog break the game's tick
        log_error(f"hosting watchdog: {ex!r}")
    try:
        smooth.tick()
    except Exception as ex:  # noqa: BLE001
        log_error(f"smoother enemies: {ex!r}")
    try:
        buddies.tick()
    except Exception as ex:  # noqa: BLE001
        log_error(f"teammate panel: {ex!r} - stopped for this session")
        buddies.S.verified = False  # once, not every second


# endregion


def join(address: str, port: int | None = None) -> None:
    global _hosting_port, _reloading_from, _client_seen
    if ":" in address:
        address, port_str = address.rsplit(":", 1)
        port = int(port_str)
    port = port or DEFAULT_PORT
    # Joining leaves our own server for good: the watchdog mustn't host again in the friend's game
    _hosting_port = _reloading_from = None
    _client_seen = False
    enable_direct()
    log(f"Connecting to {address}:{port}...")
    console(f"open {address}:{port}")


# region Commands


@command(description="Host a direct IP co-op session from the map you're currently in.")
def bl4host(args: argparse.Namespace) -> None:
    host(args.port)


bl4host.add_argument("port", nargs="?", type=int, help="UDP port (default: from the BL4 Direct app)")


@command(description="Join a direct IP co-op session, e.g. 'bl4join 203.0.113.5' or 'bl4join Bob'.")
def bl4join(args: argparse.Namespace) -> None:
    target: str = args.target
    for friend in Config.load(FRIENDS_FILE).friends:
        if friend.name.lower() == target.lower():
            join(friend.address, friend.port)
            return
    join(target)


bl4join.add_argument("target", help="friend name, or host IP optionally with :port")


@command(description="'bl4direct off' restores normal SHiFT online play; 'bl4direct' shows status.")
def bl4direct(args: argparse.Namespace) -> None:
    if args.state == "off":
        disable_direct()
    show_status()


bl4direct.add_argument("state", nargs="?", choices=["off", "status"], default="status")


def show_status() -> None:
    config = Config.load(FRIENDS_FILE)
    log(f"Direct mode: {'ON' if direct_active() else 'OFF'}  |  {compat_code(GAME_DIR)}")
    log("  Both players should see the same versions above.")
    log(f"  Host port: {config.host_port}  |  friends: {', '.join(f.name for f in config.friends) or 'none'}")


# endregion
# region Panel API


class PanelApi:
    """What the in-game panel (ui.py) needs from the rest of the mod."""

    @staticmethod
    def current_world() -> Any:
        return ENGINE.GameViewport.World if ENGINE.GameViewport else None

    @staticmethod
    def is_direct_mode() -> bool:
        return direct_active()

    @staticmethod
    def hosting_intent() -> int | None:
        """The port the player wants to host on (resumed after map changes), or None."""
        return _hosting_port

    @staticmethod
    def cancel_hosting_intent() -> None:
        disable_direct()

    @staticmethod
    def compat_line() -> str:
        return compat_code(GAME_DIR)

    @staticmethod
    def host_port() -> int:
        return Config.load(FRIENDS_FILE).host_port

    @staticmethod
    def config() -> Config:
        """The friends file, read once (the page needs several things from it)."""
        return Config.load(FRIENDS_FILE)

    @staticmethod
    def in_gameplay() -> bool:
        pc = get_pc(possibly_loading=True)
        return pc is not None and pc.Pawn is not None

    @staticmethod
    def at_main_menu() -> bool:
        return not PanelApi.in_gameplay()

    @staticmethod
    def friends() -> list[Friend]:
        return Config.load(FRIENDS_FILE).friends

    @staticmethod
    def host() -> str | None:
        """
        Hosts. Never asks about the firewall (2026-10-30, the user's ask): the page offers that
        when the friend list changes, and its Firewall row shows a rule that's out of date.
        Returns an error (none any more; kept for the panel and page).
        """
        host()
        return None

    @staticmethod
    def firewall_up_to_date() -> bool:
        return Config.load(FRIENDS_FILE).firewall_up_to_date()

    @staticmethod
    def stop_hosting() -> None:
        stop_hosting()

    @staticmethod
    def leave() -> None:
        leave()

    @staticmethod
    def join_friend(friend: Friend) -> None:
        join(friend.address, friend.port)

    @staticmethod
    def add_friend(name: str, address: str, port: int = DEFAULT_PORT) -> None:
        config = Config.load(FRIENDS_FILE)
        config.friends = [f for f in config.friends if f.name.lower() != name.lower()]
        config.friends.append(Friend(name, normalize_ipv4(address) or address, port))
        config.save(FRIENDS_FILE)
        refresh_friends()

    @staticmethod
    def remove_friend(name: str) -> None:
        config = Config.load(FRIENDS_FILE)
        config.friends = [f for f in config.friends if f.name != name]
        config.save(FRIENDS_FILE)
        refresh_friends()

    @staticmethod
    def update_firewall() -> str | None:
        """
        Rewrites the firewall rule (netsh, via UAC: "Network Command Shell") to match the friends
        file. Returns an error, if any. The lookup and the prompt run in the background; the panel
        reports it if the player says no. Used to run the setup app's copy in the game folder, which
        is gone (2026-10-28), then a hidden PowerShell (until 2026-10-30, see shared.py).
        """
        config = Config.load(FRIENDS_FILE)
        signature = config.firewall_signature()
        spec = config.firewall_spec()

        def args() -> str | None:
            # On the prompt's worker: finding our rule takes a hidden PowerShell (~0.6 s)
            return netsh_command(spec, [name for name, _ in find_rules()])

        def applied() -> None:
            # Load again: friends may have changed while Windows asked
            latest = Config.load(FRIENDS_FILE)
            latest.firewall_applied = signature
            latest.save(FRIENDS_FILE)
            log(f"Firewall rule updated ({signature}).")

        refused = "Firewall update cancelled - they can't join your game until you allow it."
        failed = "The firewall update failed - try Update again, or re-run BL4 Fix Setup."
        if not ui.run_elevated(str(NETSH), args, refused, applied, failed):
            return "Windows is already asking about the firewall - answer that prompt first."
        return None

    is_ipv4 = staticmethod(is_ipv4)
    refresh = staticmethod(lambda: refresh_friends())


ui.api = PanelApi


@command(description="Opens the BL4 Direct page (same as its open key, F6). 'bl4ui panel': the basic panel.")
def bl4ui(args: argparse.Namespace) -> None:
    # The console is still open on this frame; let it close before the menus take over input
    ui.S.pending = ui.open_panel if args.panel else ui.open_preferred
    ui.S.pending_frames = 5


bl4ui.add_argument("panel", nargs="?", choices=["panel"], help="open the basic panel instead")


# endregion
# region Options


def _friend_names() -> list[str]:
    return [f.name for f in Config.load(FRIENDS_FILE).friends] or [NO_FRIENDS]


def refresh_friends() -> None:
    """Re-reads the friends file, in case the companion app changed it while the game was open."""
    names = _friend_names()
    friend_option.choices[:] = names
    if friend_option.value not in names:
        friend_option.value = names[0]


open_key = KeybindOption(
    "Open key",
    ui.DEFAULT_KEY,
    description=(
        "Opens and closes the BL4 Direct page (host, join and add friends). Works in game, in the"
        " game's own menus and at the title screen. Unbound: open it from the Mods screen."
    ),
)
ui.open_key = lambda: open_key.value

panel_button = ButtonOption(
    "Open the basic panel",
    description="The basic BL4 Direct window: host, join friends, and add friends.",
    on_press=lambda _: ui.open_panel(),
)

host_button = ButtonOption(
    "Host a game",
    description=(
        "Reloads your current map as a co-op host. Load into your game first. Only friends added"
        " in the BL4 Direct app (and your local network, if allowed) can connect."
    ),
    on_press=lambda _: host(),
)

friend_option = DropdownOption(
    "Friend",
    NO_FRIENDS,
    [NO_FRIENDS],
    description="Which friend's game to join. Add friends in the BL4 Direct app.",
)


def _join_selected(_: ButtonOption) -> None:
    refresh_friends()
    for friend in Config.load(FRIENDS_FILE).friends:
        if friend.name == friend_option.value:
            join(friend.address, friend.port)
            return
    log_error("No friend selected - add one in the BL4 Direct app, then press 'Reload friends'.")


join_button = ButtonOption(
    "Join selected friend",
    description="Connects to the selected friend's hosted game. Run from the main menu.",
    on_press=_join_selected,
)

reload_button = ButtonOption(
    "Reload friends",
    description="Picks up friends you just added in the BL4 Direct app.",
    on_press=lambda _: refresh_friends(),
)

status_button = ButtonOption(
    "Show status / versions",
    description="Prints versions to the console. Both players' versions should match.",
    on_press=lambda _: show_status(),
)

offline_button = ButtonOption(
    "Back to normal online play",
    description="Switches networking back to SHiFT for regular matchmaking and invites.",
    on_press=lambda _: (disable_direct(), show_status()),
)

# endregion
# region Hooks

FAILURE_HINTS = {
    "ConnectionTimeout": (
        "the host didn't answer. Check the address, that they pressed Host, that they've added"
        " your public IP in their BL4 Direct app, and (over the internet) that their port is"
        " forwarded."
    ),
    "NetDriverListenFailure": "couldn't open the port - is another program already using it?",
    "PendingNetGameCreateFailure": "couldn't create the network driver.",
}


# When a connection dies the game calls its error handlers every frame for a while (29 times in
# 0.4 s, observed), so only report each failure type once per few seconds
_last_reported: dict[str, float] = {}
REPORT_INTERVAL = 5.0


def _report_failure(kind: str, failure: Any) -> None:
    if not direct_active():
        return
    name = getattr(failure, "name", str(failure))
    now = time.monotonic()
    if now - _last_reported.get(name, -REPORT_INTERVAL) < REPORT_INTERVAL:
        return
    _last_reported[name] = now
    hint = next((h for key, h in FAILURE_HINTS.items() if key in name), "")
    log_error(f"{kind} failure: {name}" + (f" - {hint}" if hint else ""))


def _after_failure() -> None:
    """A join that failed or a friend's game that dropped us: the next solo load must use SHiFT."""
    global _client_seen
    if _hosting_port is None:
        _client_seen = False
        restore_driver_setting()


@hook("/Script/Engine.GameInstance:HandleNetworkError", Type.PRE)
def on_network_error(_obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    _report_failure("Network", args.FailureType)
    _after_failure()


@hook("/Script/Engine.GameInstance:HandleTravelError", Type.PRE)
def on_travel_error(_obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    _report_failure("Travel", args.FailureType)
    _after_failure()


# endregion
# region Fast travel with friends

_arrival_logged = False

# Friends who have finished arriving in the current hosted session. Their first arrival (joining)
# must be left alone: the flag stays unset for a joining friend and they get in fine, while setting
# it mid-join leaves them on a black screen (verified). Only later travels need the fix.
_arrived: set[str] = set()


def _prepare_arrival_fix() -> None:
    """Reports the scan for the game's client-ready setter (started at enable) once it's done."""
    global _arrival_logged
    arrival.start_scan()
    if not arrival.scan_done():
        return  # still scanning in the background; the next host reports it
    found = arrival.setter()
    if arrival.scan_error:
        logging.dev_warning(f"[BL4 Direct] arrival scan failed: {arrival.scan_error}")
    if not _arrival_logged:
        _arrival_logged = True
        if found is None:
            log("Couldn't find the fast-travel arrival fix in this game version - friends may get"
                " stuck in the portal after you fast travel.")
        else:
            logging.dev_warning(f"[BL4 Direct] arrival fix ready (flag offset {found.flag_offset:#x})")


@hook("/Script/OakGame.OakPlayerController:ServerNotifyCharacterReadyForGameplay", Type.PRE)
def on_friend_arrived(obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    if _hosting_port is None:
        return
    try:
        if not obj.IsLocalController():
            _arrived.add(obj.Name)
    except Exception as ex:  # noqa: BLE001 - a friend missing here gets dropped at the next fast travel
        logging.error(f"[BL4 Direct] couldn't note a friend's arrival (fast travel may drop them): {ex!r}")


def _mark_friends_ready(reason: str) -> None:
    """
    The host checks each friend's "client ready" flag when a teleport is requested. A friend whose
    flag is unset is dropped from the session on the spot and the host then waits in the portal
    for them forever (verified). The game only sets the flag for players it can verify online,
    which never happens for a directly-connected friend, so set it for them first.
    """
    if _hosting_port is None or not direct_active():
        return
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    driver = world.NetDriver if world is not None else None
    if driver is None:
        return
    # Only controllers of live connections: find_all would also list a friend who just left and
    # whose controller is torn down but not collected yet, and the setter would run on that
    for connection in list(driver.ClientConnections):
        try:
            pc = connection.PlayerController if connection is not None else None
            if pc is None or pc.Name not in _arrived or pc.IsLocalController():
                continue
            address = pc._get_address()
            if arrival.is_ready(address) is False:
                ok = arrival.mark_ready(address)
                logging.dev_warning(f"[BL4 Direct] marked {pc.Name} ready before {reason}: {ok}")
        except Exception as ex:  # noqa: BLE001 - a travel must never die in our hook
            logging.dev_warning(f"[BL4 Direct] arrival fix failed: {ex!r}")


@hook("/Script/OakGame.OakPlayerController:RequestTeleportToStation", Type.PRE)
def on_teleport_to_station(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    _mark_friends_ready("fast travel")


@hook("/Script/OakGame.OakPlayerController:RequestTeleportToPlayer", Type.PRE)
def on_teleport_to_player(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    _mark_friends_ready("teleport to player")


# endregion

refresh_friends()

mod = build_mod(
    options=[
        open_key,
        panel_button,
        host_button,
        friend_option,
        join_button,
        reload_button,
        status_button,
        offline_button,
    ],
    hooks=[
        on_network_error,
        on_travel_error,
        hosting_watchdog,
        on_friend_arrived,
        on_teleport_to_station,
        on_teleport_to_player,
    ],
    commands=[bl4host, bl4join, bl4direct, bl4ui],
    on_enable=lambda: (refresh_friends(), ui.install(), arrival.start_scan()),
    on_disable=lambda: (ui.uninstall(), disable_direct(), smooth.cleanup()),
)
