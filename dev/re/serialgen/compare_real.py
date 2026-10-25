"""
compare_real.py <game_values.json>: our rolls (mods/bl4_weapondex/serialgen.py, with the weights,
gates and chances the game gave, exported through the bridge) against the real legendary rolls in
the saves (real_rolls.json): how often each part shows up per slot, ours vs real, for the parts
where they differ most. Real counts are small (393 rolls over ~150 items), so only big gaps mean
anything.
"""
import json, random, sys, types
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parents[2]
pkg = types.ModuleType("wdx_offline")
pkg.__path__ = [str(REPO / "mods" / "bl4_weapondex")]
sys.modules["wdx_offline"] = pkg
from wdx_offline import serialgen as G  # noqa: E402

_ROLLS = json.loads((REPO / "mods/bl4_weapondex/rolls.json").read_text(encoding="utf-8"))
G._file = lambda: _ROLLS
VALUES = json.loads(Path(sys.argv[1]).read_text())
resolve = lambda spec: VALUES[json.dumps(spec, sort_keys=True)]  # noqa: E731

real = json.loads((HERE / "real_rolls.json").read_text())
N = 200
ours, theirs = defaultdict(Counter), defaultdict(Counter)  # (item type, slot) -> part name -> count
slots_n = Counter()
rng = random.Random(3)
for r in real:
    comp = next(p[3] for p in r["parts"] if p[2] == "inv_comp")
    key = f"{r['type']}.{comp}"
    if not G.has(key):
        continue
    for p in r["parts"]:
        if p[2] != "inv_comp":
            theirs[(r["type"], p[2])][p[3]] += 1
    slots_n[r["type"]] += 1
    prepared = G.Prepared(key, 50, resolve)
    names = {}
    for inv_parts in [_ROLLS]:
        pass
    for _ in range(N):
        for table, index in G.generate(prepared, rng)[1:]:
            ours[(r["type"], table, index)]["n"] += 1
# name our (table, index) pairs through the real rolls' own resolution table
from real_rolls import inv_tables  # noqa: E402
roots, parts_t, _ = inv_tables()
ours_named = defaultdict(Counter)
for (inv, table, index), c in ours.items():
    pt, name = parts_t[table].get(index, ("?", str(index))) if isinstance(index, int) else ("text", index)
    ours_named[(inv, pt)][name] += c["n"]
gaps = []
for slot in set(theirs) | set(ours_named):
    inv = slot[0]
    n_real, n_ours = slots_n[inv], slots_n[inv] * N
    for name in set(theirs[slot]) | set(ours_named[slot]):
        a = theirs[slot][name] / n_real
        b = ours_named[slot][name] / n_ours
        if n_real >= 5:
            gaps.append((abs(a - b), slot, name, round(a, 2), round(b, 2), n_real))
gaps.sort(reverse=True)
print("biggest gaps (share of rolls with the part: real vs ours, real rolls of that type):")
for g in gaps[:25]:
    print(f"  {g[1][0]:22} {g[1][1]:18} {g[2]:40} real {g[3]:.2f} ours {g[4]:.2f}  (n={g[5]})")
print("mean gap over", len(gaps), "parts:", round(sum(g[0] for g in gaps) / max(1, len(gaps)), 3))
# How surprising each gap is if our odds were right: binomial z of the real count
import math  # noqa: E402
zs = []
for _, slot, name, real_share, our_share, n in gaps:
    p_ = min(max(our_share, 0.5 / (n * N)), 1 - 0.5 / (n * N))
    zs.append(((real_share - p_) / math.sqrt(p_ * (1 - p_) / n), slot, name, real_share, our_share, n))
zs.sort(key=lambda z: -abs(z[0]))
print("parts with |z| > 3:", sum(1 for z in zs if abs(z[0]) > 3), "of", len(zs), "(chance alone: ~0.3%, more with small n)")
for z in zs[:12]:
    print(f"  z {z[0]:6.1f}  {z[1][0]:20} {z[1][1]:16} {z[2]:38} real {z[3]:.2f} ours {z[4]:.2f} (n={z[5]})")
