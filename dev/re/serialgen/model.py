"""Item generation rules from the inv NCS (our reading), checked against real rolls."""
import json, re
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
REF = re.compile(r"inv'([^']+)'")


def _tags(lst):
    return {k.lower() for d in lst or [] for k in d}


def _merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        if v not in (None, "", [], {}):
            out[k] = v
    return out


class Inv:
    def __init__(self):
        entries = json.loads((HERE / "inv_entries.json").read_text(encoding="utf-8"))
        self.root, self.parts, self.comps = {}, defaultdict(dict), defaultdict(dict)
        for e in entries:
            key = e["key"].lower()
            if e.get("value"):
                self.root[key] = _merge(self.root.get(key, {}), e["value"])
            for d in e.get("deps") or []:
                name = d["key"].lower()
                bucket = self.comps if d["table"] == "inv_comp" else self.parts
                old = bucket[key].get(name, {"table": d["table"], "value": {}})
                bucket[key][name] = {"table": d["table"], "value": _merge(old["value"], d["value"] or {})}
        self.by_index = {int(v["serialindex"]["index"]): k for k, v in self.root.items()
                         if (v.get("serialindex") or {}).get("_scope") == "Root"}

    def chain(self, key):
        out = []
        while key and key in self.root and key not in out:
            out.append(key)
            m = REF.match(self.root[key].get("basetype") or "")
            key = m.group(1).lower() if m else None
        return out

    def parttypes(self, key):
        for k in self.chain(key):
            if self.root[k].get("parttypes"):
                return [p.lower() for p in self.root[k]["parttypes"]]
        return []

    def comp_chain(self, key, comp):
        """[(owner type, comp record)], the comp first, then its basecompositions."""
        out = []
        owner = next((k for k in self.chain(key) if comp in self.comps[k]), None)
        while owner:
            rec = self.comps[owner][comp]["value"]
            out.append((owner, comp, rec))
            m = REF.match(rec.get("basecomposition") or "")
            if not m:
                break
            t, _, c = m.group(1).partition(".")
            owner, comp = t.lower(), c.lower()
            if comp not in self.comps.get(owner, {}):
                break
        return out

    def rules(self, key, comp):
        """{parttype: rule} merged base first, then the comp's own (later wins per field)."""
        merged = {}
        for _, _, rec in reversed(self.comp_chain(key, comp)):
            for pair in ((rec.get("parttypeselectionrules") or {}).get("pairs") or {}).values():
                pt = pair["key"].lower()
                merged[pt] = pair.get("value") or {}  # the most derived rule replaces the inherited one
        return merged

    def base_tags(self, key, comp):
        return set().union(*(_tags(rec.get("basetags")) for _, _, rec in self.comp_chain(key, comp)))

    def candidates(self, key, parttype):
        """[(owner type, part name, record)] for a part type, from the type and its ancestors."""
        return [(k, n, p["value"]) for k in self.chain(key) for n, p in self.parts[k].items() if p["table"].lower() == parttype]


def rule_count(rule):
    pc = rule.get("partcount") or {}
    lo = int(pc.get("min", 1)); hi = int(pc.get("max", max(lo, 1)))
    return lo, hi


def allowed(rule):
    """The slot's allowlist; None = any part. An emptied list ([""]) allows nothing."""
    if "parts" not in rule:
        return None
    return {p["part"].lower() for p in rule["parts"] or [] if isinstance(p, dict) and p.get("part")}


def ok_with(rec, tags):
    dep = _tags(rec.get("dependencytags"))
    return dep <= tags and not (_tags(rec.get("exclusiontags")) & tags)


def check(inv, roll):
    """Replays a real roll in serial order; returns the rules it breaks as modelled."""
    key = roll["type"]
    chosen = [(p[2].lower(), p[3]) for p in roll["parts"]]
    comp = next(n for t, n in chosen if t == "inv_comp")
    rules = inv.rules(key, comp)
    tags = inv.base_tags(key, comp)
    problems = []
    taken = defaultdict(list)
    for t, n in chosen:
        if t == "inv_comp":
            continue
        cands = {nn: r for _, nn, r in inv.candidates(key, t)}
        rec = cands.get(n)
        if rec is None:
            problems.append(("unknown_part", t, n)); continue
        allow = allowed(rules.get(t, {}))
        if allow is not None and n not in allow:
            problems.append(("not_allowed", t, n))
        dep = _tags(rec.get("dependencytags"))
        if not dep <= tags:
            problems.append(("dep", t, n, sorted(dep - tags)))
        exc = _tags(rec.get("exclusiontags")) & tags
        if exc:
            problems.append(("excl", t, n, sorted(exc)))
        tags |= _tags(rec.get("addtags"))
        taken[t].append(n)
    # Counts: a shortfall is fine when nothing else was pickable at the end
    for pt in inv.parttypes(key):
        rule = rules.get(pt, {})
        lo, hi = rule_count(rule)
        n = len(taken.get(pt, []))
        allow = allowed(rule)
        left = [nn for _, nn, r in inv.candidates(key, pt)
                if nn not in taken.get(pt, []) and (allow is None or nn in allow) and ok_with(r, tags)]
        if n > hi:
            problems.append(("count_high", pt, n, (lo, hi)))
        elif n < lo and left:
            problems.append(("count_low", pt, n, (lo, hi), len(left)))
    return problems


if __name__ == "__main__":
    inv = Inv()
    rolls = json.loads((HERE / "real_rolls.json").read_text())
    kinds = Counter(); examples = {}
    bad = 0
    for r in rolls:
        pr = check(inv, r)
        bad += bool(pr)
        for p in pr:
            k = (p[0], p[1]) if not p[0].startswith("count") else (p[0], p[1], p[2], p[3])
            kinds[k] += 1
            examples.setdefault(k, (r["type"], p))
    print(f"{bad}/{len(rolls)} rolls break a rule as modelled")
    for k, n in kinds.most_common(40):
        print(n, k, examples[k])
