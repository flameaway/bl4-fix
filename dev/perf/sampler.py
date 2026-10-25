"""
External sampling profiler for a running Borderlands 4 (read-only, nothing injected).

Every tick it reads the game's frame counter and captures the stacks of the chosen threads
(suspend -> registers + stack copy -> resume, ~0.1 ms per thread), then unwinds them offline from
the modules' unwind tables. Each sample is tagged with the game frame it fell in, so analyze.py can
show what each thread was doing during the slow frames only.

    python dev/perf/sampler.py <pid> --seconds 60 --out capture.pkl.gz
        [--rate 1000] [--threads GameThread,RenderThread,RHIThread,...] [--all-named]

The game thread pauses for the copy of its stack on every sample: at 1 kHz that is ~5-10% of the
game thread's time, so absolute frame times read a little high while sampling. Compare captures
taken at the same rate.
"""

from __future__ import annotations

import argparse
import gzip
import pickle
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402

# GFrameCounter for build 26007962 (found by matching KismetSystemLibrary.GetFrameCount against
# .data; see frame_counter.py). Verified against the bridge before every capture.
FRAME_COUNTER_RVA = 0xC761E08

DEFAULT_THREADS = ["GameThread", "RenderThread", "RHIThread", "RHISubmissionThread", "FAsyncLoadingThread",
                   "IoDispatcher"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--rate", type=float, default=1000, help="samples per second per thread")
    ap.add_argument("--threads", default=",".join(DEFAULT_THREADS),
                    help="comma-separated name prefixes")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fc-rva", type=lambda s: int(s, 0), default=FRAME_COUNTER_RVA)
    args = ap.parse_args()

    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))

    p = proc.Process(args.pid)
    fc_addr = p.main.base + args.fc_rva
    wanted = [w.strip() for w in args.threads.split(",") if w.strip()]
    targets = []
    for tid, name in p.threads():
        if any(name.startswith(w) for w in wanted):
            try:
                targets.append(proc.Thread(p, tid, name))
            except OSError as e:
                print("skip", tid, name, e)
    targets.sort(key=lambda t: (wanted.index(next(w for w in wanted if t.name.startswith(w))), t.name))
    print("threads:", ", ".join(f"{t.name}({t.tid})" for t in targets))

    # warm the module function tables (the first unwind through a module reads its .pdata)
    for t in targets:
        cap = t.capture()
        if cap:
            proc.unwind(p, *cap)

    period = 1.0 / args.rate
    samples = []  # (t_ns, thread index, frame, frames tuple)
    frames = []  # (t_ns, frame counter) whenever it changes
    cpu0 = [t.cpu_time() for t in targets]
    last_fc = None
    t_start = time.perf_counter()
    deadline = t_start + args.seconds
    next_t = t_start
    misses = 0
    while not stop["now"]:
        now = time.perf_counter()
        if now >= deadline:
            break
        if now < next_t:
            time.sleep(max(0.0, next_t - now - 0.0003))
            continue
        next_t += period
        if next_t < now - 0.05:  # fell behind: don't try to catch up in a burst
            next_t = now + period
            misses += 1
        fc = p.u64(fc_addr)
        t_ns = time.perf_counter_ns()
        if fc != last_fc:
            frames.append((t_ns, fc))
            last_fc = fc
        for i, t in enumerate(targets):
            cap = t.capture()
            if cap is None:
                continue
            # a thread parked at a code page that isn't resident is stalled on a hard page fault
            res = p.resident(cap[1])
            samples.append((time.perf_counter_ns(), i, fc, tuple(proc.unwind(p, *cap)), res))
    elapsed = time.perf_counter() - t_start
    cpu1 = [t.cpu_time() for t in targets]

    p.refresh_modules()
    meta = {
        "pid": args.pid,
        "rate": args.rate,
        "elapsed": elapsed,
        "threads": [(t.tid, t.name) for t in targets],
        "thread_cpu": [(b - a) / elapsed for a, b in zip(cpu0, cpu1)],
        "modules": [(m.base, m.size, m.name) for m in p.modules],
        "main_base": p.main.base,
        "fc_rva": args.fc_rva,
        "misses": misses,
        "time": time.time(),
    }
    with gzip.open(args.out, "wb") as f:
        pickle.dump({"meta": meta, "frames": frames, "samples": samples}, f)
    n = len(samples)
    print(f"{n} samples in {elapsed:.1f}s ({n / elapsed / max(1, len(targets)):.0f}/s per thread), "
          f"{len(frames)} frames, behind {misses}x -> {args.out}")
    for (tid, name), c in zip(meta["threads"], meta["thread_cpu"]):
        print(f"  {name:28} cpu {c * 100:5.1f}%")


if __name__ == "__main__":
    main()
