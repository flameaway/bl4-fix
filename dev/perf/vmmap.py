"""
Committed-memory map of a running process (read-only): groups committed regions by type,
protection and allocation size, and shows how much of each group is actually resident.

    python dev/perf/vmmap.py <pid> [--top 25] [--resident]   # --resident: QueryWorkingSetEx (slower)

Run it twice some minutes apart to see what grows.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
from ctypes import wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", w.DWORD), ("PartitionId", w.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", w.DWORD), ("Protect", w.DWORD), ("Type", w.DWORD)]


k32.VirtualQueryEx.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t)
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.OpenProcess.restype = w.HANDLE
psapi.QueryWorkingSetEx.argtypes = (w.HANDLE, ctypes.c_void_p, w.DWORD)
psapi.GetMappedFileNameW.argtypes = (w.HANDLE, ctypes.c_void_p, w.LPWSTR, w.DWORD)

MEM_COMMIT, MEM_PRIVATE, MEM_MAPPED, MEM_IMAGE = 0x1000, 0x20000, 0x40000, 0x1000000
PROT = {0x01: "NA", 0x02: "R", 0x04: "RW", 0x08: "WC", 0x10: "X", 0x20: "RX", 0x40: "RWX", 0x80: "WCX"}


def regions(h):
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFF0000:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        yield (mbi.BaseAddress or 0, mbi.AllocationBase or 0, mbi.RegionSize, mbi.State, mbi.Protect, mbi.Type)
        addr = (mbi.BaseAddress or 0) + mbi.RegionSize


def resident_bytes(h, base, size):
    """Bytes of [base, base+size) in the working set (QueryWorkingSetEx, 4 KB pages)."""
    n = size // 4096
    total = 0
    chunk = 65536
    for start in range(0, n, chunk):
        cnt = min(chunk, n - start)
        arr = (ctypes.c_uint64 * (cnt * 2))()
        for i in range(cnt):
            arr[i * 2] = base + (start + i) * 4096
        if psapi.QueryWorkingSetEx(h, arr, ctypes.sizeof(arr)):
            total += sum(1 for i in range(cnt) if arr[i * 2 + 1] & 1)
    return total * 4096


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--resident", action="store_true")
    ap.add_argument("--save", help="write the allocation list to this json")
    ap.add_argument("--diff", help="compare with a json written by --save")
    args = ap.parse_args()
    h = k32.OpenProcess(0x410, False, args.pid)

    allocs = collections.defaultdict(lambda: [0, 0, set(), 0])  # alloc base -> committed, reserved, prots, type
    by_kind = collections.Counter()
    for base, abase, size, state, prot, typ in regions(h):
        if state == MEM_COMMIT:
            a = allocs[abase]
            a[0] += size
            a[2].add(PROT.get(prot & 0xFF, hex(prot)))
            a[3] = typ
            kind = {MEM_PRIVATE: "private", MEM_MAPPED: "mapped", MEM_IMAGE: "image"}.get(typ, hex(typ))
            by_kind[kind] += size
        elif state == 0x2000:
            allocs[abase][1] += size
    gb = 1 / 2**30
    if args.save or args.diff:
        import json, time
        cur = {str(k): [v[0], v[3]] for k, v in allocs.items() if v[0]}
        if args.save:
            json.dump({"time": time.time(), "allocs": cur}, open(args.save, "w"))
        if args.diff:
            old = json.load(open(args.diff))
            dt = time.time() - old["time"]
            o = old["allocs"]
            new = {k: v for k, v in cur.items() if k not in o}
            gone = {k: v for k, v in o.items() if k not in cur}
            grown = {k: (cur[k][0] - o[k][0]) for k in cur if k in o and cur[k][0] != o[k][0]}
            tot = lambda d: sum(v[0] for v in d.values()) * gb
            print(f"since {dt / 60:.1f} min: +{len(new)} allocations ({tot(new):.2f} GB), -{len(gone)} ({tot(gone):.2f} GB), "
                  f"{len(grown)} changed size ({sum(grown.values()) * gb:+.2f} GB)")
            sizes = collections.Counter()
            for k, v in new.items():
                sizes[(v[0] // 2**20, v[1])] += 1
            print("  new allocations by size (MB, type): " + ", ".join(
                f"{mb}MB{'' if t == MEM_PRIVATE else '(map)' if t == MEM_MAPPED else '(img)'} x{n}"
                for (mb, t), n in sizes.most_common(15)))
            gs = collections.Counter()
            for k, v in gone.items():
                gs[(v[0] // 2**20, v[1])] += 1
            print("  freed allocations by size: " + ", ".join(f"{mb}MB x{n}" for (mb, t), n in gs.most_common(10)))
    print("committed by type: " + ", ".join(f"{k} {v * gb:.2f} GB" for k, v in by_kind.items()))

    # private allocations grouped by committed size bucket
    buckets = collections.Counter()
    bucket_n = collections.Counter()
    for abase, (c, r, prots, typ) in allocs.items():
        if typ != MEM_PRIVATE or not c:
            continue
        mb = c / 2**20
        b = ("<1MB" if mb < 1 else "1-16MB" if mb < 16 else "16-64MB" if mb < 64 else "64-256MB" if mb < 256
             else "256MB-1GB" if mb < 1024 else ">=1GB")
        buckets[b] += c
        bucket_n[b] += 1
    print("private committed by allocation size:")
    for b in ["<1MB", "1-16MB", "16-64MB", "64-256MB", "256MB-1GB", ">=1GB"]:
        if b in buckets:
            print(f"  {b:>10}: {bucket_n[b]:6d} allocations, {buckets[b] * gb:6.2f} GB")

    print(f"largest {args.top} allocations (committed / reserved):")
    for abase, (c, r, prots, typ) in sorted(allocs.items(), key=lambda kv: -kv[1][0])[:args.top]:
        name = ""
        if typ != MEM_PRIVATE:
            buf = ctypes.create_unicode_buffer(260)
            psapi.GetMappedFileNameW(h, ctypes.c_void_p(abase), buf, 260)
            name = buf.value.rsplit("\\", 1)[-1]
        res = ""
        if args.resident:
            res = f" resident {resident_bytes(h, abase, c) * gb:5.2f} GB"
        print(f"  {abase:#014x} {c * gb:6.2f} / {r * gb:6.2f} GB {'/'.join(sorted(prots)):8} "
              f"{ {MEM_PRIVATE: 'priv', MEM_MAPPED: 'map', MEM_IMAGE: 'img'}.get(typ, '?')}{res} {name}")


if __name__ == "__main__":
    main()
