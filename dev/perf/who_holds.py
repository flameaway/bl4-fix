"""
Lists every process holding a handle to the given process, with the access it was granted.
PROCESS_SET_QUOTA (0x100) is what SetProcessWorkingSetSize / EmptyWorkingSet need, and
PROCESS_VM_WRITE / CREATE_THREAD mark injectors. Needs an elevated shell for system processes.

    python dev/perf/who_holds.py <pid>
"""

import ctypes
import struct
import sys
from ctypes import wintypes as w

ntdll = ctypes.WinDLL("ntdll")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi")
k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
psapi.GetProcessImageFileNameW.argtypes = (w.HANDLE, w.LPWSTR, w.DWORD)

ACCESS = {0x1: "TERMINATE", 0x2: "CREATE_THREAD", 0x8: "VM_OPERATION", 0x10: "VM_READ", 0x20: "VM_WRITE",
          0x40: "DUP_HANDLE", 0x80: "CREATE_PROCESS", 0x100: "SET_QUOTA", 0x200: "SET_INFORMATION",
          0x400: "QUERY_INFORMATION", 0x800: "SUSPEND_RESUME", 0x1000: "QUERY_LIMITED"}


def handles():
    size = 1 << 24
    while True:
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong()
        st = ntdll.NtQuerySystemInformation(64, buf, size, ctypes.byref(ret)) & 0xFFFFFFFF
        if st == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            size = max(size * 2, ret.value + (1 << 20))
            continue
        if st:
            raise OSError(hex(st))
        break
    n = struct.unpack_from("<Q", buf, 0)[0]
    # SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX: Object, UniqueProcessId, HandleValue, GrantedAccess,
    # CreatorBackTraceIndex(u16), ObjectTypeIndex(u16), HandleAttributes, Reserved  (40 bytes)
    for i in range(n):
        obj, pid, hv, acc, _, typ, _, _ = struct.unpack_from("<QQQIHHII", buf, 16 + i * 40)
        yield obj, pid, hv, acc, typ


def name_of(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return "?"
    b = ctypes.create_unicode_buffer(512)
    psapi.GetProcessImageFileNameW(h, b, 512)
    k32.CloseHandle(h)
    return b.value.rsplit("\\", 1)[-1]


def main():
    target = int(sys.argv[1])
    me = k32.GetCurrentProcessId()
    mine = k32.OpenProcess(0x1000, False, target)
    entries = list(handles())
    obj = next(o for o, p, hv, a, t in entries if p == me and hv == mine)
    rows = []
    for o, p, hv, acc, t in entries:
        if o == obj and p not in (me, target):
            flags = [v for k, v in ACCESS.items() if acc & k]
            rows.append((p, name_of(p), hex(acc), " ".join(flags)))
    for r in sorted(rows, key=lambda r: r[1].lower()):
        mark = "  <-- can trim working set" if "SET_QUOTA" in r[3] else ""
        print(f"{r[0]:6} {r[1]:32} {r[2]:>10} {r[3]}{mark}")


if __name__ == "__main__":
    main()
