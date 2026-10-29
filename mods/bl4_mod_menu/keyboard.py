"""
Typing into the mods screen, and Esc on the title screen.

No key events reach the game's pages, so keys are read from Windows with a low-level keyboard
hook (WH_KEYBOARD_LL, Build Presets' keyboard.py does the same for build names): it sees each key
before the game does and can swallow it, so the game's menus don't react while you type.

- Typing: every key (but Windows/Alt shortcuts and caps lock) becomes text with the current
  keyboard layout. Enter submits, Esc stops, Backspace/Delete edit, Ctrl+V pastes.
- EscGrab: only Esc, counted. On the title screen Esc would open the game's "quit?" dialog under
  our screen; with this it goes back a page instead.

A hook only exists while needed, runs on its own thread (low-level hooks need a message loop), only
swallows keys while the game window is in front, and lets through keys already held when it
started (so their release reaches the game too). Plain ctypes, no game access: web.py starts and
stops them on the game thread and reads what they saw.
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
# Own DLL objects: shared windll prototypes other code uses stay as they are
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
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _game_in_front() -> bool:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


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
    # Flag 4: don't change the keyboard's dead-key state (Windows 10 1607+). The game window's
    # layout (this runs on the hook's own thread)
    layout = _user32.GetKeyboardLayout(_user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None))
    got = _user32.ToUnicodeEx(vk, scan, state, out, len(out), 4, layout)
    text = out.value[:got] if got > 0 else ""
    return text if text.isprintable() else ""


class _Hook:
    """
    A low-level keyboard hook on its own thread. Subclasses say which keys to swallow and get
    them as events: (vk, character, ctrl held). Never waits on its thread: the hook starts a
    moment after this returns, and stop() only asks the thread to end (it unhooks itself).
    """

    name = "bl4_mod_menu keys"

    def __init__(self) -> None:
        self.stopped = False
        self.failed = False  # Windows refused the hook
        self._events: deque[tuple[int, str, bool]] = deque()
        self._swallowed: set[int] = set()
        self._mods: set[int] = set()
        self._held_at_start: set[int] | None = None
        self._thread_id = 0
        self._hook = None
        self._proc = _HOOKPROC(self._on_key)
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def wants(self, vk: int) -> bool:
        return True

    def _run(self) -> None:
        # Read here, not on the game thread: key reads can be slow (a streamed desktop)
        self._held_at_start = {vk for vk in range(1, 256) if _user32.GetAsyncKeyState(vk) & 0x8000}
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
            if self.stopped:
                return
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        # Keep this short: Windows drops hooks that take too long
        held = self._held_at_start
        if code == HC_ACTION and not self.stopped and held is not None:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if vk in MODIFIERS:
                (self._mods.add if down else self._mods.discard)(vk)
            alt_tab = vk == VK_TAB and bool(self._mods & ALTS)
            if self.wants(vk) and vk not in PASS and not alt_tab and _game_in_front():
                if down and vk not in held:
                    self._swallowed.add(vk)
                    if vk not in MODIFIERS:
                        self._events.append((vk, _char(vk, int(info.scanCode), self._mods), bool(self._mods & CTRLS)))
                    return 1
                if not down and vk in self._swallowed:
                    self._swallowed.discard(vk)
                    return 1
                if not down:
                    held.discard(vk)
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def stop(self) -> None:
        """Ends the hook without waiting: the thread unhooks and ends on its own."""
        self.stopped = True
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


class Typing(_Hook):
    """
    A line being typed. Read .text and .done (None while typing, True Enter, False Esc or idle).
    paste: reads the clipboard (called on a short-lived thread, never the game thread).
    """

    name = "bl4_mod_menu typing"

    def __init__(self, text: str, max_len: int, paste: Callable[[], str] | None = None) -> None:
        self.text = text
        self.max_len = max_len
        self.done: bool | None = None
        self.last_key = time.monotonic()
        self._paste = paste
        self._pasted: deque[str] = deque()
        super().__init__()

    def _read_clipboard(self) -> None:
        try:
            text = self._paste() if self._paste is not None else ""
        except Exception:  # noqa: BLE001 - an unreadable clipboard pastes nothing
            text = ""
        self._pasted.append(text)

    def poll(self) -> bool:
        """Applies the keys typed since the last call (game thread). True if anything changed."""
        changed = False
        while self._pasted and self.done is None:
            # One line, no spaces: these are links
            text = "".join((self._pasted.popleft().strip().splitlines() or [""])[0].split())
            self.text = (self.text + text)[: self.max_len]
            changed = True
        while self._events and self.done is None:
            vk, char, ctrl = self._events.popleft()
            self.last_key = time.monotonic()
            changed = True
            if vk == VK_RETURN:
                self.done = True
            elif vk == VK_ESCAPE:
                self.done = False
            elif vk == VK_BACK:
                self.text = self.text[:-1]
            elif vk == VK_DELETE:
                self.text = ""
            elif ctrl and vk == VK_V:
                threading.Thread(target=self._read_clipboard, name="bl4_mod_menu paste", daemon=True).start()
            elif vk != VK_TAB and char and len(self.text) < self.max_len:
                self.text += char
        if self.done is None and time.monotonic() - self.last_key > IDLE_S:
            self.done = False
            changed = True
        if self.done is not None:
            self.stop()
        return changed


class EscGrab(_Hook):
    """Swallows Esc; .take() says how many presses came since the last call (game thread)."""

    name = "bl4_mod_menu esc"

    def wants(self, vk: int) -> bool:
        return vk == VK_ESCAPE

    def take(self) -> int:
        count = 0
        while self._events:
            self._events.popleft()
            count += 1
        return count
