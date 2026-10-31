"""
BL4 Direct's page in the BL4 Mod Menu screen (F6, or BL4 Direct Connect in the MODS list).

The Mod Menu's screen is HTML in the game's own menu page, built from the game's Options menu
parts, so it looks native. This borrows it: bl4_mod_menu.extensions loads ui/direct.js into the
page, and the script draws this mod's page there (the Mod Menu's ns.modPages hook) with the Mod
Menu's own rows and buttons: session status, your address (Copy), Host / Stop / Leave, the
friends list (Join / Remove), Add a friend, the firewall and versions. Its state comes from
state() here (GET /ext?k=bl4direct) and clicks come back through act().

F6 opens the Mod Menu on this page (the pause menu never shows on the way, the Mod Menu sees to
that) and closes it again. Where the page can't show (no Mod Menu, or its page doesn't answer)
the UMG panel in ui.py opens instead, as before.

Typing: no key events reach the menus page, so a field being typed in uses the BL4 UI Link's
TextBox from the Mod Menu's copy (the game's own text input on the ECHO overlay page, its keys kept
from the game's menus; the page only exists inside the Mod Menu's screen). Enter / Tab move on, Esc
stops, Ctrl+V pastes.

Host, Stop, Leave and Join travel: the page closes the menus first and the action runs here a
moment later, once they're down (never travel from inside the UI).
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging

from . import clipboard, ui
from .shared import FRIENDS_FILE_NAME, UAC_HINT, Config, find_rules, is_ipv4

KEY = "bl4direct"
# The page is drawn for the mod with this name in the Mod Menu (its pyproject name)
MOD_NAME = "BL4 Direct Connect"
GAME_DIR = SETTINGS_DIR.parent.parent
# Not "bl4direct": that folder belongs to Detailed Item Cards
UI_DIR = "OakGame/Content/uiresources/bl4directconnect"
SCRIPT_URL = "coui://uiresources/bl4directconnect/direct.js"
UI_FILES = ("direct.js", "direct.css")
PUBLISH_S = 1.0
# Loaded from a folder (the dev junction): its files can change while the game runs, so they're
# re-published while the page shows. A release (.sdkmod) publishes once, at registration.
DEV = (Path(__file__).parent / "ui").is_dir()
# The Mod Menu may load after us (mods load in name order): look for it at most this often
REGISTER_RETRY_S = 1.0
# The script polls every 0.25 s while the page shows
SHOWN_S = 0.6
# A travel action waits for the menus to close, at least this long and at most TRAVEL_MAX_S
TRAVEL_MIN_S = 0.15
TRAVEL_MAX_S = 1.5
# What Windows Firewall has is read again at most this often while the page shows (a hidden
# PowerShell on a worker, ~0.6 s), and at once after a prompt or a switch
LIVE_S = 30.0
MAX_NAME = 24
MAX_IP = 21  # 255.255.255.255:65535
# The old options the page itself replaces: hidden on the HTML page (they stay in the basic panel)
HIDDEN_OPTIONS = ("Open the basic panel", "Host a game", "Friend", "Join selected friend")


def _ip_char(char: str) -> bool:
    return char.isdigit() or char in ".:"


FIELDS: dict[str, tuple[int, Callable[[str], bool]]] = {
    "name": (MAX_NAME, str.isprintable),
    "ip": (MAX_IP, _ip_char),
}


class S:
    registered = False
    register_tried = 0.0
    published_at = 0.0
    fields: dict[str, str] = {"name": "", "ip": ""}  # noqa: RUF012
    typing_field: str | None = None
    typing: Any = None  # the Mod Menu's uilink.TextBox while a field is typed in
    toast: dict[str, Any] = {"id": 0, "text": "", "error": False}  # noqa: RUF012
    # The versions line (reads Steam's manifest: once a session is enough)
    compat: str | None = None
    # A travel waiting for the menus to close: (action, asked at, the Mod Menu's menus_closed then)
    travel: tuple[Callable[[], None], float, int] | None = None
    # Why the page asks (Continue / Not now) before Windows' prompt: "friends", "on", "off", "lan",
    # or "" (2026-10-30, the user's ask: only when the list or a firewall setting changes; Host never)
    ask_firewall = ""
    was_asking = False


class Live:
    """Our rules in Windows Firewall right now (find_rules on a worker), for the Status row."""

    rules: list[tuple[str, str]] | None = None  # None: not read yet
    error: str | None = None
    checked = 0.0
    busy = False

    @classmethod
    def refresh(cls, *, force: bool = False) -> None:
        if cls.busy or (not force and time.monotonic() - cls.checked < LIVE_S):
            return
        cls.busy, cls.checked = True, time.monotonic()
        threading.Thread(target=cls._read, name="bl4_direct.firewall_read", daemon=True).start()

    @classmethod
    def _read(cls) -> None:
        try:
            cls.rules, cls.error = find_rules(), None
        except Exception as ex:  # noqa: BLE001
            cls.error = str(ex)
        finally:
            cls.checked, cls.busy = time.monotonic(), False


def _menu_module(name: str) -> Any:
    return sys.modules.get(f"bl4_mod_menu.{name}")


def _changed() -> None:
    if (exts := _menu_module("extensions")) is not None:
        exts.changed(KEY)


def say(text: str, *, error: bool = False) -> None:
    """A message for the page (shown as the Mod Menu's toast)."""
    S.toast = {"id": S.toast["id"] + 1, "text": text, "error": error}
    _changed()


# region State and actions


def state() -> dict[str, Any]:
    api = ui.api
    ui.PublicIp.lookup()  # nothing to do once it's known
    mode, connected = ui.session()
    config = api.config() if hasattr(api, "config") else None
    friends = config.friends if config is not None else api.friends()
    port = config.host_port if config is not None else api.host_port()
    return {
        "mode": mode,
        "connected": connected,
        "port": port,
        "intent": api.hosting_intent() is not None,
        "in_game": api.in_gameplay(),
        "ip": {"state": ui.PublicIp.state, "ip": ui.PublicIp.ip or ""},
        "firewall": config.firewall_up_to_date() if config is not None else api.firewall_up_to_date(),
        # Turned off in the setup app: no rule, and hosting doesn't ask Windows for anything
        "firewall_off": config is not None and not config.firewall,
        "asking": ui.Elevated.busy,
        "fw": (fw := _firewall_status(config)),
        "ask_firewall": S.ask_firewall if fw["action"] else "",
        "friends": [{"name": f.name, "address": f.address, "port": f.port} for f in friends],
        "compat": _compat(),
        "key": ui.key_text(),
        "fields": S.fields,
        "typing": S.typing_field,
        "toast": S.toast,
        "hide": HIDDEN_OPTIONS,
    }


def _friends_file() -> Path:
    return SETTINGS_DIR / FRIENDS_FILE_NAME


def _firewall_status(config: Config | None) -> dict[str, Any]:
    """
    The Firewall section: the two switches and what's blocked or let in, from the friends file
    and what Windows Firewall has right now (Live). action: "update", "remove" or None.
    """
    asking = ui.Elevated.busy
    if S.was_asking and not asking:
        Live.refresh(force=True)  # a prompt just ended: show what it did
    S.was_asking = asking
    Live.refresh()
    if config is None:
        return {"on": False, "lan": False, "summary": "", "details": "", "cls": "", "action": None}
    port = config.host_port
    friends = [f for f in config.friends if is_ipv4(f.address)]
    rules = Live.rules
    live = (
        "checking" if rules is None and Live.error is None
        else f"couldn't read it ({Live.error})" if rules is None
        else "; ".join((d or n).rstrip(".") for n, d in rules) if rules
        else "no BL4 Direct rule"
    )  # fmt: skip
    present = bool(rules) if rules is not None else None  # None: not known yet
    current = config.firewall_up_to_date()
    if config.firewall:
        allowed = ["this PC"]
        if config.allow_lan:
            allowed.append("your local network and VPN (home networks, Tailscale, ZeroTier)")
        allowed += [f"{f.name} ({f.address})" for f in friends]
        details = (
            f"UDP {port}, the co-op port, lets in: {'; '.join(allowed)}. Everyone else on the internet is blocked."
            if current and present is not False
            else f"Not applied yet: Windows Firewall doesn't match your list. Update: Windows asks if "
                 f'"Network Command Shell" may make changes (netsh, its own firewall tool). Then UDP {port} '
                 f"lets in: {'; '.join(allowed)}; everyone else is blocked."
        )  # fmt: skip
        if present is False:
            summary, cls, action = "Not in Windows Firewall yet", "bl4dc_warn", "update"
        elif not current:
            summary, cls, action = "Needs updating", "bl4dc_warn", "update"
        else:
            who = ["this PC"] + (["local network"] if config.allow_lan else [])
            if friends:
                who.append(f"{len(friends)} friend{'s' if len(friends) != 1 else ''}")
            summary, cls, action = "Lets in " + ", ".join(who), "bl4dc_on", None
    else:
        details = (
            f"No BL4 Direct rule: Steam's own rule lets anyone who can reach UDP {port} (port scanners, "
            "anyone with your IP) try to connect while you host. Turn the rule on so only your friends get through."
        )  # fmt: skip
        if present:
            summary, cls, action = "Off, old rule still there", "bl4dc_warn", "remove"
            details = "Turned off, but BL4 Direct's rule is still in Windows Firewall. Remove takes it out. " + details
        else:
            summary, cls, action = "Off: anyone can try to join", "bl4dc_warn", None
    details += f"\n\nWindows Firewall now: {live}."
    return {"on": config.firewall, "lan": config.allow_lan, "summary": summary, "details": details,
            "cls": cls, "action": action}  # fmt: skip


def _set_firewall_setting(field: str, on: bool, reason: str) -> None:
    """Saves a switch of the Firewall section; asks (Continue / Not now) if the rule must change."""
    path = _friends_file()
    config = Config.load(path)
    if getattr(config, field) == on:
        return
    setattr(config, field, on)
    if not config.firewall and Live.rules == []:
        # Off with no rule of ours in Windows: nothing to remove, nothing to ask
        config.firewall_applied = config.firewall_signature()
    config.save(path)
    Live.refresh(force=True)
    if config.firewall_up_to_date() and not (field == "firewall" and not on and Live.rules):
        S.ask_firewall = ""
        say("Saved.")
    else:
        S.ask_firewall = reason
        say("Saved. Windows Firewall changes when you Continue at the top of this page.")


def _compat() -> str:
    if S.compat is None:
        S.compat = ui.api.compat_line()
    return S.compat


def act(action: dict[str, Any]) -> None:
    api = ui.api
    match action.get("do"):
        case "type":
            field = action.get("field")
            if field in FIELDS:
                start_typing(str(field))
            else:
                stop_typing()
        case "paste":
            field = str(action.get("field"))
            if field in FIELDS:
                stop_typing()
                max_len, allowed = FIELDS[field]
                text = "".join(c for c in clipboard.clipboard_text().strip() if allowed(c))[:max_len]
                if text:
                    S.fields[field] = text
                else:
                    say("Nothing to paste: copy their address first.", error=True)
        case "clear":
            field = str(action.get("field"))
            if field in FIELDS:
                stop_typing()
                S.fields[field] = ""
        case "add":
            stop_typing()
            _add_friend()
        case "remove":
            name = str(action.get("name") or "")
            api.remove_friend(name)
            _after_friends_changed(f"Removed {name}.")
        case "copy":
            text = ui.share_address() if action.get("what") == "address" else ui.PublicIp.ip
            if text and ui.copy_to_clipboard(text):
                say(f"Copied {text}. Paste it to your friend.")
            else:
                say("Couldn't copy to the clipboard.", error=True)
        case "retry_ip":
            ui.PublicIp.state = "idle"
            ui.PublicIp.lookup()
        case "firewall":
            S.ask_firewall = ""
            if error := api.update_firewall():
                say(error, error=True)
            else:
                say(UAC_HINT)
        case "firewall_later":
            S.ask_firewall = ""
            say("Not now: Windows Firewall stays as it is until you press Update (Firewall section, Status).")
        case "set_firewall":
            on = bool(action.get("on"))
            _set_firewall_setting("firewall", on, "on" if on else "off")
        case "set_lan":
            _set_firewall_setting("allow_lan", bool(action.get("on")), "lan")
        case "dont_resume":
            api.cancel_hosting_intent()
        case "host":
            _travel(ui.host_now)
        case "stop":
            _travel(api.stop_hosting)
        case "leave":
            _travel(api.leave)
        case "join":
            name = str(action.get("name") or "")
            friend = next((f for f in api.friends() if f.name == name), None)
            if friend is None:
                say(f"{name} isn't in your friends list any more.", error=True)
            else:
                _travel(lambda: api.join_friend(friend))
        case _:
            pass


def _add_friend() -> None:
    api = ui.api
    name, address = S.fields["name"].strip(), S.fields["ip"].strip()
    ip, _, port_text = address.partition(":")
    port = int(port_text) if port_text.isdigit() and 0 < int(port_text) < 65536 else None
    if not name:
        say("Type their name first.", error=True)
    elif not api.is_ipv4(ip) or (port_text and port is None):
        say("That IP doesn't look right: it should look like 203.0.113.5", error=True)
    else:
        if port is not None and hasattr(api, "config"):
            api.add_friend(name[:MAX_NAME], ip, port)
        else:
            api.add_friend(name[:MAX_NAME], ip)
        S.fields = {"name": "", "ip": ""}
        _after_friends_changed(f"Added {name}.")


def _after_friends_changed(done: str) -> None:
    """
    The firewall rule follows the friend list (2026-10-30, the user's ask): when a change leaves it
    behind, the page asks at the top (Continue / Not now) before Windows' prompt, whether or not
    you're hosting. Hosting itself never asks.
    """
    if ui.api.firewall_up_to_date():  # also when the rule is turned off
        S.ask_firewall = ""
        say(done)
    else:
        S.ask_firewall = "friends"
        say(f"{done} Update your firewall to let them in: Continue at the top of this page.")


def _travel(action: Callable[[], None]) -> None:
    """Runs a map change once the menus are down (the page closes them as it sends this)."""
    stop_typing()
    S.travel = (action, time.monotonic(), _menus_closed())


def _menus_closed() -> int:
    return getattr(getattr(_menu_module("web"), "Link", None), "menus_closed", 0)


def _run_travel() -> None:
    if S.travel is None:
        return
    action, asked, closed = S.travel
    waited = time.monotonic() - asked
    if waited < TRAVEL_MIN_S:
        return
    pc = get_pc(possibly_loading=True)
    menus_down = pc is None or not pc.bShowMouseCursor or pc.Pawn is None or _menus_closed() != closed
    if not menus_down and waited < TRAVEL_MAX_S:
        return
    S.travel = None
    try:
        action()
    except Exception as ex:  # noqa: BLE001
        ui.log(f"Couldn't do that: {ex!r}")
        logging.error("".join(traceback.format_exception(ex)))


# endregion
# region Typing


def start_typing(field: str) -> None:
    stop_typing()
    max_len, allowed = FIELDS[field]
    link = _menu_module("uilink")
    if link is None or not hasattr(link, "TextBox"):
        say("Typing needs a newer BL4 Mod Menu. Use Paste, or the BL4 Direct app.", error=True)
        return
    S.typing = link.TextBox(S.fields[field], max_len, clipboard.clipboard_text, allowed=allowed, named=True)
    S.typing_field = field
    _changed()


def stop_typing() -> None:
    typing, field = S.typing, S.typing_field
    S.typing, S.typing_field = None, None
    if typing is None:
        return
    typing.stop()
    if field is not None:
        S.fields[field] = typing.text
    _changed()


def _poll_typing() -> None:
    typing, field = S.typing, S.typing_field
    if typing is None or field is None:
        return
    exts = _menu_module("extensions")
    # The page closed (or the menus did): the game's keys work again
    if exts is None or not exts.page_open(KEY, SHOWN_S):
        stop_typing()
        return
    if typing.poll():
        S.fields[field] = typing.text
        _changed()
    if typing.done is None:
        return
    done = typing.done
    stop_typing()
    if done == "tab" or (done == "enter" and field == "name"):
        start_typing("ip" if field == "name" else "name")
    elif done == "enter":
        _add_friend()


# endregion
# region Opening


def available() -> bool:
    """The Mod Menu is loaded, its link to the page is running, and our script is registered."""
    web = _menu_module("web")
    return S.registered and web is not None and getattr(web.Link, "server", None) is not None


def showing() -> bool:
    """Our page is up in the Mod Menu screen right now."""
    exts = _menu_module("extensions")
    pc = get_pc(possibly_loading=True)
    return exts is not None and pc is not None and bool(pc.bShowMouseCursor) and exts.page_open(KEY, SHOWN_S)


def toggle() -> bool:
    """
    F6: opens the Mod Menu on our page, or closes it if it's showing. False if the HTML page
    can't be used (no Mod Menu, its basic UMG panel is up): then the caller opens ui.py's panel.
    """
    menu, web = _menu_module("menu"), _menu_module("web")
    if not available() or menu is None or web is None or getattr(menu.S, "is_open", False):
        return False
    if showing():
        web.request_close()
        return True
    return open_page()


def open_page() -> bool:
    """Opens the Mod Menu screen on our page. False if the HTML page can't be used."""
    menu = _menu_module("menu")
    if not available() or menu is None or getattr(menu.S, "is_open", False):
        return False
    try:
        menu.open_preferred(MOD_NAME, fallback=ui.open_panel)
    except TypeError:  # a Mod Menu from before fallbacks: it falls back to its own basic panel
        menu.open_preferred(MOD_NAME)
    return True


# endregion
# region Link


def _register() -> None:
    now = time.monotonic()
    if S.registered or now - S.register_tried < REGISTER_RETRY_S:
        return
    S.register_tried = now
    exts = _menu_module("extensions")
    if exts is None:
        return
    publish(force=True)
    exts.register(KEY, SCRIPT_URL, state, act)
    S.registered = True


def unregister() -> None:
    stop_typing()
    S.travel = None
    if S.registered and (exts := _menu_module("extensions")) is not None:
        exts.unregister(KEY)
    S.registered = False


def publish(*, force: bool = False) -> None:
    """Copies the page's files to uiresources/bl4directconnect/, touching only files that changed."""
    now = time.monotonic()
    if not force and now - S.published_at < PUBLISH_S:
        return
    S.published_at = now
    for name in UI_FILES:
        try:
            content = (resources.files(__package__) / "ui" / name).read_bytes()
            target = Path(GAME_DIR) / UI_DIR / name
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(target)
        except OSError as ex:
            logging.dev_warning(f"[BL4 Direct] couldn't publish {name}: {ex}")


def tick() -> None:
    """Every camera hook call (from ui.py's tick)."""
    if not S.registered:
        _register()
        return
    if S.travel is not None:
        _run_travel()
    if S.typing is not None:
        _poll_typing()
    # Only in a dev setup, and only while the page shows (a release's files can't change); asked
    # once per PUBLISH_S, not every frame (page_open was ~10 us a frame, 2026-10-29)
    global _dev_next  # noqa: PLW0603
    if DEV and S.typing is None and (now := time.monotonic()) >= _dev_next:
        _dev_next = now + PUBLISH_S
        if (exts := _menu_module("extensions")) is not None and exts.page_open(KEY, SHOWN_S):
            publish()


_dev_next = 0.0  # dev: when tick() next looks at whether to republish the page's files


# endregion
