"""Scan a process's private committed RW memory for 8-byte values. Usage: heapscan.py PID HEXVALUE [HEXVALUE...]"""
import ctypes, struct, sys, time
from ctypes import wintypes as w

pid = int(sys.argv[1])
needles = [struct.pack("<Q", int(v, 16)) for v in sys.argv[2:]]
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = w.HANDLE
k32.ReadProcessMemory.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", w.DWORD),
                ("PartitionId", w.WORD), ("RegionSize", ctypes.c_size_t), ("State", w.DWORD), ("Protect", w.DWORD),
                ("Type", w.DWORD)]


k32.VirtualQueryEx.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t)
h = k32.OpenProcess(0x0410, False, pid)
addr, hits, scanned, t = 0, [], 0, time.time()
mbi = MBI()
while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
    base, size = mbi.BaseAddress or 0, mbi.RegionSize
    if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect in (0x04, 0x40):  # COMMIT, PRIVATE, RW/RWX
        off = 0
        while off < size:
            n = min(64 << 20, size - off)
            buf = ctypes.create_string_buffer(n)
            got = ctypes.c_size_t()
            if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)) and got.value:
                data = buf.raw[:got.value]
                scanned += got.value
                for nd in needles:
                    i = data.find(nd)
                    while i != -1:
                        if i % 8 == 0:
                            hits.append((nd.hex(), hex(base + off + i)))
                        i = data.find(nd, i + 1)
            off += n
    addr = base + size
    if addr >= 0x7FFFFFFF0000:
        break
print(f"scanned {scanned / 2**30:.1f} GiB in {time.time() - t:.0f}s; hits:", hits[:50])
