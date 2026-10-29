"""
Unreal key names and the Windows virtual-key codes for them.

The menu reads the keyboard from Windows (GetAsyncKeyState) because while it's open the game is in
UI-only input mode and PlayerInput sees nothing. So both opening the menu and rebinding a key go
through this table. Names are the ones mods_base keybinds use (Unreal's FKey names).
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time

# ctypes.windll, not PyDLL: these calls must let go of the GIL (see the key poller below).
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]

VK_ESCAPE = 0x1B
VK_LBUTTON = 0x01

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


# region Key state, read on a worker thread
#
# GetAsyncKeyState can be slow at the OS level: while the desktop is streamed (Sunshine / Moonlight
# through a virtual display driver) it took 3 ms at p90 and ~100 ms at worst, and the game thread
# spent half its time in our mods' key polls (~14 fps, 2026-10-24). So one thread reads the keys
# mods asked about and the game thread only looks at its last snapshot. GetForegroundWindow stays
# cheap (0.003 ms), so that check is first.

# Snapshot age at worst. A tap shorter than this still shows: a key Windows says was pressed since
# the last read counts as down for one snapshot. It was 15 ms: 67 polls a second, each Win32 call of
# which hands the GIL back and forth with the game thread (Mod Profiler, 2026-10-29)
POLL_S = 0.03
FORGET_S = 2.0  # a key nobody asked about for this long isn't read any more
IDLE_EXIT_S = 5.0  # with no asks for this long the thread stops (a later ask starts it again)


class _Keys:
    asked: dict[int, float] = {}  # noqa: RUF012 - vk -> time.monotonic() it was last asked about
    down: frozenset[int] = frozenset()  # held as of the last poll, empty while the game isn't in front
    thread: threading.Thread | None = None


def _poll(state: type[_Keys]) -> None:
    # Win32 calls only (no unreal objects): fine off the game thread
    was_focused = False
    polled: set[int] = set()
    while getattr(sys.modules.get(__name__), "_Keys", None) is state:  # a hot reload made a new one
        now = time.monotonic()
        wanted = [vk for vk, at in list(state.asked.items()) if now - at < FORGET_S]
        if not wanted and now - max(state.asked.values(), default=0.0) > IDLE_EXIT_S:
            return
        focused = bool(wanted) and game_focused()
        if focused:
            down = set()
            for vk in wanted:
                bits = _user32.GetAsyncKeyState(vk)
                # Bit 0 = pressed since the last read: only trusted for a key read last poll too,
                # with the game in front all along (else it's a press from long ago, or in another
                # window: Alt+Tab)
                if bits & 0x8000 or (bits & 1 and was_focused and vk in polled):
                    down.add(vk)
            state.down = frozenset(down)
            polled = set(wanted)
        else:
            state.down = frozenset()
            polled = set()
        was_focused = focused
        time.sleep(POLL_S)


def vk_down(vk: int) -> bool:
    """
    Whether a key is held, from the key thread's last snapshot (at most ~POLL_S old; False while the
    game isn't in front). Only a key's first ask (or the first after FORGET_S) reads Windows here,
    so "held when we started" stays right for edge detection and rebinding.
    """
    now = time.monotonic()
    fresh = now - _Keys.asked.get(vk, -FORGET_S) >= FORGET_S
    _Keys.asked[vk] = now
    thread = _Keys.thread
    if thread is None or not thread.is_alive():
        _Keys.thread = threading.Thread(target=_poll, args=(_Keys,), name="bl4_mod_menu.keys", daemon=True)
        _Keys.thread.start()
    if fresh:
        return game_focused() and bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    return vk in _Keys.down


# endregion


def key_down(name: str | None) -> bool:
    """Whether the named key is held, read from Windows. Only true while the game is in front."""
    vk = VIRTUAL_KEYS.get(name or "")
    return vk is not None and vk_down(vk)


def held_keys() -> set[int]:
    """Every key in the table that's currently held."""
    return {vk for vk in KEY_NAMES if vk_down(vk)}


def tap(vk: int) -> None:
    """Presses and releases a key, as if typed (goes to the focused window: the game)."""
    _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(vk, 0, 0x0002, 0)  # KEYEVENTF_KEYUP


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
    _user32.PostMessageW(ctypes.c_void_p(hwnd), WM_KEYDOWN if down else WM_KEYUP, vk, lparam)
    return True
