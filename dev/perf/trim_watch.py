"""
Catches whoever trims the game's working set: polls its working set every 50 ms and, when it drops
by more than --drop-mb, captures the stack of every game thread at once and logs any thread inside
a working-set / memory-priority call. No game thread in such a call = the kernel's memory manager
did it. Read-only (suspends each thread for ~0.1 ms only at a drop).

    python dev/perf/trim_watch.py <pid> [--minutes 60] [--drop-mb 300] [--log .perf/trim.log]
"""

import argparse
import ctypes
import ctypes.wintypes as w
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402
from symbols import Symbols  # noqa: E402

SUSPECT = ("WorkingSet", "SetInformationProcess", "SetProcessInformation", "OfferVirtualMemory",
           "DiscardVirtualMemory", "SetSystemInformation", "VirtualUnlock", "NtUnlockVirtualMemory")


class PMC(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "a", "b", "c", "d", "PagefileUsage", "PeakPagefileUsage",
        "PrivateUsage")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--drop-mb", type=float, default=300)
    ap.add_argument("--log", default=".perf/trim.log")
    a = ap.parse_args()
    p = proc.Process(a.pid)
    meta = {"pid": a.pid, "modules": [(m.base, m.size, m.name) for m in p.modules], "main_base": p.main.base}
    sym = Symbols(meta)
    psapi = ctypes.windll.psapi
    m = PMC()
    m.cb = ctypes.sizeof(m)
    last = None
    end = time.time() + a.minutes * 60
    out = open(a.log, "a", buffering=1, encoding="utf-8")
    out.write(f"--- {time.strftime('%H:%M:%S')} watching {a.pid}\n")
    while time.time() < end:
        if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(p.h), ctypes.byref(m), m.cb):
            break
        ws = m.WorkingSetSize
        if last is not None and last - ws > a.drop_mb * 2**20:
            stamp = time.strftime("%H:%M:%S")
            hits = []
            for tid, name in p.threads():
                try:
                    t = proc.Thread(p, tid, name)
                    cap = t.capture(64 * 1024)
                    if not cap:
                        continue
                    names = [sym.name(x) for x in proc.unwind(p, *cap)[:14]]
                    if any(s in n for n in names for s in SUSPECT):
                        hits.append(f"    {name or tid}: " + " <- ".join(names))
                except OSError:
                    pass
            out.write(f"{stamp} WS {last / 2**30:.2f} -> {ws / 2**30:.2f} GB "
                      f"(-{(last - ws) / 2**20:.0f} MB); threads in trim calls: {len(hits)}\n")
            for h in hits:
                out.write(h + "\n")
        last = ws
        time.sleep(0.05)


if __name__ == "__main__":
    main()
