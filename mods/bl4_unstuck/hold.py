"""
Gearbox's arrival hold: gravity 0 and move input ignored, which sometimes never lets go.

Seen 2026-10-28 (two copies of the game on one account, the host teleporting its own character):
the character hangs in the air (`CharacterMovement.GravityScale` 0, `MovementMode` Falling, no
velocity) and WASD does nothing. Gravity is a reflected property. The move lock isn't:
`OakPlayerController` overrides `Controller::IsMoveInputIgnored` (vtable slot read from the
UFunction's native thunk), and its version also answers True while a byte of the character's
movement component is set (+0x11F0 in build 26007962). `ResetIgnoreMoveInput`,
`SetIgnoreMoveInput(False)` and `ResetIgnoreInputFlags` don't touch that byte. Writing 0 to it
released the character, and it stayed released.

The offsets are read out of the override's own code, not hard-coded, and only used when every
check passes: the thunk and the override match their patterns exactly, the override lies inside
the exe, and the pawn / component pointers the code reads really are the controller's pawn and
that pawn's movement component. Otherwise `flag_offset()` is None and only gravity is restored.
"""

from __future__ import annotations

import ctypes
import re
from typing import Any

from unrealsdk import logging
from unrealsdk.unreal import UObject

# Controller::execIsMoveInputIgnored: ... mov rax,[rcx]; call [rax+SLOT]; mov [rsi],al
_THUNK = re.compile(
    rb"\x56\x48\x83\xEC\x20\x4C\x89\xC6\x48\x8B\x42\x20\x48\x83\xF8\x01\x48\x83\xD8\xFF"
    rb"\x48\x89\x42\x20\x48\x8B\x01\xFF\x90(....)\x88\x06",
    re.S,
)
# The Oak override: engine counter, a controller byte, then (pawn is an OakCharacter) two bytes
# of the movement component the pawn points at
_OVERRIDE = re.compile(
    rb"\x56\x57\x48\x81\xEC\x88\x00\x00\x00\x48\x8B\x05....\x48\x31\xE0\x48\x89\x84\x24\x80\x00\x00\x00"
    rb"\x40\xB6\x01\x80\xB9(....)\x00\x75.\x80\xB9(....)\x00\x75."
    rb"\x48\x8B\xB9(....)\x48\x85\xFF\x74.\x48\x8B\x05....\x48\x85\xC0\x74."
    rb"\x48\x8B\x4F\x10\x48\x63\x50\x38\x3B\x51\x38\x7F.\x48\x83\xC0\x30\x48\x8B\x49\x30\x48\x39\x04\xD1\x75."
    rb"\x48\x8B\x87(....)\x48\x85\xC0\x74.\x80\xB8(....)\x00\x75.\x80\xB8(....)\x00\x75.\x31\xF6",
    re.S,
)
_UFUNC_FUNC = 0xD8  # UFunction::Func

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p


class _Found:
    done = False
    pawn_off = 0  # controller -> pawn pointer
    comp_off = 0  # pawn -> movement component pointer
    flag = 0  # the hold byte in the movement component; 0 = unknown


def _u64(address: int) -> int:
    return ctypes.c_uint64.from_address(address).value


def _image() -> tuple[int, int]:
    base = _kernel32.GetModuleHandleW(None) or 0
    nt = base + ctypes.c_uint32.from_address(base + 0x3C).value
    return base, base + ctypes.c_uint32.from_address(nt + 0x50).value  # OptionalHeader.SizeOfImage


def _find(pc: UObject) -> None:
    """Reads the offsets out of the game's code. Game thread, with a character (once a session)."""
    pawn = pc.Pawn
    move = getattr(pawn, "CharacterMovement", None) if pawn is not None else None
    if move is None:
        return  # try again with a character
    _Found.done = True
    lo, hi = _image()
    func = pc.Class._find("IsMoveInputIgnored")
    thunk = _u64(func._get_address() + _UFUNC_FUNC)
    if not lo <= thunk < hi - 64:
        raise ValueError(f"IsMoveInputIgnored thunk {thunk:#x} outside the exe")
    match = _THUNK.match(ctypes.string_at(thunk, 64))
    if match is None:
        raise ValueError("IsMoveInputIgnored thunk doesn't match")
    slot = int.from_bytes(match.group(1), "little")
    override = _u64(_u64(pc._get_address()) + slot)
    if not lo <= override < hi - 256:
        raise ValueError(f"override {override:#x} outside the exe")
    match = _OVERRIDE.match(ctypes.string_at(override, 256))
    if match is None:
        raise ValueError("IsMoveInputIgnored override doesn't match")
    _, _, pawn_off, comp_off, flag, _ = (int.from_bytes(g, "little") for g in match.groups())
    if _u64(pc._get_address() + pawn_off) != pawn._get_address():
        raise ValueError(f"controller+{pawn_off:#x} isn't the pawn")
    if _u64(pawn._get_address() + comp_off) != move._get_address():
        raise ValueError(f"pawn+{comp_off:#x} isn't its movement component")
    if not 0 < flag < 0x4000:
        raise ValueError(f"hold flag offset {flag:#x} out of range")
    _Found.pawn_off, _Found.comp_off, _Found.flag = pawn_off, comp_off, flag


def flag_offset(pc: UObject) -> int | None:
    """The hold byte's offset in the movement component, or None if it couldn't be confirmed."""
    if not _Found.done:
        try:
            _find(pc)
        except Exception as ex:  # noqa: BLE001 - a game patch: gravity only
            logging.warning(f"[Unstuck] can't find the movement hold in this game build ({ex}): gravity only")
    return _Found.flag or None


def held(pawn: UObject) -> bool:
    """Whether the hold byte is set on this character (False if unknown)."""
    move = getattr(pawn, "CharacterMovement", None)
    if not _Found.flag or move is None:
        return False
    return ctypes.c_uint8.from_address(move._get_address() + _Found.flag).value != 0


def default_gravity(pawn: UObject) -> float:
    try:
        value = float(pawn.Class.ClassDefaultObject.CharacterMovement.GravityScale)
    except Exception:  # noqa: BLE001
        value = 1.0
    return value if value > 0 else 1.0


def release(pc: UObject, pawn: UObject) -> dict[str, Any]:
    """Lets the character go: hold byte off, gravity back. Returns what it changed."""
    changed: dict[str, Any] = {}
    move = pawn.CharacterMovement
    if flag_offset(pc) is not None and held(pawn):
        ctypes.c_uint8.from_address(move._get_address() + _Found.flag).value = 0
        changed["hold"] = True
    if move.GravityScale <= 0:
        move.GravityScale = default_gravity(pawn)
        changed["gravity"] = move.GravityScale
    return changed
