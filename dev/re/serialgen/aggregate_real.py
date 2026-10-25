"""aggregate_real.py <game_values.json>: aggregate features, real legendary rolls vs ours (same items)."""
import json, random, re, sys, types
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parents[2]
pkg = types.ModuleType("wdx_offline"); pkg.__path__ = [str(REPO / "mods/bl4_weapondex")]; sys.modules["wdx_offline"] = pkg
from wdx_offline import serialgen as G  # noqa: E402
R = json.loads((REPO / "mods/bl4_weapondex/rolls.json").read_text(encoding="utf-8")); G._file = lambda: R
V = json.loads(Path(sys.argv[1]).read_text())
res = lambda s: V.get(json.dumps(s, sort_keys=True), 1.0)  # noqa: E731
from real_rolls import inv_tables  # noqa: E402
roots, parts_t, _ = inv_tables()
real = json.loads((HERE / "real_rolls.json").read_text())


def features(names, slots):
    f = {}
    passives = [n for n in names if n.startswith("passive_")]
    if passives:
        groups = Counter(re.sub(r"_tier_\d$", "", n) for n in passives)
        f["cm skills"] = len(groups)
        f["cm top tier"] = max(groups.values())
    if any(s in ("barrel", "grip", "magazine") for s in slots):
        f["weapon: licensed parts"] = sum(1 for n in names if "licensed" in n or n.startswith(("part_mag_0", "part_grip_04", "part_grip_05", "part_underbarrel_0")) and any(x in n for x in ("_bor", "_cov", "_tor", "_hyp", "_ted", "_atlas", "ammoswitcher", "malswitch", "gauss", "discharge", "beam", "stungrenade")))
        f["weapon: has element"] = int(any(n in ("part_fire", "part_shock", "part_cryo", "part_corrosive", "part_radiation") for n in names))
        f["weapon: 2nd element"] = int(any(n.startswith("part_secondary_elem") for n in names))
        f["weapon: parts"] = len(names)
        f["weapon: ted licensed barrel"] = int(any(n.startswith("part_barrel_licensed_ted") for n in names))
        f["weapon: hyp licensed barrel"] = int(any(n.startswith("part_barrel_licensed_hyp") for n in names))
        f["weapon: jak licensed barrel"] = int(any(n.startswith("part_barrel_licensed_jak") for n in names))
        f["weapon: tor mag"] = int(any(n.startswith("part_mag_03_tor") for n in names))
        f["weapon: atlas ub"] = int(any(n.startswith("part_underbarrel_04_atlas") for n in names))
        f["weapon: ted grip"] = int(any(n.startswith("part_grip_05") for n in names))
    return f


def named(pairs):
    out = []
    for table, index in pairs:
        if isinstance(index, str):
            out.append((index, "text"))
        else:
            out.append((parts_t[table].get(index, ("?", "?"))[1], parts_t[table].get(index, ("?", "?"))[0]))
    return out


real_f, ours_f = {}, {}
rng = random.Random(5)
N = 50
for r in real:
    comp = next(p[3] for p in r["parts"] if p[2] == "inv_comp")
    key = f"{r['type']}.{comp}"
    if not G.has(key):
        continue
    names = [p[3] for p in r["parts"] if p[2] != "inv_comp"]
    for k, v in features(names, [p[2] for p in r["parts"]]).items():
        real_f.setdefault(k, Counter())[v] += 1
    prep = G.Prepared(key, 50, res)
    for _ in range(N):
        got = named(G.generate(prep, rng)[1:])
        for k, v in features([n for n, _ in got], [s for _, s in got]).items():
            ours_f.setdefault(k, Counter())[v] += 1
for k in sorted(real_f):
    a, b = real_f[k], ours_f.get(k, Counter())
    na, nb = sum(a.values()), sum(b.values())
    vals = sorted(set(a) | set(b))
    mean_a = sum(v * c for v, c in a.items()) / na
    mean_b = sum(v * c for v, c in b.items()) / max(1, nb)
    print(f"{k:24} real n={na:3} mean {mean_a:5.2f} | ours mean {mean_b:5.2f}")
    print("   " + "  ".join(f"{v}: {a[v] / na:.2f}/{b[v] / max(1, nb):.2f}" for v in vals))
