"""Build serials from part lists; round-trip check against real rolls."""
import json, sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mods" / "bl4_reroll"))
import serial as S  # noqa: E402
from model import Inv  # noqa: E402


def part_tokens(ti, parts):
    """parts: [(table root index, part index)] in serial order -> Part tokens."""
    out = []
    i = 0
    while i < len(parts):
        t, v = parts[i]
        if t == ti:
            out.append(S.Part(v)); i += 1; continue
        j = i
        while j < len(parts) and parts[j][0] == t:
            j += 1
        run = [p[1] for p in parts[i:j]]
        out.append(S.Part(t, value=run[0]) if len(run) == 1 else S.Part(t, values=[S.Num(x) for x in run]))
        i = j
    return out


def build(ti, level, seed, parts, varbit_type=False):
    toks = [S.Num(ti, varbit=varbit_type), S.Sep(False), S.Num(0), S.Sep(False), S.Num(1), S.Sep(False), S.Num(level),
            S.Sep(True), S.Num(2), S.Sep(False), S.Num(seed), S.Sep(True), S.Sep(True)]
    toks += part_tokens(ti, parts)
    toks.append(S.Sep(True))
    return S.encode(S.Serial(toks))


if __name__ == "__main__":
    rolls = json.loads((HERE / "real_rolls.json").read_text())
    same = text_same = 0
    diffs = []
    for r in rolls:
        s = S.decode(r["serial"])
        ti = s.item_type
        first = next(t for t in s.tokens if isinstance(t, S.Num))
        h = s.header()
        parts = [(p[0], p[1]) for p in r["parts"]]
        try:
            built = build(ti, h.get(1, 1), h.get(2, 0), parts, first.varbit)
        except Exception as ex:
            diffs.append((r["serial"], str(ex))); continue
        if built == r["serial"]:
            same += 1
        elif str(S.decode(built)) == str(s):
            text_same += 1
        else:
            diffs.append((str(s), str(S.decode(built))))
    print(f"{same} byte-identical, {text_same} same tokens (bit widths differ), {len(diffs)} different of {len(rolls)}")
    for d in diffs[:8]:
        print("  real ", d[0]); print("  built", d[1])
