# Trace who makes WeaponStatsContainers: swap the type singleton's vtable for a copy whose slot 3
# (MakeShared, RVA 0xAE7392) records the native call stack, then forwards. One 8-byte store in .data;
# undone by wdx_trace_stop() or automatically after LIMIT captures / TIMEOUT s.
import ctypes, struct, threading, time
from ctypes import c_void_p, c_uint32, c_uint64

_k = ctypes.WinDLL("kernel32")
_k.GetModuleHandleW.restype = c_void_p
_nt = ctypes.WinDLL("ntdll")
_nt.RtlCaptureStackBackTrace.restype = ctypes.c_ushort
_nt.RtlCaptureStackBackTrace.argtypes = [c_uint32, c_uint32, c_void_p, c_void_p]

BASE = _k.GetModuleHandleW(None)
SINGLETON, VTABLE, MAKER, SLOT = 0xCB94E60, 0xB4A7930, 0xAE7392, 3
SIZE = 652288000
LIMIT, TIMEOUT = 40, 90.0


class WdxTrace:
    stacks: list = []
    active = False
    started = 0.0
    copy = None
    cb = None
    orig = None


def _frames():
    buf = (c_void_p * 62)()
    n = _nt.RtlCaptureStackBackTrace(0, 62, buf, None)
    return [buf[i] - BASE for i in range(n) if buf[i] and 0 <= buf[i] - BASE < SIZE]


def _make(this, out):
    try:
        if len(WdxTrace.stacks) < LIMIT:
            WdxTrace.stacks.append((time.monotonic(), threading.get_native_id(), _frames()))
    except Exception:
        pass
    return WdxTrace.orig(this, out)


_k.VirtualProtect.argtypes = [c_void_p, ctypes.c_size_t, c_uint32, ctypes.POINTER(c_uint32)]


def _poke(address, value):
    old = c_uint32()
    if not _k.VirtualProtect(c_void_p(address), 8, 0x04, ctypes.byref(old)):
        raise OSError("VirtualProtect failed")
    c_uint64.from_address(address).value = value
    _k.VirtualProtect(c_void_p(address), 8, old.value, ctypes.byref(old))


def wdx_trace_start():
    slot = BASE + VTABLE + 8 * SLOT
    if c_uint64.from_address(slot).value != BASE + MAKER:
        print("slot mismatch", hex(c_uint64.from_address(slot).value - BASE))
        return
    if ctypes.string_at(BASE + MAKER, 9).hex() != "41565657534883ec28":
        print("maker prologue mismatch")
        return
    proto = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)
    WdxTrace.orig = proto(BASE + MAKER)
    WdxTrace.cb = proto(_make)
    WdxTrace.stacks = []
    WdxTrace.started = time.monotonic()
    _poke(slot, ctypes.cast(WdxTrace.cb, c_void_p).value)
    WdxTrace.active = True
    print("tracing vtable slot", hex(slot - BASE))


def wdx_trace_stop():
    if WdxTrace.active:
        _poke(BASE + VTABLE + 8 * SLOT, BASE + MAKER)
        WdxTrace.active = False
    print("stopped;", len(WdxTrace.stacks), "captures")


def wdx_trace_report():
    from collections import Counter
    seen = Counter(tuple(s[2][:14]) for s in WdxTrace.stacks)
    for stack, n in seen.most_common():
        print(n, "x", " ".join(hex(a) for a in stack))
    print("threads", Counter(s[1] for s in WdxTrace.stacks), "game thread", threading.get_native_id())
