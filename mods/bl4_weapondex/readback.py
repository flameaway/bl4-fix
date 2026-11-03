"""
Reads a render target's pixels back to Python, cheaply (2026-11-03).

KismetRenderingLibrary's own read functions, called through a params block of ours that is kept
for the whole session (ProcessEvent on it, as bake.py does for the spawner), not through the SDK's
call. Through the SDK the read itself was ~3 ms, but letting go of its out array cost 42-50 ms for
512x512 floats (25 ms as 8-bit): the SDK destroys the array's 262,144 structs one by one. In a kept
block nothing is destroyed: the engine reads into the same array on the next call (it reuses the
same memory: the same buffer every read, checked). Byte for byte the SDK call's pixels, at
3.2-4.1 ms a 512x512 read (12 boss passes, 2026-11-03).

The first read after a capture still waits for the GPU to finish that capture (~20 ms).

Game thread only. The blocks hold ~4 MB (floats) and ~1 MB (8-bit) for the session.
"""

from __future__ import annotations

import ctypes
import struct
from typing import Any

import unrealsdk
from unrealsdk.unreal import WrappedStruct

# UObject::ProcessEvent's vtable slot (this build: the address unrealsdk logs as "Detoured ProcessEvent")
PROCESS_EVENT_SLOT = 73
_PROCESS_EVENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

# function name -> (its out array's name, bytes a pixel)
_READS = {
    "ReadRenderTargetRaw": ("OutLinearSamples", 16),  # FLinearColor: RGBA floats
    "ReadRenderTarget": ("OutSamples", 4),  # FColor: BGRA bytes
}

# function name -> [function, params block, out array's address, bytes a pixel]. Kept across
# a hot reload: a new block would leave the old one's array to the SDK's slow destroy
_BLOCKS: dict[str, list[Any]] = globals().get("_BLOCKS") or {}


def _block(name: str) -> list[Any]:
    block = _BLOCKS.get(name)
    if block is None:
        library = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        func = getattr(library, name).func
        out_name, size = _READS[name]
        out = next(p for p in func._properties() if p.Name == out_name)  # noqa: SLF001
        params = WrappedStruct(func)
        block = _BLOCKS[name] = [func, params, params._get_address() + out.Offset_Internal, size]  # noqa: SLF001
    return block


def _read(name: str, context: Any, target: Any, pixels: int) -> bytes | None:
    func, params, out, size = _block(name)
    library = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    params.WorldContextObject = context
    params.TextureRenderTarget = target
    params.bNormalize = False
    params.ReturnValue = False
    this = library._get_address()  # noqa: SLF001
    vtable = ctypes.c_uint64.from_address(this).value
    _PROCESS_EVENT(ctypes.c_uint64.from_address(vtable + 8 * PROCESS_EVENT_SLOT).value)(this, func._get_address(), params._get_address())  # noqa: SLF001
    params.WorldContextObject = params.TextureRenderTarget = None  # no stale pointers left in the block
    data, count = struct.unpack("<Qi", ctypes.string_at(out, 12))
    if not params.ReturnValue or not data or count != pixels:
        return None
    return ctypes.string_at(data, count * size)


def floats(context: Any, target: Any, pixels: int) -> bytes | None:
    """The target's pixels as RGBA floats (ReadRenderTargetRaw), or None if it couldn't be read."""
    return _read("ReadRenderTargetRaw", context, target, pixels)


def colors(context: Any, target: Any, pixels: int) -> bytes | None:
    """The target's pixels as 8-bit BGRA (ReadRenderTarget), or None if it couldn't be read."""
    return _read("ReadRenderTarget", context, target, pixels)
