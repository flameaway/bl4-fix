"""
ECHO Anywhere for Borderlands 4: open the ECHO menu (inventory, map, missions, skills, ...) at any
time, including the moments the game normally refuses (Fight For Your Life, scripted sequences,
dialogue, cutscenes, ...).

When you press the ECHO menu key (Tab, or View on a controller) and the game says no, we open the
menu ourselves through the game's own internal open call (see native.py). When the game would open
it anyway, we leave it alone.

Keys are read from the Windows keyboard state (and XInput for controllers) like our other mods,
and only while the game window is in front.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

import unrealsdk
from mods_base import BoolOption, KeybindOption, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import native

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

# Fires several times a frame, in menus too
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0
# A press this soon after the menu was open is the key closing it, not a request to open it
CLOSE_GRACE = 0.5  # s
# The game refuses the menu while you spawn in (end of a load, fast travel, respawn) and opening
# it then breaks the spawn-in animation, so we leave those first seconds to the game
SPAWN_GRACE = 8.0  # s
XINPUT_GAMEPAD_BACK = 0x0020
PAD_PROBE_S = 2.0  # how often empty controller slots are checked for a new pad
VK_MENU = 0x12  # either Alt: Alt+Tab switches windows, it isn't a request for the menu

_pc_class = unrealsdk.find_class("OakPlayerController")

# ctypes.windll, not PyDLL: these calls must let go of the GIL (GetAsyncKeyState can take ~100 ms,
# e.g. while the desktop is streamed). Keys go through BL4 Mod Menu's key thread when it's loaded.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


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


class _XInputGamepad(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short),
    ]  # fmt: skip


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XInputGamepad)]  # noqa: RUF012


try:
    _xinput = ctypes.windll.xinput1_4
except OSError:
    _xinput = None


class _Pads:
    connected: set[int] = set()  # noqa: RUF012
    probed_at = 0.0
    state = _XInputState()


def _pad_back_down() -> bool:
    if _xinput is None:
        return False
    # Microsoft: don't read empty slots every frame (slow on some systems), look for new pads every
    # few seconds instead. Measured here: 2.5 us for all four, so this is only a guard.
    now = time.monotonic()
    pads = range(4) if now - _Pads.probed_at >= PAD_PROBE_S else tuple(_Pads.connected)
    if len(pads) == 4:  # noqa: PLR2004
        _Pads.probed_at = now
    down = False
    for pad in pads:
        if _xinput.XInputGetState(pad, ctypes.byref(_Pads.state)) != 0:
            _Pads.connected.discard(pad)
            continue
        _Pads.connected.add(pad)
        down = down or bool(_Pads.state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK)
    return down


def _game_window_focused() -> bool:
    hwnd = _user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _async_down(vk: int) -> bool:
    """
    Whether a key is held while the game is in front. Read by BL4 Mod Menu's key thread when it's
    loaded: GetAsyncKeyState can take ~100 ms on the game thread (a streamed desktop, 2026-10-24).
    """
    keys = sys.modules.get("bl4_mod_menu.keys")
    if keys is not None and hasattr(keys, "_Keys"):
        return keys.vk_down(vk)
    return _game_window_focused() and bool(_user32.GetAsyncKeyState(vk) & 0x8000)


class Keys:
    was_down = True  # a key held when we start doesn't count

    @classmethod
    def pressed(cls) -> bool:
        """True once per press of the ECHO menu key or button."""
        vk = _virtual_key(key_option.value)
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


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        return
    try:
        if not pc.Class._inherits(_pc_class):
            return  # manager_offset is an OakPlayerController field
        menu = _menu(pc)
        if menu is None:
            return
        now = time.monotonic()
        if menu.state(pc) != 0:
            State.last_open = now
        pawn = pc.Pawn._get_address()
        if pawn != State.pawn:
            State.pawn, State.spawned = pawn, now
        if not _game_window_focused():
            Keys.was_down = True
            return
        if not Keys.pressed() or now - State.last_open < CLOSE_GRACE:
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
