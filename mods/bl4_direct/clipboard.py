"""
The text on the Windows clipboard, for the F6 page's Paste buttons (plain ctypes, any thread).

Typing itself goes through the BL4 UI Link's TextBox (the game's own text input, 2026-10-30); this
module was keyboard.py, which also had the low-level keyboard hook typing used before.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

CF_UNICODETEXT = 13

# Own copies: ctypes.windll's function objects are shared with every mod in the process, and
# giving them argtypes would change them for everyone
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


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
