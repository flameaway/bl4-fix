"""Dominant stacks of one thread in the slow frames of a capture:
    python dev/perf/frame_stacks.py capture.pkl.gz RHIThread [--min-ms 150] [--depth 30] [--frames 6]"""
import argparse
import collections
import gzip
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import frame_times  # noqa: E402
from symbols import Symbols  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("capture")
ap.add_argument("thread")
ap.add_argument("--min-ms", type=float, default=150)
ap.add_argument("--depth", type=int, default=30)
ap.add_argument("--frames", type=int, default=6)
ap.add_argument("--paths", type=int, default=2)
a = ap.parse_args()
cap = pickle.load(gzip.open(a.capture))
meta, frames, samples = cap["meta"], cap["frames"], cap["samples"]
sym = Symbols(meta)
ti = next(i for i, (_, n) in enumerate(meta["threads"]) if n.startswith(a.thread))
slow = sorted([(d, f, t) for f, t, d, n in frame_times(frames) if n == 1 and d >= a.min_ms], reverse=True)
by = collections.defaultdict(list)
fs = {f for _, f, _ in slow}
for s in samples:
    if s[1] == ti and s[2] in fs:
        by[s[2]].append(s[3])
agg = collections.Counter()
for d, f, t in slow:
    c = collections.Counter()
    for st in by[f]:
        c[tuple(sym.name(x) for x in st[:a.depth])] += 1
        agg[tuple(sym.name(x) for x in st[:a.depth])] += 1
for d, f, t in slow[:a.frames]:
    print(f"=== frame {f} {d:.0f} ms ({len(by[f])} samples)")
    c = collections.Counter(tuple(sym.name(x) for x in st[:a.depth]) for st in by[f])
    for path, n in c.most_common(a.paths):
        print(f"  x{n}: " + " <- ".join(path))
print(f"=== all {len(slow)} frames >= {a.min_ms} ms, by function present (share of samples):")
tot = sum(len(by[f]) for _, f, _ in slow)
inc = collections.Counter()
for d, f, t in slow:
    for st in by[f]:
        for n in {sym.name(x) for x in st}:
            inc[n] += 1
for n, c in inc.most_common(40):
    print(f"  {c / max(1, tot) * 100:5.1f}%  {n}")
