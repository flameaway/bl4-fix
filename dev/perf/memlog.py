"""Logs a process's commit / working set / page faults and the system's free commit every N
seconds to a CSV (read-only):  python dev/perf/memlog.py <pid> .perf/mem.csv [--every 30]"""
import argparse
import ctypes
import ctypes.wintypes as w
import os
import time


class PMC(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
        "PrivateUsage")]


class PERF(ctypes.Structure):
    _fields_ = [("cb", w.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable", "SystemCache",
        "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        ("HandleCount", w.DWORD), ("ProcessCount", w.DWORD), ("ThreadCount", w.DWORD)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("out")
    ap.add_argument("--every", type=float, default=30)
    a = ap.parse_args()
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    k32.OpenProcess.restype = w.HANDLE
    h = k32.OpenProcess(0x410, 0, a.pid)
    hc = w.DWORD()
    new = not os.path.exists(a.out)
    with open(a.out, "a", buffering=1) as f:
        if new:
            f.write("time,private_gb,ws_gb,peak_ws_gb,page_faults,handles,sys_commit_gb,sys_commit_limit_gb,phys_avail_gb\n")
        while True:
            m = PMC(); m.cb = ctypes.sizeof(m)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
                break
            pi = PERF(); pi.cb = ctypes.sizeof(pi)
            psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb)
            k32.GetProcessHandleCount(h, ctypes.byref(hc))
            pg = pi.PageSize / 2**30
            f.write(f"{time.strftime('%H:%M:%S')},{m.PrivateUsage / 2**30:.3f},{m.WorkingSetSize / 2**30:.3f},"
                    f"{m.PeakWorkingSetSize / 2**30:.3f},{m.PageFaultCount},{hc.value},{pi.CommitTotal * pg:.2f},"
                    f"{pi.CommitLimit * pg:.2f},{pi.PhysicalAvailable * pg:.2f}\n")
            time.sleep(a.every)


if __name__ == "__main__":
    main()
