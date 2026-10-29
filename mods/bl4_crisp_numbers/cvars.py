"""
Console variables' "set by" priority, read and written in the cvar object itself.

Every console variable remembers which level set it last (the top byte of its flags: 0x01
scalability, 0x02 game setting, ... 0x0E console). A lower level can't overwrite a higher one.
Our console writes (ExecuteConsoleCommand) are 0x0E, so after one the game's own writes are
ignored: that's how we stop the ECHO menu's r.ScreenPercentage 10. Putting the byte back lets the
game write again (the Options menu's DLSS mode, the mod switched off).

The cvar objects come from the game's own IConsoleManager::FindConsoleVariable, the way the game's
set-cvar-by-name helper does it (build 26007962, read 2026-10-28):

    5bcdd88: 48 8B 0D <rel32>      mov rcx, [GConsoleManager]
    5bcdd8f: 48 85 C9 74 32        test rcx, rcx; je
    5bcdd94: 48 8B 01 48 89 F2     mov rax, [rcx]; mov rdx, rsi
    5bcdd9a: 41 B0 01 FF 90 C8 00 00 00   mov r8b, 1; call [rax+0xC8]  (FindConsoleVariable)

Flags sit at +0x18 of the object (checked: 0x0E after a console write, 0x01 on an untouched
scalability cvar). If the bytes or any pointer don't check out, `find` fails and the mod steps
aside rather than guess.

No hooks here, so this file can be reloaded in a running game.
"""

from __future__ import annotations

import ctypes
import struct

HELPER_RVA = 0x5BCDD88  # the `mov rcx, [GConsoleManager]` in the set-cvar-by-name helper
HELPER_BYTES = bytes.fromhex("488b0d") + b"\0\0\0\0" + bytes.fromhex("4885c97432488b014889f241b001ff90c8000000")
FIND_SLOT = 0xC8  # IConsoleManager vtable: FindConsoleVariable(name, bTrackFrequentCalls)
FLAGS_OFFSET = 0x18
SET_BY_CONSOLE = 0x0E

_kernel32 = ctypes.windll.kernel32
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_Find = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_bool)


class NotFound(Exception):
    pass


def _image() -> tuple[int, int]:
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    (size,) = struct.unpack("<I", ctypes.string_at(base + pe + 0x50, 4))
    return base, base + size


class Cvar:
    def __init__(self, name: str, addr: int) -> None:
        self.name = name
        self._flags = ctypes.c_uint32.from_address(addr + FLAGS_OFFSET)

    @property
    def set_by(self) -> int:
        return self._flags.value >> 24

    @set_by.setter
    def set_by(self, level: int) -> None:
        self._flags.value = (self._flags.value & 0x00FFFFFF) | ((level & 0xFF) << 24)


class Console:
    def __init__(self) -> None:
        lo, hi = _image()
        at = lo + HELPER_RVA
        code = ctypes.string_at(at, len(HELPER_BYTES))
        if code[:3] != HELPER_BYTES[:3] or code[7:] != HELPER_BYTES[7:]:
            raise NotFound("the set-cvar helper (game patched?)")
        manager_ptr = at + 7 + struct.unpack_from("<i", code, 3)[0]
        self._manager = ctypes.c_uint64.from_address(manager_ptr).value
        if not self._manager:
            raise NotFound("GConsoleManager")
        vtable = ctypes.c_uint64.from_address(self._manager).value
        find = ctypes.c_uint64.from_address(vtable + FIND_SLOT).value
        if not (lo <= vtable < hi and lo <= find < hi):
            raise NotFound("the console manager's vtable")
        self._find = _Find(find)
        self._lo, self._hi = lo, hi

    def cvar(self, name: str) -> Cvar:
        addr = self._find(self._manager, name, False)
        if not addr or not self._lo <= ctypes.c_uint64.from_address(addr).value < self._hi:
            raise NotFound(name)
        cvar = Cvar(name, addr)
        if not 0 <= cvar.set_by <= SET_BY_CONSOLE:
            raise NotFound(f"{name}'s flags (set by {cvar.set_by:#x})")
        return cvar
