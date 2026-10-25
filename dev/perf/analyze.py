"""
Reports on a sampler.py capture: frame times, the slow frames, and what each thread was doing in
them compared with normal frames.

    python dev/perf/analyze.py capture.pkl.gz [--hitch-ms 40] [--thread GameThread] [--top 30]
        [--folded out.folded]      # flame graph input (speedscope.app / inferno / flamegraph.pl)
        [--each 10]                # print the dominant stack of the N worst frames
        [--focus NAME]             # only samples whose stack contains NAME (substring)
"""

from __future__ import annotations

import argparse
import collections
import gzip
import pickle
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from symbols import Symbols  # noqa: E402


def load(path):
    with gzip.open(path, "rb") as f:
        return pickle.load(f)


def frame_times(frames):
    """[(frame number, start ns, duration ms)] from the counter's change points."""
    out = []
    for (t0, f0), (t1, f1) in zip(frames, frames[1:]):
        if f0 is None or f1 is None or f1 <= f0:
            continue
        per = (t1 - t0) / 1e6 / (f1 - f0)
        out.append((f0, t0, per, f1 - f0))
    return out


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--hitch-ms", type=float, default=0)
    ap.add_argument("--thread", default="")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--each", type=int, default=8)
    ap.add_argument("--depth", type=int, default=40)
    ap.add_argument("--folded")
    ap.add_argument("--focus")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--events", help="events.json from dev/perf/events.py: list them next to slow frames")
    ap.add_argument("--share", help="comma-separated NAME substrings: % of each thread's samples containing it")
    ap.add_argument("--callers", help="show the call paths leading to functions matching NAME")
    ap.add_argument("--callees", help="show what functions matching NAME spend their time in")
    args = ap.parse_args()

    cap = load(args.capture)
    meta, frames, samples = cap["meta"], cap["frames"], cap["samples"]
    sym = Symbols(meta, live=not args.no_live)
    threads = [n for _, n in meta["threads"]]

    ft = frame_times(frames)
    single = [d for _, _, d, n in ft if n == 1]
    med = statistics.median(single) if single else 16.7
    hitch_ms = args.hitch_ms or max(2 * med, 33.0)
    print(f"capture: {meta['elapsed']:.1f}s, {len(samples)} samples, rate {meta['rate']}/s/thread, "
          f"behind {meta['misses']}x")
    print("thread CPU use: " + ", ".join(f"{n} {c * 100:.0f}%" for n, c in zip(threads, meta["thread_cpu"])))
    if ft:
        all_d = [d for _, _, d, _ in ft for _ in range(1)]
        fps = (frames[-1][1] - frames[0][1]) / ((frames[-1][0] - frames[0][0]) / 1e9)
        print(f"frames: {frames[-1][1] - frames[0][1]} in {(frames[-1][0] - frames[0][0]) / 1e9:.1f}s = {fps:.1f} fps;"
              f" frame ms median {med:.1f}, p90 {pct(all_d, 90):.1f}, p99 {pct(all_d, 99):.1f}, max {max(all_d):.1f}")
    hitches = [(f, t, d) for f, t, d, n in ft if n == 1 and d >= hitch_ms]
    hitch_set = {f for f, _, _ in hitches}
    hitch_total = sum(d for _, _, d in hitches)
    print(f"slow frames (>= {hitch_ms:.0f} ms): {len(hitches)}, {hitch_total:.0f} ms total; "
          f"worst: {', '.join(f'{d:.0f}' for _, _, d in sorted(hitches, key=lambda h: -h[2])[:12])}")

    # per thread aggregates
    names_cache: dict[int, tuple] = {}

    def keys(stack):
        out = []
        for a in stack:
            k = names_cache.get(a)
            if k is None:
                k = sym.func_key(a)
                names_cache[a] = k
            out.append(k)
        return out

    sel = [i for i, n in enumerate(threads) if not args.thread or n.startswith(args.thread)]
    folded = collections.Counter()
    for ti in sel:
        tname = threads[ti]
        incl = {"hitch": collections.Counter(), "normal": collections.Counter()}
        excl = {"hitch": collections.Counter(), "normal": collections.Counter()}
        tot = collections.Counter()
        for t_ns, i, fc, stack, *_ in samples:
            if i != ti:
                continue
            ks = keys(stack)
            if args.focus and not any(args.focus in sym.key_name(k) for k in ks):
                continue
            kind = "hitch" if fc in hitch_set else "normal"
            tot[kind] += 1
            for k in set(ks):
                incl[kind][k] += 1
            if ks:
                excl[kind][ks[0]] += 1
            if args.folded:
                folded[";".join([tname] + [sym.key_name(k) for k in reversed(ks[:args.depth * 2])])] += 1
        if not tot:
            continue
        print(f"\n=== {tname}: {tot['normal']} normal-frame samples, {tot['hitch']} slow-frame samples")
        nh, nn = max(1, tot["hitch"]), max(1, tot["normal"])
        print("  inclusive, normal frames:")
        for k, c in incl["normal"].most_common(args.top):
            print(f"    {c / nn * 100:5.1f}%  {sym.key_name(k)}")
        print("  exclusive (leaf), normal frames:")
        for k, c in excl["normal"].most_common(args.top // 2):
            print(f"    {c / nn * 100:5.1f}%  {sym.key_name(k)}")
        if tot["hitch"]:
            print("  over-represented in slow frames (inclusive: slow% vs normal%):")
            diff = sorted(incl["hitch"], key=lambda k: -(incl["hitch"][k] / nh - incl["normal"][k] / nn))
            for k in diff[:args.top]:
                a, b = incl["hitch"][k] / nh * 100, incl["normal"][k] / nn * 100
                if a - b < 2:
                    break
                print(f"    {a:5.1f}% vs {b:5.1f}%  {sym.key_name(k)}")

    if args.events:
        import json
        evs = json.load(open(args.events))
        t0 = frames[0][0] if frames else 0
        t_end = frames[-1][0] if frames else 0
        evs = [e for e in evs if t0 - 2e9 <= e[0] <= t_end + 2e9]
        print(f"\ntimeline: {len(evs)} game-state changes and {len(hitches)} slow frames")
        items = [(e[0], "event", e) for e in evs] + [(t, "slow", (f, d)) for f, t, d in hitches]
        prev = None
        for t, kind, x in sorted(items, key=lambda i: i[0]):
            if kind == "event":
                snap = dict(x[2])
                diff = {k: v for k, v in snap.items() if prev is None or prev.get(k) != v}
                prev = snap
                print(f"  +{(t - t0) / 1e9:7.2f}s  event {diff}")
            else:
                print(f"  +{(t - t0) / 1e9:7.2f}s  SLOW frame {x[0]}: {x[1]:.0f} ms")

    # the worst frames one by one: game thread's dominant path
    gt = threads.index("GameThread") if "GameThread" in threads else 0
    by_frame = collections.defaultdict(list)
    faults = collections.Counter()  # (frame, thread) -> samples parked on a non-resident code page
    fault_total = collections.Counter()
    for t_ns, i, fc, stack, *rest in samples:
        if rest and rest[0] is False:
            fault_total[i] += 1
            if fc in hitch_set:
                faults[(fc, i)] += 1
        if fc in hitch_set:
            by_frame[(fc, i)].append(stack)
    if fault_total:
        per = 1000 / meta["rate"]
        print("\nsamples parked on a non-resident code page (hard page fault), whole capture: " + ", ".join(
            f"{threads[i]} {n} (~{n * per:.0f} ms)" for i, n in sorted(fault_total.items())))
    for f, t, d in sorted(hitches, key=lambda h: -h[2])[:args.each]:
        print(f"\n--- frame {f}: {d:.0f} ms at +{(t - frames[0][0]) / 1e9:.1f}s")
        for ti in [gt] + [i for i in sel if i != gt]:
            stacks = by_frame.get((f, ti), [])
            if not stacks:
                continue
            # deepest common path: count each (depth-limited) root-first prefix
            c = collections.Counter()
            for s in stacks:
                ks = list(reversed(keys(s)))
                c[tuple(ks[:args.depth])] += 1
            top, n = c.most_common(1)[0]
            leafs = collections.Counter(keys(s)[0] for s in stacks)
            pf = faults.get((f, ti), 0)
            print(f"  [{threads[ti]}] {len(stacks)} samples{f' ({pf} in PAGE FAULTS)' if pf else ''}; top leaves: " +
                  "; ".join(f"{sym.key_name(k)} x{n}" for k, n in leafs.most_common(3)))
            if ti == gt:
                for k in top[-18:]:
                    print(f"      {sym.key_name(k)}")

    if args.share:
        pats = [x for x in args.share.split(",") if x]
        nframes = max(1, frames[-1][1] - frames[0][1]) if frames else 1
        for ti in sel:
            n = 0
            hits = collections.Counter()
            for t_ns, i, fc, stack, *_ in samples:
                if i != ti:
                    continue
                n += 1
                names = {sym.key_name(k) for k in keys(stack)}
                for pt in pats:
                    if any(pt in nm for nm in names):
                        hits[pt] += 1
            if n:
                print(f"\n=== {threads[ti]} share of samples (wall time; ms per frame over {nframes} frames)")
                for pt in pats:
                    f = hits[pt] / n
                    print(f"  {f * 100:5.1f}%  {f * meta['elapsed'] * 1000 / nframes:6.2f} ms/frame  {pt}")

    if args.callers or args.callees:
        pat = args.callers or args.callees
        for ti in sel:
            paths = collections.Counter()
            n = 0
            for t_ns, i, fc, stack, *_ in samples:
                if i != ti:
                    continue
                ks = keys(stack)
                idx = next((j for j, k in enumerate(ks) if pat in sym.key_name(k)), None)
                if idx is None:
                    continue
                n += 1
                if args.callers:
                    paths[tuple(ks[idx:idx + args.depth])] += 1
                else:
                    paths[tuple(reversed(ks[max(0, idx - args.depth):idx + 1]))] += 1
            if not n:
                continue
            tot_t = sum(1 for s in samples if s[1] == ti)
            print(f"\n=== {threads[ti]}: {n} samples ({n / max(1, tot_t) * 100:.1f}%) contain {pat!r}")
            for path, c in paths.most_common(args.top):
                print(f"  {c / n * 100:5.1f}%  " + (" <- " if args.callers else " -> ").join(
                    sym.key_name(k) for k in path))

    if args.folded:
        with open(args.folded, "w", encoding="utf-8") as fh:
            for line, n in folded.items():
                fh.write(f"{line} {n}\n")
        print(f"\nfolded stacks -> {args.folded}")


if __name__ == "__main__":
    main()
