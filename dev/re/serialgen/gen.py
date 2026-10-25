"""Roll an item ourselves from the inv rules (model.py) and build its serial (build.py)."""
import json, random, sys
from pathlib import Path

from model import Inv, _tags, allowed, ok_with, rule_count
from build import build

HERE = Path(__file__).parent
VARBIT = json.loads((HERE / "varbit_types.json").read_text())


def chance(rule, attrs):
    a = rule.get("additionalpartchance") or {}
    if "constant" in a:
        return float(a["constant"])
    if a.get("attribute"):
        return attrs.get(a["attribute"].split("'")[1].lower(), 0.5)
    return 0.0


def generate(inv, key, comp, level=50, rng=None, attrs=None, weights=None):
    """[(table root index, part index)] in serial order, plus the part names. Uniform weights unless given."""
    rng = rng or random.Random()
    attrs = attrs or {}
    ti = int(inv.root[key]["serialindex"]["index"])
    owner = next(o for o, c, _ in inv.comp_chain(key, comp))
    comp_rec = inv.comps[owner][comp]["value"]
    out = [(ti, int(comp_rec["serialindex"]["index"]))]
    names = [("inv_comp", comp)]
    rules = inv.rules(key, comp)
    tags = inv.base_tags(key, comp)
    caps = [(_tags(r.get("tags")), int(r.get("max", 99)))
            for _, _, rec in inv.comp_chain(key, comp)[:1] for r in rec.get("parttagselectionrules") or []]
    if not caps:  # the nearest comp in the chain that has tag rules
        for _, _, rec in inv.comp_chain(key, comp):
            if rec.get("parttagselectionrules"):
                caps = [(_tags(r.get("tags")), int(r.get("max", 99))) for r in rec["parttagselectionrules"]]
                break
    capped = [0] * len(caps)
    for pt in inv.parttypes(key):
        rule = rules.get(pt, {})
        lo, hi = rule_count(rule)
        if rule.get("additionalpartchance"):
            want = lo
            while want < hi and rng.random() < chance(rule, attrs):
                want += 1
        else:
            want = rng.randint(lo, hi)
        allow = allowed(rule)
        picked = []
        for _ in range(want):
            pool = []
            for o, n, r in inv.candidates(key, pt):
                if n in picked or (allow is not None and n not in allow) or not ok_with(r, tags):
                    continue
                add = _tags(r.get("addtags"))
                if any(add & t and capped[i] >= m for i, (t, m) in enumerate(caps)):
                    continue
                pool.append((o, n, r, add))
            if not pool:
                break
            w = [(weights or {}).get((r.get("weight") or {}).get("attribute", ""), 1.0) for _, _, r, _ in pool]
            o, n, r, add = rng.choices(pool, weights=w)[0]
            picked.append(n)
            tags |= add
            for i, (t, m) in enumerate(caps):
                if add & t:
                    capped[i] += 1
            out.append((int(inv.root[o]["serialindex"]["index"]), int(r["serialindex"]["index"])))
            names.append((pt, n))
    return out, names


if __name__ == "__main__":
    inv = Inv()
    keys = sys.argv[1:] or ["vla_ar.comp_05_legendary_dualdamage", "mal_sg.comp_05_legendary_kaleidosplode",
                            "tor_shield.comp_05_legendary_firewerks", "classmod_exo_soldier.comp_05_legendary_06"]
    rng = random.Random(1)
    rolls = []
    for k in keys:
        key, comp = k.split(".")
        for _ in range(3):
            parts, names = generate(inv, key, comp, rng=rng)
            ti = int(inv.root[key]["serialindex"]["index"])
            varbit = VARBIT.get(str(ti), ti > 100)  # as the real serials of this type store it
            serial = build(ti, 50, rng.randrange(1, 4096), parts, varbit)
            rolls.append({"key": k, "serial": serial, "names": names})
            print(k, serial); print("   ", [n for _, n in names[1:]])
    (HERE / "gen_rolls.json").write_text(json.dumps(rolls, indent=1))
    # Same checker the real rolls passed
    from model import check
    from real_rolls import resolve, inv_tables
    roots, parts_t, _ = inv_tables()
    bad = 0
    for g in rolls:
        _, ti, res = resolve(g["serial"], roots, parts_t)
        pr = check(inv, {"type": g["key"].split(".")[0], "parts": res})
        if pr:
            bad += 1; print("BREAKS", g["key"], pr)
    print(f"{len(rolls) - bad}/{len(rolls)} generated rolls pass the checker")
