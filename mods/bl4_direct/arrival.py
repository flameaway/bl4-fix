"""
Making sure connected friends finish arriving after a fast travel.

When the host fast-travels, the game holds every player in the travel portal until each player's
controller has its native "client ready" flag set. The flag is set by the game once a per-player
readiness check (tied to the player's online ID) completes. We've seen that check never complete
for a directly-connected friend: the host's own controller was ready, the friend's wasn't, and
both players sat in the portal indefinitely (verified, two instances on one account).

Setting the flag after the fact doesn't release anyone (verified), so like the BL4 split-screen
mod (TitanNav/bl4-2player-splitscreen), whose analysis this follows, we call the game's own setter
for that controller when the travel notice goes out.

The setter isn't a UFunction, so it's found by code pattern in the running exe. It's only used
when the pattern matches exactly once AND the code that checks the flag matches exactly once with
the same flag offset; otherwise this does nothing and the game behaves as normal.
"""

from __future__ import annotations

import ctypes
import re
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

# push rsi; push rdi; push rbx; sub rsp,30h; mov rax,[rip+?]; xor rax,rsp; mov [rsp+28h],rax;
# cmp byte [rcx+FLAG],0; jne ?; mov rsi,rcx; mov byte [rcx+FLAG],1; call ?
_SETTER = re.compile(
    rb"\x56\x57\x53\x48\x83\xEC\x30\x48\x8B\x05....\x48\x31\xE0\x48\x89\x44\x24\x28"
    rb"\x80\xB9(....)\x00\x0F\x85....\x48\x89\xCE\xC6\x81(....)\x01\xE8",
    re.S,
)
# cmp byte [rsi+FLAG],0; jne ?; mov rax,[rsi+PLAYERSTATE]; test rax,rax; je ?; test byte [rax+?],8
_CHECK = re.compile(rb"\x80\xBE(....)\x00\x75.\x48\x8B\x86....\x48\x85\xC0\x74.\xF6\x80....\x08", re.S)

_EXECUTABLE_SECTION = 0x20000000
# Small chunks with a pause between: `re` holds the GIL for a whole finditer, and the game thread's
# hooks wait for it (16 MB chunks stalled every hook 4-27 ms; the scan takes ~0.25 s in all). The
# pause must really sleep: sleep(0) takes the GIL straight back and starves the game thread.
_CHUNK = 1 << 20
_OVERLAP = 0x100

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.ReadProcessMemory.argtypes = (
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
)
_kernel32.ReadProcessMemory.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


@dataclass(frozen=True)
class ReadySetter:
    address: int
    flag_offset: int


def _read(address: int, size: int) -> bytes | None:
    """Reads our own memory without risking an access violation on unreadable pages."""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(
        _kernel32.GetCurrentProcess(), ctypes.c_void_p(address), buf, size, ctypes.byref(got),
    )
    return buf.raw[: got.value] if ok else None


def _code_section(base: int) -> tuple[int, int] | None:
    header = _read(base, 0x1000) or b""
    if len(header) < 0x200:
        return None
    pe = struct.unpack_from("<I", header, 0x3C)[0]
    count = struct.unpack_from("<H", header, pe + 6)[0]
    optional_size = struct.unpack_from("<H", header, pe + 20)[0]
    for i in range(count):
        entry = pe + 24 + optional_size + i * 40
        virtual_size, virtual_address = struct.unpack_from("<II", header, entry + 8)
        if struct.unpack_from("<I", header, entry + 36)[0] & _EXECUTABLE_SECTION:
            return virtual_address, virtual_size
    return None


def _locate() -> ReadySetter | None:
    base = int(_kernel32.GetModuleHandleW(None))
    section = _code_section(base)
    if section is None:
        return None
    rva, size = section
    setters: list[tuple[int, int]] = []
    checks: list[int] = []
    pos = 0
    while pos < size:
        data = _read(base + rva + pos, min(_CHUNK + _OVERLAP, size - pos))
        if data:
            limit = min(_CHUNK, len(data))  # matches starting in the overlap belong to the next chunk
            for m in _SETTER.finditer(data):
                if m.start() < limit and m.group(1) == m.group(2):
                    setters.append((base + rva + pos + m.start(), struct.unpack("<I", m.group(1))[0]))
            for m in _CHECK.finditer(data):
                if m.start() < limit:
                    checks.append(struct.unpack("<I", m.group(1))[0])
        pos += _CHUNK
        time.sleep(0.001)
    if len(setters) == 1 and len(checks) == 1 and setters[0][1] == checks[0]:
        return ReadySetter(*setters[0])
    return None


_setter: ReadySetter | None = None
_scan: threading.Thread | None = None
scan_error = ""  # why the scan failed, if it raised


def _scan_worker() -> None:
    # Only ReadProcessMemory and regexes here, no unreal calls: safe off the game thread
    global _setter, scan_error
    try:
        _setter = _locate()
    except Exception as ex:  # noqa: BLE001 - a failed scan just means no fix
        scan_error = repr(ex)


def start_scan() -> None:
    """Starts the once-per-session scan of the exe (several seconds) on a background thread."""
    global _scan
    if _scan is None:
        _scan = threading.Thread(target=_scan_worker, name="bl4_direct.arrival_scan", daemon=True)
        _scan.start()


def scan_done() -> bool:
    return _scan is not None and not _scan.is_alive()


def setter() -> ReadySetter | None:
    """The setter, waiting for the scan if it's still running (only a travel right after enable)."""
    start_scan()
    assert _scan is not None
    _scan.join()
    return _setter


def is_ready(controller_address: int) -> bool | None:
    found = setter()
    if found is None:
        return None
    value = _read(controller_address + found.flag_offset, 1)
    return None if not value else value != b"\x00"


def mark_ready(controller_address: int) -> bool:
    """Calls the game's own setter for this controller. True if the flag reads set afterwards."""
    found = setter()
    if found is None:
        return False
    ctypes.CFUNCTYPE(None, ctypes.c_void_p)(found.address)(ctypes.c_void_p(controller_address))
    return bool(is_ready(controller_address))
