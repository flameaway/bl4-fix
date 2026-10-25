"""xref.py <rva_hex>...: every rip-relative reference in .text to these RVAs (numpy scan + capstone check)."""
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re_tool as R  # noqa: E402
from capstone import x86  # noqa: E402

CHUNK = 1 << 24


def xrefs(targets):
    targets = np.array(sorted(targets), dtype=np.int64)
    out = {int(t): [] for t in targets}
    lo, hi = R.TLO, R.THI
    for start in range(lo, hi, CHUNK):
        end = min(hi, start + CHUNK + 16)
        b = np.frombuffer(R.IMG[start:end], dtype=np.uint8).astype(np.int64)
        n = len(b) - 4
        v = b[:n] | (b[1:n + 1] << 8) | (b[2:n + 2] << 16) | (b[3:n + 3] << 24)
        v = np.where(v >= 1 << 31, v - (1 << 32), v)
        pos = np.arange(start, start + n, dtype=np.int64)
        for k in (0, 1, 2, 4):  # bytes of immediate after the disp
            tgt = pos + 4 + k + v
            hit = np.nonzero(np.isin(tgt, targets))[0]
            for h in hit:
                p = int(pos[h]); t = int(tgt[h])
                for back in range(2, 9):
                    s = p - back
                    ins = next(R.CS.disasm(R.IMG[s:s + 16], s), None)
                    if ins is None or ins.address + ins.size <= p:
                        continue
                    if any(op.type == x86.X86_OP_MEM and op.mem.base == x86.X86_REG_RIP
                           and ins.address + ins.size + op.mem.disp == t for op in ins.operands):
                        out[t].append(ins)
                        break
    return out


if __name__ == "__main__":
    res = xrefs([int(a, 16) for a in sys.argv[1:]])
    for t, insns in res.items():
        print(f"== {t:#x}: {len(insns)}")
        seen = set()
        for ins in insns:
            if ins.address in seen:
                continue
            seen.add(ins.address)
            f = R.func_of(ins.address)
            print(f"  {R.fmt(ins)}   func {f[0]:#x}-{f[1]:#x}" if f else f"  {R.fmt(ins)}")
