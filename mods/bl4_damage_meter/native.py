"""
Hearing every hit a player deals: a binding on the character's own "caused damage" delegate.

The game tells each character about every bit of damage it deals: `OakCharacter.DamageCauserData`
holds `OnCausedAnyDamage`, an inline multicast delegate `(DamagedActor, CausedDamageDetails)`
broadcast by the host's damage pipeline (guns, grenades, DoT ticks, melee, skills, shields...).
Nothing in the game binds it from script, so its invocation list is empty and broadcasting costs
nothing until we add an entry.

A dynamic delegate calls each bound function through `UObject::ProcessEvent`, which unrealsdk can
hook. So we bind it to a UFunction with exactly the same parameters,
`GbxInventoryAugmentScript.OnInventoryAugmentCausedDamageConditionMet(DamagedActor, Details)`, on
that class's default object, and hook that function (PRE, Block for the default object only). The
function is a script event with no native body and the default object has no script, so even an
unhooked call does nothing.

pyunrealsdk 1.10 can't edit `MulticastInlineDelegateProperty` values ("Unknown object type"), so we
append the binding ourselves: one FScriptDelegate (16 bytes: FWeakObjectPtr {index, serial} +
FName) at the end of the delegate's TArray, grown with the engine's allocator (unrealsdk's
`u_malloc` / `u_realloc` exports). The weak pointer's bytes come from the engine itself: we set a
reflected weak field (`AssetCompileData.Asset`) to the default object and copy what it wrote.

Everything is checked before the first write (`Binder.ready()`): the delegate and function
signatures match field for field, the weak pointer's index is the object's, the FName bytes read
back as the function's name, and each TArray header looks sane. On any mismatch nothing is bound.

Game thread only.
"""

from __future__ import annotations

import ctypes
from typing import Any

import unrealsdk
from unrealsdk import logging

AUGMENT_CLASS = "GbxInventoryAugmentScript"
FUNC_NAME = "OnInventoryAugmentCausedDamageConditionMet"
FUNC_PATH = f"/Script/GbxGame.{AUGMENT_CLASS}:{FUNC_NAME}"
CAUSER_PROP = "DamageCauserData"
DELEGATE_PROP = "OnCausedAnyDamage"
DELEGATE_SIG = "CauseAnyPipelineDamageDelegate__DelegateSignature"
ENTRY_SIZE = 16  # FScriptDelegate: FWeakObjectPtr (8) + FName (8)
NAME_OFFSET = 0x18  # UObjectBase::NamePrivate
INDEX_OFFSET = 0x0C  # UObjectBase::InternalIndex
MAX_ENTRIES = 256  # a delegate with more bindings than this isn't one we understand

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_k32.GetModuleHandleW.restype = ctypes.c_void_p
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
_k32.ReadProcessMemory.restype = ctypes.c_int
_process = _k32.GetCurrentProcess()


def safe_read(address: int, size: int) -> bytes | None:
    """Memory we haven't proven valid: ReadProcessMemory fails instead of crashing the game."""
    if not 0x10000 <= address < 0x7FFFFFFFFFFF or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not _k32.ReadProcessMemory(_process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)):
        return None
    return buffer.raw[: got.value] if got.value == size else None


def _allocator() -> tuple[Any, Any] | None:
    handle = _k32.GetModuleHandleW("unrealsdk.dll")
    if not handle:
        return None
    try:
        sdk = ctypes.CDLL("unrealsdk.dll", handle=handle)
        # Checked by disassembly (unrealsdk 3.2.0): u_malloc(size_t len), u_realloc(void*, size_t len)
        malloc = sdk._unrealsdk_export__u_malloc
        realloc = sdk._unrealsdk_export__u_realloc
    except (AttributeError, OSError):
        return None
    malloc.argtypes = [ctypes.c_size_t]
    malloc.restype = ctypes.c_void_p
    realloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    realloc.restype = ctypes.c_void_p
    return malloc, realloc


def _signature(fields: Any) -> list[tuple[str, int, str]]:
    out = []
    for prop in fields:
        target = getattr(prop, "Struct", None) or getattr(prop, "PropertyClass", None)
        out.append((prop.Class.Name, prop.Offset_Internal, target.Name if target is not None else ""))
    return out


class Binder:
    """The binding entry and the checks behind it. `ready()` once; then bind/unbind per character."""

    def __init__(self) -> None:
        self.error: str | None = None
        self.entry = b""
        self.cdo: Any = None
        self.cdo_addr = 0
        self.func: Any = None
        self._scratch: Any = None  # the weak field we let the engine fill (kept: it holds a weak ref)
        self._alloc = None
        # character class address -> (offset of the delegate in the character, ok) for classes checked
        self._offsets: dict[int, int | None] = {}

    def ready(self) -> bool:
        if self.entry:
            return True
        if self.error is not None:
            return False
        try:
            self._prepare()
        except Exception as ex:  # noqa: BLE001 - a game patch: say so, bind nothing
            self.error = f"{type(ex).__name__}: {ex}"
        if self.error is not None:
            logging.error(f"[Carnage Meter] can't listen for damage on this game version: {self.error}")
            return False
        return True

    def _prepare(self) -> None:
        cls = unrealsdk.find_class(AUGMENT_CLASS)
        func = cls._find(FUNC_NAME)
        cdo = cls.ClassDefaultObject
        oak = unrealsdk.find_class("OakCharacter")
        causer = oak._find_prop(CAUSER_PROP)
        delegate = causer.Struct._find_prop(DELEGATE_PROP)
        if delegate.Class.Name != "MulticastInlineDelegateProperty":
            self.error = f"{DELEGATE_PROP} is a {delegate.Class.Name}"
            return
        sig = delegate.Signature
        if sig.Name != DELEGATE_SIG:
            self.error = f"{DELEGATE_PROP} signature is {sig.Name}"
            return
        # The function must take exactly what the delegate passes
        theirs, ours = _signature(sig._properties()), _signature(func._properties())
        if theirs != ours:
            self.error = f"signatures differ: {theirs} vs {ours}"
            return
        alloc = _allocator()
        if alloc is None:
            self.error = "unrealsdk's allocator isn't exported"
            return
        scratch = unrealsdk.make_struct("AssetCompileData", Asset=cdo)
        weak = ctypes.string_at(scratch._get_address(), 8)
        if int.from_bytes(weak[:4], "little", signed=True) != cdo.InternalIndex or weak[4:] == b"\0\0\0\0":
            self.error = "the weak pointer the engine wrote isn't the default object's"
            return
        fname = ctypes.string_at(func._get_address() + NAME_OFFSET, 8)
        tag = unrealsdk.make_struct("GameplayTag")
        ctypes.memmove(tag._get_address(), fname, 8)
        if str(tag.TagName) != FUNC_NAME:
            self.error = f"name bytes read back as {tag.TagName!s}"
            return
        self.func, self.cdo, self.cdo_addr = func, cdo, cdo._get_address()
        self._scratch, self._alloc = scratch, alloc
        self.entry = weak + fname

    def _delegate_address(self, pawn: Any) -> int | None:
        cls = pawn.Class
        key = cls._get_address()
        if key not in self._offsets:
            offset = None
            try:
                causer = cls._find_prop(CAUSER_PROP)
                delegate = causer.Struct._find_prop(DELEGATE_PROP)
                if delegate.Class.Name == "MulticastInlineDelegateProperty" and delegate.Signature.Name == DELEGATE_SIG:
                    offset = causer.Offset_Internal + delegate.Offset_Internal
            except (ValueError, AttributeError):
                pass
            self._offsets[key] = offset
        offset = self._offsets[key]
        return None if offset is None else pawn._get_address() + offset

    def _header(self, base: int) -> tuple[int, int, int] | None:
        raw = safe_read(base, 16)
        if raw is None:
            return None
        data = int.from_bytes(raw[:8], "little")
        num = int.from_bytes(raw[8:12], "little", signed=True)
        cap = int.from_bytes(raw[12:16], "little", signed=True)
        if not (0 <= num <= cap <= MAX_ENTRIES) or (cap and not data) or (data and safe_read(data, ENTRY_SIZE * max(cap, 1)) is None):
            return None
        return data, num, cap

    def is_bound(self, pawn: Any) -> bool:
        base = self._delegate_address(pawn) if self.entry else None
        header = self._header(base) if base is not None else None
        if header is None:
            return False
        data, num, _ = header
        return any(ctypes.string_at(data + ENTRY_SIZE * i, ENTRY_SIZE) == self.entry for i in range(num))

    def bind(self, pawn: Any) -> bool:
        """Adds our entry to this character's delegate (once). False if it can't be done safely."""
        if not self.ready():
            return False
        base = self._delegate_address(pawn)
        header = self._header(base) if base is not None else None
        if header is None or base is None:
            return False
        data, num, cap = header
        for i in range(num):
            if ctypes.string_at(data + ENTRY_SIZE * i, ENTRY_SIZE) == self.entry:
                return True
        if num >= cap:
            malloc, realloc = self._alloc
            grown = realloc(data, ENTRY_SIZE * (num + 1)) if data else malloc(ENTRY_SIZE * (num + 1))
            if not grown:
                return False
            ctypes.c_uint64.from_address(base).value = grown
            ctypes.c_int32.from_address(base + 12).value = num + 1
            data = grown
        ctypes.memmove(data + ENTRY_SIZE * num, self.entry, ENTRY_SIZE)
        ctypes.c_int32.from_address(base + 8).value = num + 1
        return True

    def unbind(self, pawn: Any) -> bool:
        """Takes our entry out again (the memory stays with the delegate, which frees it)."""
        if not self.entry:
            return False
        base = self._delegate_address(pawn)
        header = self._header(base) if base is not None else None
        if header is None or base is None:
            return False
        data, num, _ = header
        for i in range(num):
            if ctypes.string_at(data + ENTRY_SIZE * i, ENTRY_SIZE) == self.entry:
                ctypes.memmove(data + ENTRY_SIZE * i, data + ENTRY_SIZE * (i + 1), ENTRY_SIZE * (num - i - 1))
                ctypes.c_int32.from_address(base + 8).value = num - 1
                return True
        return False


def struct_offsets() -> dict[str, int]:
    """
    Field offsets in the hook's parameters block (DamagedActor, then Details' fields), keyed by
    lowercased name (the game spells some in lower case: `damage`, `damageradius`).
    """
    func = unrealsdk.find_class(AUGMENT_CLASS)._find(FUNC_NAME)
    details = func._find_prop("Details")
    out = {"damagedactor": func._find_prop("DamagedActor").Offset_Internal}
    for prop in details.Struct._properties():
        out[prop.Name.lower()] = details.Offset_Internal + prop.Offset_Internal
    return out
