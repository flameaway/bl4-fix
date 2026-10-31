"""
Unreal key names, the Windows virtual-key codes we still key them by, and the key state every mod
reads (vk_down / key_down / held_keys).

Keys come from the game's own input (gamekeys: the game's key map, see "Key state" below), not
from Windows. Names are the ones mods_base keybinds use (Unreal's FKey names); the virtual-key
codes stay as the table's keys so callers didn't have to change.
"""

from __future__ import annotations

import ctypes

from . import gamekeys

# ctypes.windll, not PyDLL: these calls must let go of the GIL.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]

VK_ESCAPE = 0x1B
VK_LBUTTON = 0x01
POSTED_IGNORE_S = 0.25  # a key we posted ourselves (post_key) doesn't count as pressed for this long

KEY_NAMES: dict[int, str] = {
    **{0x41 + i: chr(0x41 + i) for i in range(26)},
    **{0x30 + i: name for i, name in enumerate(_DIGITS)},
    **{0x70 + i: f"F{i + 1}" for i in range(12)},
    **{0x60 + i: f"NumPad{name}" for i, name in enumerate(_DIGITS)},
    0x6A: "Multiply", 0x6B: "Add", 0x6D: "Subtract", 0x6E: "Decimal", 0x6F: "Divide",
    0x08: "BackSpace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock",
    0x20: "SpaceBar", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2D: "Insert", 0x2E: "Delete",
    0x90: "NumLock", 0x91: "ScrollLock",
    0xA0: "LeftShift", 0xA1: "RightShift", 0xA2: "LeftControl", 0xA3: "RightControl",
    0xA4: "LeftAlt", 0xA5: "RightAlt",
    0xBA: "Semicolon", 0xBB: "Equals", 0xBC: "Comma", 0xBD: "Hyphen", 0xBE: "Period",
    0xBF: "Slash", 0xC0: "Tilde", 0xDB: "LeftBracket", 0xDC: "Backslash", 0xDD: "RightBracket",
    0xDE: "Apostrophe",
    # Left click is how you start a rebind, so it can't be bound here
    0x02: "RightMouseButton", 0x04: "MiddleMouseButton", 0x05: "ThumbMouseButton",
    0x06: "ThumbMouseButton2",
}  # fmt: skip
VIRTUAL_KEYS: dict[str, int] = {name: vk for vk, name in KEY_NAMES.items()}

# How a few names read on screen
_DISPLAY = {
    "SpaceBar": "Space", "BackSpace": "Backspace", "LeftControl": "Left Ctrl",
    "RightControl": "Right Ctrl", "LeftShift": "Left Shift", "RightShift": "Right Shift",
    "LeftAlt": "Left Alt", "RightAlt": "Right Alt", "RightMouseButton": "Right Mouse",
    "MiddleMouseButton": "Middle Mouse", "ThumbMouseButton": "Mouse 4",
    "ThumbMouseButton2": "Mouse 5", "PageUp": "Page Up", "PageDown": "Page Down",
}  # fmt: skip


def display_name(key: str | None) -> str:
    if not key:
        return "Unbound"
    if key in _DIGITS:
        return str(_DIGITS.index(key))
    return _DISPLAY.get(key, key)


# The last foreground window and whether it's ours: one tuple, replaced whole (any thread)
_focus: tuple[int, bool] = (0, False)


def game_focused() -> bool:
    """
    True if the game window is the foreground window (so key state is meant for us). One Win32 call
    while the foreground window stays the same (it was 3 a call; every ctypes call lets other
    threads take the GIL from the game thread, 2026-10-29).
    """
    global _focus  # noqa: PLW0603
    hwnd = _user32.GetForegroundWindow()
    known, ours = _focus
    if hwnd != known or not hwnd:
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        ours = bool(hwnd) and pid.value == _kernel32.GetCurrentProcessId()
        _focus = (hwnd, ours)
    return ours


# region Key state: the game's own input
#
# gamekeys (a copy of shared/gamekeys, shared by every mod that carries it) reads the game's
# PlayerInput key state: in play and with menus up, mouse and pad buttons too, held keys dropped
# when the window loses focus. No Windows key reads, no keyboard hooks, no XInput (2026-10-30).
# Until then a worker thread here polled GetAsyncKeyState and XInput (`pad_state`).


class _Keys:
    """Kept: other mods look for it (hasattr) to know this module reads keys."""


def vk_down(vk: int) -> bool:
    """Whether a key is held (see gamekeys.down). Any thread; read at most once a frame."""
    return gamekeys.vk_down(vk)


def mute(seconds: float) -> None:
    """No key counts as held for this long, for every mod (a text box of ours is being typed in)."""
    gamekeys.mute(seconds)


# endregion


def key_down(name: str | None) -> bool:
    """Whether the named key (an Unreal key name, pads too) is held: see gamekeys.down."""
    return gamekeys.down(name)


def held_keys() -> set[int]:
    """Every key in the table that's currently held (~100 reads the first time in a frame: for a
    rebind capture, not for every frame)."""
    return {vk for vk in KEY_NAMES if vk_down(vk)}


WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101


def _game_window() -> int | None:
    """The game's main window (the visible top-level one of this process)."""
    found: list[int] = []
    pid = _kernel32.GetCurrentProcessId()
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def check(hwnd: int, _: int) -> bool:
        owner = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(proc(check), None)
    return found[0] if found else None


def post_key(vk: int, *, down: bool) -> bool:
    """
    Sends a key down or up message to the game window. The game samples input once a frame, so a
    press only counts if the up comes a few frames after the down (a same-frame tap is ignored).
    """
    hwnd = _game_window()
    if hwnd is None:
        return False
    lparam = 1 if down else 0xC0000001
    # The game's input sees a posted key as pressed (Windows' key state never did): ours isn't a press
    if name := gamekeys.VK_NAMES.get(vk):
        gamekeys.ignore(name, 5.0 if down else POSTED_IGNORE_S)
    _user32.PostMessageW(ctypes.c_void_p(hwnd), WM_KEYDOWN if down else WM_KEYUP, vk, lparam)
    return True
