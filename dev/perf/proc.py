"""
Read-only access to a running game from another process: threads (with their names), modules,
memory, thread contexts, and an x64 stack unwinder driven by each module's .pdata unwind info.

Nothing here writes to the game. SuspendThread is used only around GetThreadContext + one stack
read (tens of microseconds); `Process.sample()` always resumes, even if Ctrl+C arrives.
"""

from __future__ import annotations

import bisect
import ctypes
import struct
from ctypes import wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
psapi = ctypes.WinDLL("psapi", use_last_error=True)

k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
k32.OpenThread.restype = w.HANDLE
k32.OpenThread.argtypes = (w.DWORD, w.BOOL, w.DWORD)
k32.ReadProcessMemory.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t))
k32.ReadProcessMemory.restype = w.BOOL
k32.SuspendThread.argtypes = (w.HANDLE,)
k32.SuspendThread.restype = w.DWORD
k32.ResumeThread.argtypes = (w.HANDLE,)
k32.ResumeThread.restype = w.DWORD
k32.GetThreadContext.argtypes = (w.HANDLE, ctypes.c_void_p)
k32.GetThreadContext.restype = w.BOOL
k32.GetThreadDescription.argtypes = (w.HANDLE, ctypes.POINTER(ctypes.c_wchar_p))
k32.GetThreadDescription.restype = ctypes.c_long
k32.LocalFree.argtypes = (ctypes.c_void_p,)
k32.CloseHandle.argtypes = (w.HANDLE,)
k32.CreateToolhelp32Snapshot.restype = w.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = (w.DWORD, w.DWORD)
k32.GetThreadTimes.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
psapi.EnumProcessModulesEx.argtypes = (w.HANDLE, ctypes.POINTER(ctypes.c_void_p), w.DWORD,
                                       ctypes.POINTER(w.DWORD), w.DWORD)
psapi.GetModuleBaseNameW.argtypes = (w.HANDLE, ctypes.c_void_p, w.LPWSTR, w.DWORD)
psapi.GetModuleInformation.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, w.DWORD)
psapi.QueryWorkingSetEx.argtypes = (w.HANDLE, ctypes.c_void_p, w.DWORD)
ntdll.NtQueryInformationThread.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG,
                                           ctypes.POINTER(w.ULONG))


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ThreadID", w.DWORD),
                ("th32OwnerProcessID", w.DWORD), ("tpBasePri", w.LONG), ("tpDeltaPri", w.LONG),
                ("dwFlags", w.DWORD)]


k32.Thread32First.argtypes = (w.HANDLE, ctypes.POINTER(THREADENTRY32))
k32.Thread32Next.argtypes = (w.HANDLE, ctypes.POINTER(THREADENTRY32))

PROCESS_QUERY_INFORMATION = 0x400
PROCESS_VM_READ = 0x10
THREAD_SUSPEND_RESUME = 0x2
THREAD_GET_CONTEXT = 0x8
THREAD_QUERY_INFORMATION = 0x40
THREAD_QUERY_LIMITED_INFORMATION = 0x800

CONTEXT_AMD64_CONTROL_INTEGER = 0x100003
# CONTEXT (x64) offsets: integer registers in UNWIND_INFO register-number order, then Rip
_REG_OFFS = [0x78, 0x80, 0x88, 0x90, 0x98, 0xA0, 0xA8, 0xB0,  # rax rcx rdx rbx rsp rbp rsi rdi
             0xB8, 0xC0, 0xC8, 0xD0, 0xD8, 0xE0, 0xE8, 0xF0]  # r8..r15
_RIP_OFF = 0xF8
RSP = 4


class Module:
    """One loaded module: lazily read pages, its function table and export names."""

    def __init__(self, proc: "Process", base: int, size: int, name: str):
        self.proc, self.base, self.size, self.name = proc, base, size, name
        self.end = base + size
        self._pages: dict[int, bytes] = {}
        self._funcs: list[tuple[int, int, int]] | None = None
        self._starts: list[int] = []
        self._exports: list[tuple[int, str]] | None = None
        self._unwind_cache: dict[int, tuple] = {}
        self.local = None  # optional local flat image (file offset == RVA) for the main exe

    def read(self, rva: int, size: int) -> bytes:
        if self.local is not None and rva + size <= len(self.local):
            return self.local[rva:rva + size]
        out = bytearray()
        while size > 0:
            page = rva & ~0xFFF
            data = self._pages.get(page)
            if data is None:
                data = self.proc.read(self.base + page, 0x1000).ljust(0x1000, b"\0")
                self._pages[page] = data
            off = rva - page
            take = min(size, 0x1000 - off)
            out += data[off:off + take]
            rva += take
            size -= take
        return bytes(out)

    def _dirs(self):
        hdr = self.read(0, 0x400)
        pe = struct.unpack_from("<I", hdr, 0x3C)[0]
        return pe + 24 + 112  # data directories of a PE32+ optional header

    def funcs(self):
        if self._funcs is None:
            d = self._dirs()
            exc_rva, exc_size = struct.unpack_from("<II", self.read(d + 3 * 8, 8))
            raw = self.read(exc_rva, exc_size) if exc_size else b""
            fs = []
            for i in range(0, len(raw) - 11, 12):
                b, e, u = struct.unpack_from("<III", raw, i)
                if b and e > b:
                    fs.append((b, e, u))
            fs.sort()
            self._funcs = fs
            self._starts = [f[0] for f in fs]
        return self._funcs

    def lookup(self, rva: int):
        fs = self.funcs()
        i = bisect.bisect_right(self._starts, rva) - 1
        if i >= 0 and fs[i][0] <= rva < fs[i][1]:
            return fs[i]
        return None

    def exports(self):
        if self._exports is None:
            ex = []
            try:
                d = self._dirs()
                e_rva, e_size = struct.unpack_from("<II", self.read(d, 8))
                if e_size:
                    ed = self.read(e_rva, 40)
                    nfuncs, nnames, afuncs, anames, aords = struct.unpack_from("<IIIII", ed, 20)
                    funcs = struct.unpack_from(f"<{nfuncs}I", self.read(afuncs, nfuncs * 4))
                    names = struct.unpack_from(f"<{nnames}I", self.read(anames, nnames * 4))
                    ords = struct.unpack_from(f"<{nnames}H", self.read(aords, nnames * 2))
                    for n, o in zip(names, ords):
                        s = self.read(n, 128).split(b"\0", 1)[0].decode(errors="replace")
                        f = funcs[o]
                        if not (e_rva <= f < e_rva + e_size):  # skip forwarders
                            ex.append((f, s))
            except Exception:  # noqa: BLE001
                pass
            ex.sort()
            self._exports = ex
        return self._exports

    def unwind_ops(self, unwind_rva: int):
        """Parses UNWIND_INFO (following chains) into (prolog_size, frame_reg, frame_off, ops)."""
        hit = self._unwind_cache.get(unwind_rva)
        if hit is not None:
            return hit
        chain = []
        rva = unwind_rva
        first = None
        for _ in range(32):
            hdr = self.read(rva, 4)
            flags = hdr[0] >> 3
            prolog, count, frame = hdr[1], hdr[2], hdr[3]
            codes = self.read(rva + 4, count * 2)
            ops = []
            i = 0
            while i < count:
                off, b = codes[i * 2], codes[i * 2 + 1]
                op, info = b & 0xF, b >> 4
                if op == 0:  # PUSH_NONVOL
                    ops.append((off, 0, info, 0)); i += 1
                elif op == 1:  # ALLOC_LARGE
                    if info == 0:
                        size = struct.unpack_from("<H", codes, (i + 1) * 2)[0] * 8; i += 2
                    else:
                        size = struct.unpack_from("<I", codes, (i + 1) * 2)[0]; i += 3
                    ops.append((off, 1, 0, size))
                elif op == 2:  # ALLOC_SMALL
                    ops.append((off, 1, 0, info * 8 + 8)); i += 1
                elif op == 3:  # SET_FPREG
                    ops.append((off, 3, 0, 0)); i += 1
                elif op == 4:  # SAVE_NONVOL
                    ops.append((off, 4, info, struct.unpack_from("<H", codes, (i + 1) * 2)[0] * 8)); i += 2
                elif op == 5:  # SAVE_NONVOL_FAR
                    ops.append((off, 4, info, struct.unpack_from("<I", codes, (i + 1) * 2)[0])); i += 3
                elif op == 6:  # EPILOG (v2) / SAVE_XMM (v1): no register effect for us
                    i += 2 if (hdr[0] & 7) == 1 else 1
                elif op == 7:
                    i += 3
                elif op == 8:  # SAVE_XMM128
                    i += 2
                elif op == 9:  # SAVE_XMM128_FAR
                    i += 3
                elif op == 10:  # PUSH_MACHFRAME
                    ops.append((off, 10, info, 0)); i += 1
                else:
                    i += 1
            entry = (prolog, frame & 0xF, (frame >> 4) * 16, ops)
            if first is None:
                first = entry
            else:
                chain.append(entry)
            if flags & 0x4:  # UNW_FLAG_CHAININFO: a RUNTIME_FUNCTION follows the codes
                crva = rva + 4 + ((count + 1) & ~1) * 2
                _, _, rva = struct.unpack_from("<III", self.read(crva, 12))
                continue
            break
        res = (first, chain)
        self._unwind_cache[unwind_rva] = res
        return res


class Process:
    def __init__(self, pid: int):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid})")
        self.modules: list[Module] = []
        self._mod_starts: list[int] = []
        self.refresh_modules()
        self._buf = ctypes.create_string_buffer(256 * 1024)
        self._ctx = ctypes.create_string_buffer(1232 + 16)
        a = ctypes.addressof(self._ctx)
        self._ctx_addr = (a + 15) & ~15
        self._got = ctypes.c_size_t()

    # memory
    def read(self, addr: int, size: int) -> bytes:
        buf = self._buf if size <= len(self._buf) else ctypes.create_string_buffer(size)
        ok = k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(self._got))
        if not ok and not self._got.value:
            return b""
        return buf.raw[:self._got.value]

    def resident(self, addr: int) -> bool | None:
        """Whether the page holding addr is in the working set right now (QueryWorkingSetEx). A thread
        sampled at a non-resident code address is waiting on a hard page fault."""
        q = (ctypes.c_uint64 * 2)(addr & ~0xFFF, 0)
        if not psapi.QueryWorkingSetEx(self.h, q, 16):
            return None
        return bool(q[1] & 1)

    def u64(self, addr: int) -> int | None:
        d = self.read(addr, 8)
        return struct.unpack("<Q", d)[0] if len(d) == 8 else None

    # modules
    def refresh_modules(self):
        arr = (ctypes.c_void_p * 2048)()
        need = w.DWORD()
        psapi.EnumProcessModulesEx(self.h, arr, ctypes.sizeof(arr), ctypes.byref(need), 3)
        n = need.value // ctypes.sizeof(ctypes.c_void_p)
        old = {m.base: m for m in self.modules}
        mods = []
        for i in range(min(n, 2048)):
            base = arr[i]
            if not base:
                continue
            if base in old:
                mods.append(old[base])
                continue
            info = (ctypes.c_void_p * 3)()
            psapi.GetModuleInformation(self.h, ctypes.c_void_p(base), info, ctypes.sizeof(info))
            name = ctypes.create_unicode_buffer(260)
            psapi.GetModuleBaseNameW(self.h, ctypes.c_void_p(base), name, 260)
            size = int(info[1] or 0) & 0xFFFFFFFF
            mods.append(Module(self, base, size, name.value))
        mods.sort(key=lambda m: m.base)
        self.modules = mods
        self._mod_starts = [m.base for m in mods]

    @property
    def main(self) -> Module:
        return next(m for m in self.modules if m.name.lower().endswith(".exe"))

    def module_of(self, addr: int) -> Module | None:
        i = bisect.bisect_right(self._mod_starts, addr) - 1
        if i >= 0 and addr < self.modules[i].end:
            return self.modules[i]
        return None

    # threads
    def threads(self) -> list[tuple[int, str]]:
        snap = k32.CreateToolhelp32Snapshot(0x4, 0)
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        out = []
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == self.pid:
                out.append((te.th32ThreadID, thread_name(te.th32ThreadID)))
            ok = k32.Thread32Next(snap, ctypes.byref(te))
        k32.CloseHandle(snap)
        return out


def thread_name(tid: int) -> str:
    h = k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not h:
        return ""
    p = ctypes.c_wchar_p()
    name = ""
    if k32.GetThreadDescription(h, ctypes.byref(p)) >= 0 and p.value:
        name = p.value
        k32.LocalFree(p)
    k32.CloseHandle(h)
    return name


class Thread:
    def __init__(self, proc: Process, tid: int, name: str):
        self.proc, self.tid, self.name = proc, tid, name
        self.h = k32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION,
                                False, tid)
        if not self.h:
            raise OSError(ctypes.get_last_error(), f"OpenThread({tid})")
        tbi = (ctypes.c_byte * 48)()
        ntdll.NtQueryInformationThread(self.h, 0, tbi, 48, None)
        teb = struct.unpack_from("<Q", bytes(tbi), 8)[0]
        self.stack_base = proc.u64(teb + 8) or 0
        self._ct = (w.FILETIME * 4)()

    def cpu_time(self) -> float:
        """Kernel + user CPU seconds used by this thread so far."""
        ft = self._ct
        k32.GetThreadTimes(self.h, ctypes.byref(ft[0]), ctypes.byref(ft[1]), ctypes.byref(ft[2]),
                           ctypes.byref(ft[3]))
        k = ft[2].dwLowDateTime | (ft[2].dwHighDateTime << 32)
        u = ft[3].dwLowDateTime | (ft[3].dwHighDateTime << 32)
        return (k + u) / 1e7

    def capture(self, max_stack: int = 192 * 1024):
        """Suspends the thread just long enough to read its registers and stack.

        Returns (regs list[16], rip, rsp, stack bytes) or None."""
        p = self.proc
        ctx = p._ctx_addr
        ctypes.memmove(ctx + 0x30, struct.pack("<I", CONTEXT_AMD64_CONTROL_INTEGER), 4)
        if k32.SuspendThread(self.h) == 0xFFFFFFFF:
            return None
        try:
            if not k32.GetThreadContext(self.h, ctx):
                return None
            raw = ctypes.string_at(ctx, 0x100)
            rsp = struct.unpack_from("<Q", raw, 0x98)[0]
            size = max_stack
            if self.stack_base and self.stack_base > rsp:
                size = min(size, self.stack_base - rsp)
            got = ctypes.c_size_t()
            k32.ReadProcessMemory(p.h, ctypes.c_void_p(rsp), p._buf, size, ctypes.byref(got))
        finally:
            k32.ResumeThread(self.h)
        regs = [struct.unpack_from("<Q", raw, o)[0] for o in _REG_OFFS]
        rip = struct.unpack_from("<Q", raw, _RIP_OFF)[0]
        return regs, rip, rsp, p._buf.raw[:got.value]


def unwind(proc: Process, regs: list[int], rip: int, rsp: int, stack: bytes, max_frames: int = 96):
    """Walks the stack captured by Thread.capture. Returns a list of absolute return addresses
    (the first is the sampled RIP)."""
    regs = list(regs)
    out = [rip]
    lo, hi = rsp, rsp + len(stack)

    def rd(addr):
        if lo <= addr <= hi - 8:
            return struct.unpack_from("<Q", stack, addr - lo)[0]
        raise IndexError

    cur_rsp = rsp
    first = True
    try:
        for _ in range(max_frames):
            mod = proc.module_of(rip)
            if mod is None:
                break
            rva = rip - mod.base
            fe = mod.lookup(rva if first else rva - 1)
            if fe is None:  # leaf function: return address on top of the stack
                rip = rd(cur_rsp)
                cur_rsp += 8
            else:
                begin, _, urva = fe
                offset = rva - begin
                (prolog, freg, foff, ops), chain = mod.unwind_ops(urva)
                # a `ret` at RIP means the epilogue already undid the frame (leaf sample only)
                if first and mod.read(rva, 1) == b"\xc3":
                    rip = rd(cur_rsp)
                    cur_rsp += 8
                    out.append(rip)
                    first = False
                    regs[RSP] = cur_rsp
                    continue
                in_prolog = offset < prolog
                regs[RSP] = cur_rsp
                set_fp = any(o[1] == 3 and (not in_prolog or o[0] <= offset) for o in ops)
                est = (regs[freg] - foff) if (freg and set_fp) else cur_rsp
                machframe = False
                for entries in ([(ops, in_prolog)] + [(c[3], False) for c in chain]):
                    for off, kind, reg, val in entries[0]:
                        if entries[1] and off > offset:
                            continue
                        if kind == 0:
                            regs[reg] = rd(cur_rsp); cur_rsp += 8
                        elif kind == 1:
                            cur_rsp += val
                        elif kind == 3:
                            cur_rsp = regs[freg] - foff
                        elif kind == 4:
                            regs[reg] = rd(est + val)
                        elif kind == 10:
                            if reg:
                                cur_rsp += 8
                            rip = rd(cur_rsp)
                            cur_rsp = rd(cur_rsp + 24)
                            machframe = True
                            break
                    if machframe:
                        break
                if not machframe:
                    rip = rd(cur_rsp)
                    cur_rsp += 8
            first = False
            regs[RSP] = cur_rsp
            if rip < 0x10000 or not (lo <= cur_rsp <= hi):
                break
            out.append(rip)
    except IndexError:
        pass
    return out
