"""
Offline checks of mods/bl4_weapondex/serialgen.py (the mod's own generator):
1. its encoder rebuilds the real serials from the saves (real_rolls.json) byte for byte;
2. its rolls of every dex item pass the prototype's rule checker (model.check);
3. its serials decode with bl4_reroll/serial.py to exactly the parts it rolled.
Weights / gates / chances that need the game resolve to their attribute's stand-in (1, 0).
"""
import json, random, sys, types
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parents[2]
pkg = types.ModuleType("wdx_offline")
pkg.__path__ = [str(REPO / "mods" / "bl4_weapondex")]
sys.modules["wdx_offline"] = pkg
from wdx_offline import serialgen as G  # noqa: E402

_ROLLS = json.loads((REPO / "mods/bl4_weapondex/rolls.json").read_text(encoding="utf-8"))
G._file = lambda: _ROLLS  # (importlib.resources needs a real package)

sys.path.insert(0, str(REPO / "mods" / "bl4_reroll"))
import serial as S  # noqa: E402
from model import Inv, check  # noqa: E402
from real_rolls import inv_tables, resolve  # noqa: E402

# 1. Encoder vs real serials
rolls = json.loads((HERE / "real_rolls.json").read_text())
same = tokens = 0
diff = []
for r in rolls:
    s = S.decode(r["serial"])
    h = s.header()
    built = G.serial(s.item_type, h.get(1, 1), h.get(2, 0), [(p[0], p[1]) for p in r["parts"]])
    if built == r["serial"]:
        same += 1
    elif str(S.decode(built)) == str(s):
        tokens += 1
    else:
        diff.append((str(s), str(S.decode(built))))
print(f"1. {same} byte-identical, {tokens} same tokens, {len(diff)} different of {len(rolls)}")
for a, b in diff[:3]:
    print("   real ", a); print("   built", b)

# 1b. The game's own serials of class mods whose special stat has no serial index (dropped through
# SpawnInventoryFromCharacter 2026-10-24): that part is a text token "ClassMod.<part>"
GAME_TEXT = {
    "@Ug#2fK2}TYg3>VNSg$kum;ZW;St5G*n=?v<;L8oHK4$tS!H`z6(hv{>8d7KWjzw-3>Jw})5Uvjt/K9{TANdN":
        (255, 50, 44, [(255, 545), (255, 544), (255, 48), (255, 245), (255, 101), (255, 139), (255, 308), (255, 371),
                       (234, "ClassMod.statspecial_ricochet_damage"), (234, 67)]),
    "@UgeA!am/)}}!stq%xv4?DF{sd~KL&Neph_Clh=Xct(5V=*!}EFbO?J)cVfq/i9;d_Xuet6X)6;MFmmF?~&*f_O0R":
        (402, 50, 1114, [(402, 217), (402, 1), (402, 279), (402, 224), (402, 287), (402, 259), (402, 322), (402, 385),
                         (402, 342), (234, "ClassMod.statspecial_function_damage"), (234, 15)]),
}
print("1b.", sum(G.serial(*v) == k for k, v in GAME_TEXT.items()), "of", len(GAME_TEXT), "game serials with a text part rebuilt byte for byte")

# 2 + 3. Generated rolls
inv = Inv()
roots, parts_t, _ = inv_tables()


def resolve_all(serial):
    """real_rolls.resolve plus text parts: (table, "<type>.<part>", parttype, part name)."""
    s, ti, res = resolve(serial, roots, parts_t)
    # Rebuild in token order: Part tokens expand to resolve()'s rows, text tokens stay text
    rows = iter(res)
    out = []
    for t in S.decode(serial).tokens:
        if isinstance(t, S.Part):
            for _ in range(len(t.values) if t.values is not None else 1):
                out.append(next(rows))
        elif isinstance(t, S.Text):
            owner, _, name = t.text.partition(".")
            rec = inv.parts[owner.lower()][name.lower()]
            out.append((int(inv.root[owner.lower()]["serialindex"]["index"]), t.text, rec["table"], name.lower()))
    return s, out
dex = json.loads((REPO / "mods/bl4_weapondex/dex.json").read_text(encoding="utf-8"))
asked = Counter()


def stand_in(spec):
    asked[spec.get("a") or str(spec.get("t"))] += 1
    return G.product(spec, lambda a: 0.0 if "mingamestage" in a.lower() else 1.0, lambda *t: 1.0)


rng = random.Random(7)
bad = missing = decoded_bad = n = 0
for key in dex["items"]:
    if not G.has(key):
        missing += 1; print("   no rules:", key); continue
    for _ in range(20):
        serial, parts = G.roll(G.Prepared(key, 50, stand_in), 50, rng)
        n += 1
        s, res = resolve_all(serial)
        got = [(p[0], p[1]) for p in res]
        if got != parts or s.level != 50:
            decoded_bad += 1
            if decoded_bad < 4:
                print("   decode differs", key, parts, got)
        problems = check(inv, {"type": key.split(".")[0], "parts": res})
        if problems:
            bad += 1
            if bad < 6:
                print("   BREAKS", key, problems)
print(f"2. {n - bad}/{n} generated rolls pass the checker; {missing} dex items without rules")
print(f"3. {n - decoded_bad}/{n} decode to exactly the parts rolled")
print("   values asked:", len(asked))
