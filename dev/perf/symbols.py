"""
Names for the sampler's addresses. The shipping exe has no symbols, so names come from:

- UFunction native entries (`.perf/uobjects.json`, from dump_uobjects.py): `Class::execFoo` thunks.
- Class vtables (read from the live game): each slot is named after the most basic class that
  still has the same pointer there (`Actor::vt[42]`), so overrides show as `OakCharacter::vt[42]`.
- Hand-confirmed names in `dev/perf/names.json` (rva hex -> name), e.g. engine loop functions
  identified from their callers/strings.
- Strings the function references (`sub_1234 "SomeString"`), found with capstone on demand.
- Exports for system / driver DLLs (nearest export below the address).

    sym = Symbols(meta)            # meta from a capture; reads the live game if pid is running
    sym.name(addr)                 # "Borderlands4.exe!Actor::vt[42]" / "ntdll.dll!NtWaitForSingleObject+0x14"
"""

from __future__ import annotations

import bisect
import json
import os
import re
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PERF = REPO / ".perf"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "dev" / "re" / "pylib"))

import proc  # noqa: E402

DUMP = Path(os.environ.get("BL4_RE_DUMP", REPO / "dev" / "re" / "dump"))


class MainImage:
    """The dumped flat image (file offset == RVA), used for the exe's code and strings."""

    def __init__(self):
        import mmap
        meta = json.load(open(DUMP / "meta.json"))
        f = open(DUMP / "image.bin", "rb")
        self.img = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        self.secs = meta["sections"]
        text = next(s for s in self.secs if s["name"] == ".text")
        self.text = (text["va"], text["va"] + text["vsize"])
        pd = next(s for s in self.secs if s["name"] == ".pdata")
        raw = self.img[pd["va"]:pd["va"] + pd["vsize"]]
        fs = []
        for i in range(0, len(raw) - 11, 12):
            b, e, _ = struct.unpack_from("<III", raw, i)
            if b and e > b:
                fs.append((b, e))
        fs.sort()
        self.funcs = fs
        self.starts = [b for b, _ in fs]
        self.size = len(self.img)
        self._cs = None

    def func_start(self, rva: int) -> int | None:
        i = bisect.bisect_right(self.starts, rva) - 1
        if i >= 0 and self.funcs[i][0] <= rva < self.funcs[i][1]:
            return self.funcs[i][0]
        return None

    def func_range(self, start: int):
        i = bisect.bisect_left(self.starts, start)
        if i < len(self.funcs) and self.funcs[i][0] == start:
            return self.funcs[i]
        return None

    def string_at(self, rva: int) -> str | None:
        if not (0 < rva < self.size - 4):
            return None
        b = self.img[rva:rva + 200]
        # UTF-16LE
        m = re.match(rb"((?:[\x20-\x7e]\x00){4,})\x00\x00", b)
        if m:
            return m.group(1).decode("utf-16le")
        m = re.match(rb"([\x20-\x7e]{5,})\x00", b)
        if m:
            return m.group(1).decode()
        return None

    def strings_in(self, start: int, limit: int = 3) -> list[str]:
        """Strings referenced by `lea reg, [rip+x]` in the function (and its fragments)."""
        if self._cs is None:
            import capstone
            self._cs = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        rng = self.func_range(start)
        if not rng:
            return []
        end = min(rng[1], start + 0x3000)
        out = []
        code = self.img[start:end]
        for i in range(len(code) - 7):
            # 48/4C 8D 05/0D/15/1D/25/2D/35/3D disp32  == lea r64, [rip+disp32]
            if code[i] in (0x48, 0x4C) and code[i + 1] == 0x8D and (code[i + 2] & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", code, i + 3)[0]
                tgt = start + i + 7 + disp
                s = self.string_at(tgt)
                if s and s not in out:
                    out.append(s)
                    if len(out) >= limit:
                        break
        return out


class Symbols:
    def __init__(self, meta: dict, live: bool = True):
        self.meta = meta
        self.mods = sorted(meta["modules"])
        self.mod_starts = [m[0] for m in self.mods]
        self.main_base = meta["main_base"]
        self.main = MainImage()
        self.names: dict[int, str] = {}  # main exe function start rva -> name
        self._cache: dict[int, str] = {}
        self._ext: dict[str, list[tuple[int, str]]] = {}
        self.proc = None
        if live:
            try:
                self.proc = proc.Process(meta["pid"])
            except OSError:
                self.proc = None
        self._load_ufunctions()
        self._load_vtables()
        self._load_manual()

    # sources
    def _load_ufunctions(self):
        p = PERF / "uobjects.json"
        if not p.exists():
            return
        data = json.load(open(p))
        counts: dict[int, int] = {}
        for addr, _, _ in data["functions"]:
            counts[addr] = counts.get(addr, 0) + 1
        for addr, name, flags in data["functions"]:
            rva = addr - self.main_base
            if counts[addr] > 3:
                continue  # shared entry (script functions: ProcessInternal)
            cls, fn = name.split("::", 1)
            self.names.setdefault(rva, f"{cls}::exec{fn}")
        shared = sorted(((c, a) for a, c in counts.items() if c > 3), reverse=True)
        if shared:
            self.names[shared[0][1] - self.main_base] = "UObject::ProcessInternal (script VM)"
        self.uobjects = data

    def _load_vtables(self):
        cache = PERF / f"vtables_{self.meta['pid']}.json"
        if cache.exists():
            vt = json.load(open(cache))
        elif self.proc is not None and getattr(self, "uobjects", None):
            vt = self._read_vtables()
            json.dump(vt, open(cache, "w"))
        else:
            return
        sup = {c[0]: c[1] for c in self.uobjects["classes"]}

        def depth(c):
            d = 0
            while c in sup and sup[c] and d < 64:
                c = sup[c]
                d += 1
            return d

        # name each (slot, pointer) after the shallowest class that has it
        best: dict[int, tuple[int, str, int]] = {}
        for cls, table in vt.items():
            dp = depth(cls)
            for slot, ptr in enumerate(table):
                rva = ptr - self.main_base
                cur = best.get(rva)
                if cur is None or dp < cur[0]:
                    best[rva] = (dp, cls, slot)
        for rva, (_, cls, slot) in best.items():
            self.names.setdefault(rva, f"{cls}::vt[{slot}]")

    def _read_vtables(self):
        p = self.proc
        lo, hi = self.main.text
        out = {}
        for name, _, cdo in self.uobjects["classes"]:
            if not cdo:
                continue
            vptr = p.u64(cdo)
            if not vptr:
                continue
            raw = p.read(vptr, 8 * 400)
            table = []
            for i in range(0, len(raw), 8):
                v = struct.unpack_from("<Q", raw, i)[0]
                r = v - self.main_base
                if not (lo <= r < hi):
                    break
                table.append(v)
            if table:
                out[name] = table
        return out

    def _load_manual(self):
        p = HERE / "names.json"
        if p.exists():
            for k, v in json.load(open(p)).items():
                if not k.startswith("_"):
                    self.names[int(k, 16)] = v

    # lookup
    def module(self, addr: int):
        i = bisect.bisect_right(self.mod_starts, addr) - 1
        if i >= 0 and addr < self.mods[i][0] + self.mods[i][1]:
            return self.mods[i]
        return None

    def _exports(self, mod) -> list[tuple[int, str]]:
        name = mod[2]
        if name not in self._ext:
            ex = []
            if self.proc is not None:
                m = self.proc.module_of(mod[0])
                if m is not None:
                    ex = m.exports()
            self._ext[name] = ex
        return self._ext[name]

    def func_key(self, addr: int):
        """A stable id for the function containing addr: (module, function start rva)."""
        mod = self.module(addr)
        if mod is None:
            return ("?", addr)
        rva = addr - mod[0]
        if mod[0] == self.main_base:
            s = self.main.func_start(rva)
            return (mod[2], s if s is not None else rva)
        ex = self._exports(mod)
        i = bisect.bisect_right(ex, (rva, "￿")) - 1
        return (mod[2], ex[i][0] if i >= 0 else 0)

    def name(self, addr: int) -> str:
        key = self.func_key(addr)
        return self.key_name(key)

    def key_name(self, key) -> str:
        modname, start = key
        if modname == "?":
            return f"?{start:#x}"
        if modname.lower() == "borderlands4.exe":
            n = self.names.get(start)
            if n:
                return n
            hit = self._cache.get(start)
            if hit is None:
                strs = self.main.strings_in(start, 2)
                hit = f"sub_{start:x}" + (f" {strs!r}" if strs else "")
                self._cache[start] = hit
            return hit
        mod = next(m for m in self.mods if m[2] == modname)
        ex = self._exports(mod)
        i = bisect.bisect_left(ex, (start, ""))
        if i < len(ex) and ex[i][0] == start:
            return f"{modname}!{ex[i][1]}"
        return f"{modname}!{start:#x}" if start else f"{modname}"
