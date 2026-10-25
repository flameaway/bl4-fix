"""
DEV ONLY - lets two copies of BL4 on ONE Steam/SHiFT account co-op fast travel, for local testing.
Not part of the mod: real friends have their own accounts and never hit this.

Why it's needed (docs/fast-travel-investigation.md): the host's online layer keeps one "local user"
slot that points at the host's own PlayerController. When a client signed into the SAME account
joins, the game re-binds that slot to the client's controller (verified: the slot flips the moment
the client logs in). From then on the host can't find its own player, its arrival readiness check
never passes, and the host sits in the fast-travel portal forever.

This re-points the slot at the host's controller whenever it flips. Run it on the HOST through the
bridge only AFTER the client has fully loaded in (gear in hand), right before a fast-travel test,
and remove the hook afterwards. Use an absolute path: the game's working directory isn't the repo.

DON'T run it before or while the client joins (2026-10-25): it re-pointed the slot the moment the
client logged in, and the client then loaded with nothing equipped (backpack items flagged as
equipped, every `EquippedInventorySlots` entry empty, on both sides). The same join without the
guard gave full gear. A host map reload with the guard still hooked also came up gearless.

    python -I tools/bridge.py A "exec(open(r'C:/path/to/bl4-direct/dev/re/same_account_guard.py').read())"

Offsets are for the game build of 2026-10-22 (Borderlands4.exe 647,338,264 bytes); re-derive them
with the tools in this folder after a game update.
"""
import ctypes
import struct
import time

import unrealsdk
from mods_base import get_pc
from unrealsdk import hooks

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetModuleHandleW.restype = ctypes.c_void_p
_k32.GetModuleHandleW.argtypes = (ctypes.c_wchar_p,)
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t))
_BASE = _k32.GetModuleHandleW(None)
_VP = ctypes.c_void_p

RVA_GET_GAME_INSTANCE = 0x47210F0  # PlayerController -> GameInstance
RVA_ONLINE_USERS_OWNER = 0xF6F32  # GameInstance -> object whose +0x68 is the online user subsystem
RVA_UOBJECT_CHUNKS = 0xC9647C8  # global: GUObjectArray chunk table (for weak-pointer serials)
USERS_ARRAY, USER_PC_WEAK = 0x8, 0x24  # subsystem+8 = TArray<User*>; User+0x24 = {index, serial}


def _read(addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    ok = _k32.ReadProcessMemory(_k32.GetCurrentProcess(), _VP(addr), buf, size, ctypes.byref(got))
    return buf.raw[:got.value] if ok else b""


def _q(addr):
    return struct.unpack("<Q", _read(addr, 8))[0]


def _user0_slot():
    pc = get_pc()
    gi = ctypes.CFUNCTYPE(_VP, _VP)(_BASE + RVA_GET_GAME_INSTANCE)(pc._get_address())
    owner = ctypes.CFUNCTYPE(_VP, _VP)(_BASE + RVA_ONLINE_USERS_OWNER)(gi)
    users = _q(owner + 0x68)
    return _q(_q(users + USERS_ARRAY)) + USER_PC_WEAK


def _weak_of(obj):
    idx = obj.InternalIndex
    item = _q(_q(_BASE + RVA_UOBJECT_CHUNKS) + (idx >> 16) * 8) + (idx & 0xFFFF) * 0x18
    if _q(item) != obj._get_address():
        raise RuntimeError("object table layout changed - re-derive RVA_UOBJECT_CHUNKS")
    return struct.pack("<ii", idx, struct.unpack("<i", _read(item + 0x10, 4))[0])


GUARD_LOG = globals().setdefault("GUARD_LOG", [])
_state = {"slot": None, "t": 0.0}


def _tick(*_):
    now = time.monotonic()
    if now - _state["t"] < 0.1:
        return
    _state["t"] = now
    try:
        pc = get_pc()
        if pc is None or not pc.IsLocalController():
            return
        slot = _user0_slot()
        want = _weak_of(pc)
        if _read(slot, 8) != want:
            ctypes.memmove(slot, want, 8)
            GUARD_LOG.append((time.strftime("%H:%M:%S"), "re-pointed local user 0 to", pc.Name))
    except Exception as ex:  # noqa: BLE001 - dev helper, never break the game tick
        GUARD_LOG.append((time.strftime("%H:%M:%S"), "error", repr(ex)[:120]))


_HOOK = "/Script/Engine.CameraModifier:BlueprintModifyPostProcess"
hooks.remove_hook(_HOOK, hooks.Type.POST, "same_account_guard")
hooks.add_hook(_HOOK, hooks.Type.POST, "same_account_guard", _tick)
print("same-account guard on; check GUARD_LOG for re-points")
