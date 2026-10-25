"""
The game's own ECHO menu (status menu) functions, called directly.

OakPlayerController.OpenStatusMenu is BlueprintCallable, but it first asks CanOpenStatusMenu,
and when that says no it only plays the "denied" feedback. Past that check it calls one internal
function on the controller's UIStatusMenuManager (`open(manager, &submenu, true)`), and that is
what we call. Its only check is the menu's state machine (state 0 = closed), so it can't open the
menu twice.

The machine code these come from (verified against the running game, 2026-10-23):

    CanOpenStatusMenu exec thunk:  ... E8 <can_open> 88 06            call; mov [rsi], al
    OpenStatusMenu implementation: E8 <can_open> 84 C0 74 ??           call; test al, al; je
                                   48 81 C6 <manager offset>           add rsi, imm32
                                   ... 41 B0 01 E8 <open>              mov r8b, 1; call
    open:                          ... E8 <state> 85 C0                call; test eax, eax

We find each one from the UFunction's native pointer and check every step (the manager offset
must match the reflected UIStatusMenuManager property, can_open must agree with the game's
CanOpenStatusMenu). If a game patch moves things, `find` fails and the mod does nothing rather
than call a wrong address.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import ctypes
import struct

import unrealsdk

UFUNCTION_FUNC_OFFSET = 0xD8  # UFunction::Func, checked against the module range below
SCAN = 0x100  # bytes of each function we search
MAX_STATE = 3  # highest value the menu state check returns (verified 2026-10-23)

_kernel32 = ctypes.windll.kernel32
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p

_CanOpen = ctypes.CFUNCTYPE(ctypes.c_uint8, ctypes.c_void_p)
_Open = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)
_State = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


class NotFound(Exception):
    pass


def _module_range() -> tuple[int, int]:
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    (size,) = struct.unpack("<I", ctypes.string_at(base + pe + 0x50, 4))  # OptionalHeader.SizeOfImage
    return base, base + size


def _native(name: str, lo: int, hi: int) -> int:
    func = unrealsdk.find_object("Function", f"/Script/OakGame.OakPlayerController:{name}")
    addr = ctypes.c_uint64.from_address(func._get_address() + UFUNCTION_FUNC_OFFSET).value
    if not lo <= addr < hi:
        raise NotFound(f"{name} has no native pointer at the expected place")
    return addr


def _calls(addr: int, follow: bytes, lo: int, hi: int) -> list[tuple[int, int]]:
    """(position, target) of each `E8 rel32` in the first SCAN bytes that's followed by `follow`."""
    code = ctypes.string_at(addr, SCAN)
    found = []
    for i in range(len(code) - 5 - len(follow)):
        if code[i] == 0xE8 and code[i + 5 : i + 5 + len(follow)] == follow:
            target = addr + i + 5 + struct.unpack_from("<i", code, i + 1)[0]
            if lo <= target < hi:
                found.append((i, target))
    return found


class StatusMenu:
    """The internal functions, once found."""

    def __init__(self, can_open: int, open_: int, state: int, manager_offset: int) -> None:
        self.can_open_addr = can_open
        self.open_addr = open_
        self.state_addr = state
        self.manager_offset = manager_offset
        self._can_open = _CanOpen(can_open)
        self._open = _Open(open_)
        self._state = _State(state)
        self._none_name = ctypes.c_uint64(0)  # FName None: the default sub-menu

    def can_open(self, pc: unrealsdk.unreal.UObject) -> bool:
        """The game's own answer (same as pc.CanOpenStatusMenu(), without the reflection call)."""
        return bool(self._can_open(pc._get_address()) & 1)

    def state(self, pc: unrealsdk.unreal.UObject) -> int:
        """0 when the menu is closed."""
        return self._state(pc._get_address() + self.manager_offset)

    def open(self, pc: unrealsdk.unreal.UObject) -> None:
        self._open(pc._get_address() + self.manager_offset, ctypes.byref(self._none_name), True)


def find(pc: unrealsdk.unreal.UObject) -> StatusMenu:
    lo, hi = _module_range()

    thunk = _native("CanOpenStatusMenu", lo, hi)
    calls = _calls(thunk, b"\x88\x06", lo, hi)
    if len(calls) != 1:
        raise NotFound("CanOpenStatusMenu's body")
    can_open = calls[0][1]

    # OpenStatusMenu's thunk reads its arguments with other calls; its body is the one that
    # starts by asking can_open
    thunk = _native("OpenStatusMenu", lo, hi)
    body = None
    for _, target in _calls(thunk, b"", lo, hi):
        asks = _calls(target, b"\x84\xc0\x74", lo, hi)
        if asks and asks[0][1] == can_open:
            body, ask_at = target, asks[0][0]
            break
    if body is None:
        raise NotFound("OpenStatusMenu's body")

    code = ctypes.string_at(body + ask_at, 0x40)
    at = code.find(b"\x48\x81\xc6", 5)
    if at < 0:
        raise NotFound("the status menu manager offset")
    (offset,) = struct.unpack_from("<i", code, at + 3)
    prop = next(p for p in pc.Class._properties() if p.Name == "UIStatusMenuManager")
    if offset != prop.Offset_Internal:
        raise NotFound(f"manager offset {offset:#x} != UIStatusMenuManager {prop.Offset_Internal:#x}")
    # The call right after `mov r8b, 1`: its target comes from that spot, not from the first E8
    # byte after the add (one can sit inside an immediate or a displacement)
    call = code.find(b"\x41\xb0\x01\xe8", at + 7)
    if call < 0 or call + 8 > len(code):
        raise NotFound("the internal open call")
    open_ = body + ask_at + call + 8 + struct.unpack_from("<i", code, call + 4)[0]
    if not lo <= open_ < hi:
        raise NotFound("the internal open call's target")

    # The state check is open's gate, so it's the first call in it
    states = _calls(open_, b"\x85\xc0", lo, hi)
    if not states or _calls(open_, b"", lo, hi)[0] != states[0]:
        raise NotFound("the menu state check")

    menu = StatusMenu(can_open, open_, states[0][1], offset)
    if menu.can_open(pc) != bool(pc.CanOpenStatusMenu()):
        raise NotFound("can_open disagrees with CanOpenStatusMenu")
    # 0 closed, 1-3 another menu or a per-player block (2 while the ECHO menu is up)
    if not 0 <= (state := menu.state(pc)) <= MAX_STATE:
        raise NotFound(f"the menu state check returned {state}")
    return menu
