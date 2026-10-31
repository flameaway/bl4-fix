"""
ECHO Anywhere for Borderlands 4: open the ECHO menu (inventory, map, missions, skills, ...) at any
time, including the moments the game normally refuses (Fight For Your Life, scripted sequences,
dialogue, cutscenes, ...).

When you press the ECHO menu key (Tab, or View on a controller) and the game says no, we open the
menu ourselves through the game's own internal open call (see native.py). When the game would open
it anyway, we leave it alone.

Keys and the pad's View button come from the game's own input (gamekeys, like our other mods),
only while the game window is in front.
"""

from __future__ import annotations

import ctypes
import time

import unrealsdk
from mods_base import BoolOption, KeybindOption, build_mod, get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

from . import frametick, gamekeys, health, native

__version__ = "0.1.3"
__version_info__ = (0, 1, 3)

# A press this soon after the menu was open is the key closing it, not a request to open it
CLOSE_GRACE = 0.5  # s
# The game refuses the menu while you spawn in (end of a load, fast travel, respawn) and opening
# it then breaks the spawn-in animation, so we leave those first seconds to the game
SPAWN_GRACE = 8.0  # s
PAD_VIEW = "Gamepad_Special_Left"  # the View button, Action_StatusMenu's pad binding
VK_MENU = 0x12  # either Alt: Alt+Tab switches windows, it isn't a request for the menu

_pc_class = unrealsdk.find_class("OakPlayerController")

# ctypes.windll, not PyDLL: these calls must let go of the GIL. Only the focus check uses them; keys
# come from the game's own input (gamekeys).
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_focus: tuple[int, bool] = (0, False)  # (foreground window last seen, whether it's ours)


def log(msg: str) -> None:
    logging.info(f"[ECHO Anywhere] {msg}")


key_option = KeybindOption(
    "ECHO menu key",
    "Tab",
    description="The key you open the ECHO menu with. Change it if you rebound it in the game.",
)
pad_option = BoolOption(
    "Controller View button",
    True,
    description="Also opens the ECHO menu with the controller's View (Back) button.",
)


# region Input

_VIRTUAL_KEYS = {
    "Tab": 0x09, "Enter": 0x0D, "CapsLock": 0x14, "SpaceBar": 0x20, "LeftShift": 0xA0,
    "RightShift": 0xA1, "LeftControl": 0xA2, "RightControl": 0xA3, "LeftAlt": 0xA4, "RightAlt": 0xA5,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Tilde": 0xC0, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF, "Semicolon": 0xBA,
    "LeftBracket": 0xDB, "RightBracket": 0xDD, "Backslash": 0xDC, "Apostrophe": 0xDE,
    "ThumbMouseButton": 0x05, "ThumbMouseButton2": 0x06, "MiddleMouseButton": 0x04,
}  # fmt: skip
_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]


def _virtual_key(name: str | None) -> int | None:
    """Windows key code for an Unreal key name."""
    if not name:
        return None
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in _DIGITS:
        return ord("0") + _DIGITS.index(name)
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:  # noqa: PLR2004
        return 0x70 + int(name[1:]) - 1
    return _VIRTUAL_KEYS.get(name)


def _pad_back_down() -> bool:
    # The game's own input (gamekeys): the View button. Until 2026-10-30 XInput, through BL4 Mod
    # Menu's key thread when it was loaded (~28 us a frame on the game thread otherwise)
    return gamekeys.down(PAD_VIEW)


def _game_window_focused() -> bool:
    """One Win32 call while the foreground window stays the same (it was 3 every call)."""
    global _focus  # noqa: PLW0603
    hwnd = _user32.GetForegroundWindow()
    known, ours = _focus
    if hwnd != known or not hwnd:
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        ours = bool(hwnd) and pid.value == _kernel32.GetCurrentProcessId()
        _focus = (hwnd, ours)
    return ours


def _async_down(vk: int) -> bool:
    """
    Whether a key is held, from the game's own input (gamekeys, shared by our mods; False while the
    game isn't in front). Until 2026-10-30 this read Windows (GetAsyncKeyState, through BL4 Mod
    Menu's key thread when it was loaded).
    """
    return gamekeys.vk_down(vk)

class Keys:
    was_down = True  # a key held when we start doesn't count
    code: tuple[str | None, int | None] = (None, None)  # (key name, _virtual_key(name)): ~5 us of string work

    @classmethod
    def pressed(cls) -> bool:
        """True once per press of the ECHO menu key or button."""
        name, vk = cls.code
        if name != key_option.value:
            name = key_option.value
            vk = _virtual_key(name)
            cls.code = (name, vk)
        down = vk is not None and _async_down(vk)
        if down and _async_down(VK_MENU):
            down = False
        if not down and pad_option.value:
            down = _pad_back_down()
        was, cls.was_down = cls.was_down, down
        return down and not was


# endregion
# region Menu


class State:
    menu: native.StatusMenu | None = None
    failed = False
    last_open = 0.0  # when we last saw the menu open
    pawn = 0  # address of the pawn we last saw, and when it appeared
    spawned = 0.0
    polled_at = -1e9  # the last read of the menu state and the pawn (STATE_POLL_S)
    open_seen = False  # the menu wasn't closed at that read: read every frame until it is


# Between key presses the menu state and the pawn are read this often (every frame while the menu
# is open, and at once on a press). They were read every frame, with 3 Win32 calls for the window
# focus: the mod's whole cost in play (Mod Profiler, 2026-10-29). They only feed the grace times
# (CLOSE_GRACE 0.5 s, SPAWN_GRACE 8 s), which a press checks again itself.
STATE_POLL_S = 0.1


def _poll(pc: UObject, menu: native.StatusMenu, now: float) -> None:
    State.polled_at = now
    State.open_seen = menu.state(pc) != 0
    if State.open_seen:
        State.last_open = now
    pawn = pc.Pawn._get_address()
    if pawn != State.pawn:
        State.pawn, State.spawned = pawn, now


def _menu(pc: UObject) -> native.StatusMenu | None:
    if State.menu is None and not State.failed:
        # Any error counts (a renamed function, a stray read on an unmapped page...): one try per
        # session, not one per frame
        State.failed = True
        try:
            State.menu = native.find(pc)
        except native.NotFound as ex:
            logging.error(f"[ECHO Anywhere] this game version isn't supported ({ex}); doing nothing")
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[ECHO Anywhere] couldn't find the menu functions ({ex!r}); doing nothing")
        else:
            State.failed = False
    return State.menu


@frametick.tick("bl4_echo_anywhere")  # once per engine frame, in menus too (the shared Frame Tick, frametick/)
def tick() -> None:
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
    except Exception:  # noqa: BLE001 - never let it break the game's tick
        pass
    now = time.monotonic()
    try:
        # The key first: the game's own input (gamekeys, False while the game isn't in front). The
        # rest only on a press, while the menu is open, or every STATE_POLL_S
        pressed = Keys.pressed()
        if not pressed and not State.open_seen and now - State.polled_at < STATE_POLL_S:
            return
        State.polled_at = now
        pc = get_pc(possibly_loading=True)
        if pc is None or pc.Pawn is None:
            State.open_seen = False
            return
        if not pc.Class._inherits(_pc_class):
            return  # manager_offset is an OakPlayerController field
        menu = _menu(pc)
        if menu is None:
            return
        _poll(pc, menu, now)
        if not pressed:
            return
        if not _game_window_focused():
            return  # a controller's button in another window (Keys.was_down stays set)
        if now - State.last_open < CLOSE_GRACE:
            return
        if now - State.spawned < SPAWN_GRACE:
            return
        if pc.ActiveCinematicModeIdent.TokenCount:
            # A cinematic mode (the DLC's no-weapon flashbacks: CinMode_Nightmare) swaps in its
            # own input; opening the menu there left the cursor on and the camera on click-drag
            log(f"left Tab to the game: cinematic mode {pc.ActiveCinematicModeIdent.InlineTokens.Name}")
            return
        if menu.can_open(pc):
            return  # the game opens it itself
        menu.open(pc)
        log("opened the ECHO menu")
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[ECHO Anywhere] {ex!r}")


def _enable() -> None:
    Keys.was_down = True


# endregion

mod = build_mod(
    options=[key_option, pad_option],
    hooks=[tick],
    on_enable=_enable,
)
