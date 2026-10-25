"""
Offline helpers over the dumped flat image (file offset == RVA), made by ext_dump.py.

Needs capstone: `pip install --target dev/re/pylib capstone` (or set PYTHONPATH yourself).
The dump directory defaults to dev/re/dump; override with BL4_RE_DUMP.
"""
import bisect, json, mmap, os, re, struct, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "pylib"))
import capstone  # noqa: E402
from capstone import x86  # noqa: E402

D = os.environ.get("BL4_RE_DUMP", os.path.join(os.path.dirname(__file__), "dump"))
META = json.load(open(os.path.join(D, "meta.json")))
_f = open(os.path.join(D, "image.bin"), "rb")
IMG = mmap.mmap(_f.fileno(), 0, access=mmap.ACCESS_READ)
SECS = {s["name"]: s for s in META["sections"]}
TEXT = SECS[".text"]
TLO, THI = TEXT["va"], TEXT["va"] + TEXT["vsize"]

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
CS.detail = True

# function table from .pdata
_pd = SECS[".pdata"]
_raw = IMG[_pd["va"]:_pd["va"] + _pd["vsize"]]
FUNCS = []
for i in range(0, len(_raw) - 11, 12):
    b, e, u = struct.unpack_from("<III", _raw, i)
    if b == 0:
        break
    FUNCS.append((b, e))
FUNCS.sort()
_starts = [b for b, _ in FUNCS]


def func_of(rva):
    i = bisect.bisect_right(_starts, rva) - 1
    if i >= 0 and FUNCS[i][0] <= rva < FUNCS[i][1]:
        # walk back over chained fragments: a fragment that starts right after a previous one
        return FUNCS[i]
    return None


def disasm(start, end=None, max_insns=400):
    end = end or start + 0x2000
    out = []
    for ins in CS.disasm(IMG[start:end], start):
        out.append(ins)
        if len(out) >= max_insns:
            break
    return out


def fmt(ins):
    return f"{ins.address:#010x}: {ins.mnemonic:7} {ins.op_str}"


def call_targets(insns):
    out = []
    for ins in insns:
        if ins.mnemonic in ("call", "jmp") and ins.operands and ins.operands[0].type == x86.X86_OP_IMM:
            out.append((ins.address, ins.mnemonic, ins.operands[0].imm))
    return out


def find_disp(disp, lo=TLO, hi=THI):
    """Every instruction in .text with a memory operand displacement == disp."""
    needle = struct.pack("<i", disp)
    hits = []
    for m in re.finditer(re.escape(needle), IMG[lo:hi]):
        pos = lo + m.start()
        for back in range(2, 9):
            s = pos - back
            ins = next(CS.disasm(IMG[s:s + 16], s), None)
            if ins is None or ins.address + ins.size <= pos:
                continue
            if any(op.type == x86.X86_OP_MEM and op.mem.disp == disp for op in ins.operands):
                hits.append(ins)
                break
    return hits


def find_calls_to(target, lo=TLO, hi=THI):
    """Direct E8 rel32 calls to target."""
    hits = []
    data = IMG[lo:hi]
    i = data.find(b"\xE8")
    # faster: scan for each possible rel32 is hard; brute force over E8 bytes
    while i != -1:
        rel = struct.unpack_from("<i", data, i + 1)[0] if i + 5 <= len(data) else None
        if rel is not None and lo + i + 5 + rel == target:
            hits.append(lo + i)
        i = data.find(b"\xE8", i + 1)
    return hits


if __name__ == "__main__":
    print("funcs", len(FUNCS), "text", hex(TLO), hex(THI))
