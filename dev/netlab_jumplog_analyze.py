"""
Joins the host and client logs of dev/netlab_jumplog.py and explains each correction the host sent.

    python -I dev/netlab_jumplog_analyze.py <host.json> <client.json> [--detail N]

For each correction (host's ServerLastClientAdjustmentTime changed) it prints what the client and
the host were doing just before: movement mode (1 walk, 3 fall, 5 flying, 6 custom), jump count,
glide flags, mantle counter, time since the last Space press, and the client-vs-host position gap.
The host's copy of the client lags the client by about one link trip, so the gap is taken at the
time offset that fits the two paths best (found from the whole log).
"""

import bisect
import json
import math
import sys
from collections import Counter


def load(path):
    with open(path) as f:
        d = json.load(f)
    cols = {c: i for i, c in enumerate(d["cols"])}
    return d, cols


def interp(rows, ts, t):
    """Client position at time t, linear between the frames around it."""
    i = bisect.bisect_right(ts, t)
    if i <= 0 or i >= len(rows):
        return rows[min(max(i - 1, 0), len(rows) - 1)][1:4]
    a, b = rows[i - 1], rows[i]
    f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
    return [a[k] + (b[k] - a[k]) * f for k in (1, 2, 3)]


def at(rows, ts, t):
    """Row at or just before t."""
    i = bisect.bisect_right(ts, t) - 1
    return rows[max(i, 0)]


def main():
    host, hc = load(sys.argv[1])
    client, cc = load(sys.argv[2])
    detail = int(sys.argv[sys.argv.index("--detail") + 1]) if "--detail" in sys.argv else 15
    hr, cr = host["rows"], client["rows"]
    t_lo, t_hi = max(hr[0][0], cr[0][0]), min(hr[-1][0], cr[-1][0])
    hr = [r for r in hr if t_lo <= r[0] <= t_hi]
    cr = [r for r in cr if t_lo <= r[0] <= t_hi]
    hts, cts = [r[0] for r in hr], [r[0] for r in cr]
    span = t_hi - t_lo
    print(f"overlap {span:.1f} s, host rows {len(hr)} ({len(hr) / span:.0f}/s), client rows {len(cr)} ({len(cr) / span:.0f}/s)")

    # Offset: host shows the client's position `lag` seconds late. Pick the lag that fits best.
    def gap(r_h, lag):
        c = at(cr, cts, r_h[0] - lag)
        return math.dist(r_h[1:4], c[1:4])

    best = None
    for ms in range(0, 400, 5):
        lag = ms / 1000
        errs = sorted(gap(r, lag) for r in hr[::3])
        med = errs[len(errs) // 2]
        if best is None or med < best[1]:
            best = (lag, med)
    lag = best[0]
    print(f"host lags client by {lag * 1000:.0f} ms (median path gap {best[1]:.1f} cm)")

    spaces = [e[0] for e in client["events"] if e[1] == "space"]
    resps = [e[0] for e in client["events"] if e[1] == "resp"]
    print(f"space presses {len(spaces)}, move responses received {len(resps)} ({len(resps) / span:.1f}/s)")

    # Corrections and acks from the host log
    corr, acks = [], 0
    for a, b in zip(hr, hr[1:]):
        if b[hc["adj_t"]] != a[hc["adj_t"]]:
            corr.append(b)
        if b[hc["ack_t"]] != a[hc["ack_t"]]:
            acks += 1
    print(f"corrections {len(corr)} ({len(corr) / span * 60:.1f}/min), acks {acks} ({acks / span:.1f}/s)")

    # Client snaps: own position moved more than its velocity explains
    snaps = []
    for a, b in zip(cr, cr[1:]):
        dt = b[0] - a[0]
        if dt <= 0 or dt > 0.25:
            continue
        ex = (a[1] + a[4] * dt, a[2] + a[5] * dt, a[3] + a[6] * dt)
        err = math.dist(ex, b[1:4])
        if err > 50:
            snaps.append((b[0], err, a, b))
    print(f"client snaps > 50 cm: {len(snaps)} ({len(snaps) / span * 60:.1f}/min)")

    def describe(r, cols):
        return (f"mode {r[cols['mode']]}/{r[cols['cmode']]} jc {r[cols['jcount']]} press {r[cols['pressed']]} "
                f"glide {r[cols['glide']]}{r[cols['may_glide']]}{r[cols['glide_ready']]} mantle {r[cols['mantle_ctr']]} "
                f"df {r[cols['delayed_fall']]} spr {r[cols['sprint']]} vz {r[cols['vz']]:.0f}"
                + (f" wj {r[cols['was_jumping']]} pre {r[cols['jcount_pre']]} {r[cols['jtype']].replace('Movement.JumpType.', '')}"
                   + (f" sgr {r[cols['stop_glide_rel']]}" if "stop_glide_rel" in cols else "")
                   if "was_jumping" in cols else ""))

    def since_space(t):
        i = bisect.bisect_right(spaces, t) - 1
        return t - spaces[i] if i >= 0 else None

    # Classify each correction by the client state at the time the host's copy reflects
    kinds = Counter()
    rows_out = []
    for r in corr:
        t_client = r[0] - lag
        c = at(cr, cts, t_client)
        ss = since_space(t_client)
        g = gap(r, lag)
        key = (c[cc["mode"]], r[hc["mode"]], min(c[cc["jcount"]], 2))
        kinds[key] += 1
        rows_out.append((r[0], g, ss, describe(c, cc), describe(r, hc)))
    # Phase of each correction: just after a press, just after landing, in the air, or on the ground
    lands = [b[0] for a, b in zip(cr, cr[1:]) if a[cc["mode"]] == 3 and b[cc["mode"]] == 1]
    phases = Counter()
    for r in corr:
        t = r[0] - lag
        ss = since_space(t)
        i = bisect.bisect_right(lands, t) - 1
        sl = t - lands[i] if i >= 0 else None
        c = at(cr, cts, t)
        if ss is not None and ss < 0.3:
            phases["jump start (<300 ms after Space)"] += 1
        elif sl is not None and sl < 0.8:
            phases["after landing (<800 ms)"] += 1
        elif c[cc["mode"]] == 3:
            phases["in the air"] += 1
        else:
            phases["on the ground"] += 1
    print("\ncorrections by phase:", dict(phases.most_common()))
    print("corrections by (client mode, host mode, client jump count):")
    for k, n in kinds.most_common():
        print(f"  {k}: {n}")
    ss_all = [x[2] for x in rows_out if x[2] is not None]
    if ss_all:
        buckets = Counter(min(int(s * 10), 20) for s in ss_all)
        print("time since last Space (100 ms buckets, 20 = 2 s+):", dict(sorted(buckets.items())))
    print(f"\nfirst {detail} corrections:")
    for t, g, ss, cdesc, hdesc in rows_out[:detail]:
        sstr = f"{ss * 1000:5.0f}" if ss is not None else "  n/a"
        print(f"  t+{t - t_lo:6.2f}s gap {g:6.1f} space {sstr} ms | C {cdesc} | H {hdesc}")

    # Snaps vs corrections: does each client snap follow a correction?
    ct = [r[0] for r in corr]
    print(f"\nfirst {detail} client snaps (nearest correction before it):")
    for t, err, a, b in snaps[:detail]:
        i = bisect.bisect_right(ct, t) - 1
        prev = f"{(t - ct[i]) * 1000:5.0f} ms" if i >= 0 else "none"
        ss = since_space(t)
        sstr = f"{ss * 1000:5.0f}" if ss is not None else "  n/a"
        print(f"  t+{t - t_lo:6.2f}s snap {err:6.1f} cm, corr {prev} before, space {sstr} ms | before {describe(a, cc)} | after {describe(b, cc)}")

    # --window A B: every client frame in [t+A, t+B] next to the host's copy `lag` later
    if "--window" in sys.argv:
        i = sys.argv.index("--window")
        w0, w1 = t_lo + float(sys.argv[i + 1]), t_lo + float(sys.argv[i + 2])
        ct_set = set(round(t - lag, 2) for t in ct)
        print(f"\nwindow t+{w0 - t_lo:.2f}..{w1 - t_lo:.2f} (host shifted by {lag * 1000:.0f} ms; * = correction sent)")
        for e in client["events"]:
            if w0 <= e[0] <= w1 and e[1] == "space":
                print(f"  t+{e[0] - t_lo:6.3f} SPACE")
        prev_adj = None
        for h in hr:
            if not w0 <= h[0] <= w1:
                continue
            c = at(cr, cts, h[0] - lag)
            p = interp(cr, cts, h[0] - lag)
            mark = "*" if prev_adj is not None and h[hc["adj_t"]] != prev_adj else " "
            prev_adj = h[hc["adj_t"]]
            dxy = math.hypot(p[0] - h[1], p[1] - h[2])
            print(f" {mark}t+{h[0] - t_lo:6.3f} z C {p[2]:8.1f} H {h[3]:8.1f} dxy {dxy:5.1f} dz {p[2] - h[3]:6.1f} "
                  f"vxy C {math.hypot(c[4], c[5]):4.0f} H {math.hypot(h[4], h[5]):4.0f} "
                  f"| C {describe(c, cc)} | H {describe(h, hc)}")


if __name__ == "__main__":
    main()
