"""
Typing into the BL4 Direct page's text fields (a friend's name, their IP address).

No key events reach the game's pages, so the text is read from Windows. The page sits on top of
the game's pause menu, which reacts to keys (Esc closes it, Tab and others move around), so while
you type the keys mustn't reach the game at all. A low-level keyboard hook (WH_KEYBOARD_LL) does
both: it sees each key before the game does, turns it into a character with the current keyboard
layout (shift and caps lock included), and swallows it. Same technique as Build Presets' naming
(bl4_builds/keyboard.py), plus Tab (next field) and Ctrl+V (paste).

The hook only exists while a field is being typed in, runs on its own thread (low-level hooks need
a message loop), and only swallows keys while the game window is in front. A key that was already
held when typing started is let through (so its release reaches the game too).

Plain ctypes, no game access: the page link (page.py) starts and stops it and reads the text.
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from collections.abc import Callable
from ctypes import wintypes

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_QUIT = 0x12
WM_USER = 0x400
PM_NOREMOVE = 0
VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_ESCAPE, VK_DELETE = (
    0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x14, 0x1B, 0x2E
)  # fmt: skip
VK_V = 0x56
VK_LWIN, VK_RWIN = 0x5B, 0x5C
CF_UNICODETEXT = 13
# Left/right modifiers as the hook reports them -> the generic key
MODIFIERS = {0xA0: VK_SHIFT, 0xA1: VK_SHIFT, 0xA2: VK_CONTROL, 0xA3: VK_CONTROL, 0xA4: VK_MENU, 0xA5: VK_MENU}
CTRLS = {0xA2, 0xA3}
# Let these through: Windows shortcuts must keep working, caps lock must toggle, and Alt+Tab
# must still switch windows while typing
ALTS = {0xA4, 0xA5}
PASS = {VK_LWIN, VK_RWIN, VK_CAPITAL, *ALTS}
# Stop typing if nothing was pressed for this long (say, the player walked away)
IDLE_S = 120.0


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
# Own copies: ctypes.windll's function objects are shared with every mod in the process, and
# giving them argtypes would change them for everyone
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetKeyState.restype = ctypes.c_short
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetKeyboardLayout.restype = ctypes.c_void_p
_user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
_user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                wintypes.UINT, ctypes.c_void_p]  # fmt: skip
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _game_in_front() -> bool:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def clipboard_text(max_len: int = 200) -> str:
    """The text on the Windows clipboard (first max_len characters), or ""."""
    if not _user32.OpenClipboard(None):
        return ""
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        locked = _kernel32.GlobalLock(handle)
        if not locked:
            return ""
        try:
            return ctypes.wstring_at(locked)[:max_len]
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _char(vk: int, scan: int, mods: set[int]) -> str:
    """
    The character this key types with these modifiers held (shift / AltGr) and caps lock, or "".
    The modifiers come from the hook itself: it swallows them, so Windows' key state never sees
    them down.
    """
    state = (ctypes.c_ubyte * 256)()
    for key in mods:
        state[key] = 0x80
        state[MODIFIERS[key]] = 0x80
    if _user32.GetKeyState(VK_CAPITAL) & 1:
        state[VK_CAPITAL] = 0x01
    if state[VK_CONTROL] and not state[VK_MENU]:
        return ""  # Ctrl+key: a shortcut, not text
    out = ctypes.create_unicode_buffer(8)
    # The game window's layout (this runs on the hook's own thread). Flag 4: don't change the
    # keyboard's dead-key state (Windows 10 1607+)
    layout = _user32.GetKeyboardLayout(_user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None))
    got = _user32.ToUnicodeEx(vk, scan, state, out, len(out), 4, layout)
    text = out.value[:got] if got > 0 else ""
    return text if text.isprintable() else ""


class Typing:
    """
    One field being typed in. Read .text, .done (None while typing; else "enter", "tab" or "esc")
    and .failed (Windows refused the hook). Never waits on its thread: the hook starts a moment
    after this returns, and stop() only asks the thread to end (it unhooks itself).

    allowed: which characters the field takes (typed or pasted); others are dropped.
    """

    def __init__(self, text: str, max_len: int, allowed: Callable[[str], bool] = str.isprintable) -> None:
        self.text = text
        self.max_len = max_len
        self.allowed = allowed
        self.done: str | None = None
        self.failed = False
        self.last_key = time.monotonic()
        self._events: deque[tuple[int, str, bool]] = deque()
        self._swallowed: set[int] = set()
        self._mods: set[int] = set()
        self._held_at_start = {vk for vk in range(1, 256) if _user32.GetAsyncKeyState(vk) & 0x8000}
        self._thread_id = 0
        self._hook = None
        self._proc = _HOOKPROC(self._on_key)
        self._thread = threading.Thread(target=self._run, name="bl4_direct typing", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        msg = wintypes.MSG()
        # Make this thread's message queue first: stop() posts WM_QUIT once _thread_id is set, and
        # a post to a thread without a queue is lost
        _user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)
        self._thread_id = _kernel32.GetCurrentThreadId()
        self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            self.failed = True
            return
        try:
            # stop() came before _thread_id was set (so nothing was posted): end now
            if self.done is not None:
                return
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        # Keep this short: Windows drops hooks that take too long
        if code == HC_ACTION and self.done is None:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if vk in MODIFIERS:
                (self._mods.add if down else self._mods.discard)(vk)
            alt_tab = vk == VK_TAB and bool(self._mods & ALTS)
            if vk not in PASS and not alt_tab and _game_in_front():
                if down and vk not in self._held_at_start:
                    self._swallowed.add(vk)
                    if vk not in MODIFIERS:
                        ctrl = bool(self._mods & CTRLS) and not self._mods & ALTS
                        self._events.append((vk, _char(vk, int(info.scanCode), self._mods), ctrl))
                    return 1
                if not down and vk in self._swallowed:
                    self._swallowed.discard(vk)
                    return 1
                if not down:
                    self._held_at_start.discard(vk)
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _add(self, chars: str) -> None:
        for char in chars:
            if len(self.text) >= self.max_len:
                return
            if self.allowed(char):
                self.text += char

    def poll(self) -> bool:
        """Applies the keys typed since the last call (game thread). True if anything changed."""
        changed = False
        while self._events and self.done is None:
            vk, char, ctrl = self._events.popleft()
            self.last_key = time.monotonic()
            changed = True
            if vk == VK_RETURN:
                self.done = "enter"
            elif vk == VK_TAB:
                self.done = "tab"
            elif vk == VK_ESCAPE:
                self.done = "esc"
            elif vk == VK_BACK:
                self.text = self.text[:-1]
            elif vk == VK_DELETE:
                self.text = ""
            elif ctrl and vk == VK_V:
                self._add(clipboard_text().strip())
            elif char:
                self._add(char)
        if self.done is None and time.monotonic() - self.last_key > IDLE_S:
            self.done = "esc"
            changed = True
        return changed

    def stop(self) -> None:
        """Ends the typing without waiting: the thread unhooks and ends on its own."""
        if self.done is None:
            self.done = "esc"
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
