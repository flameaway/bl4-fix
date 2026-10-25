# Decode our generated serials in game, check the game kept every part, build their stats.
import ctypes, json, time
from collections import Counter
from ctypes import c_void_p, c_uint64
from mods_base import get_pc
from bl4_reroll import native as wdx_rn
from bl4_weapondex import stats as wdx_st

# Set WDX_BATCH (path of serialgen's gen_batch.json) in the bridge namespace before exec'ing this
class WdxGen:
    batch = json.load(open(WDX_BATCH))
    results = []
    ms = []

def wdx_gen_chunk(start, n):
    a = wdx_rn.api()
    wdx_st.resolve_defs(get_pc())
    get_stats = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(a.base + 0xC65C56)
    src = get_pc().PlayerState.BackpackItems.items[3]._get_address() + 0x18
    t0 = time.perf_counter()
    for g in WdxGen.batch[start:start + n]:
        ident = wdx_rn._Identity(src)
        try:
            src_c = c_uint64.from_address(src + 0x90).value
            ok = ident.load(g["serial"])
            c = c_uint64.from_address(ident.address + 0x90).value
            if ok and c == src_c:
                WdxGen.results.append((g["key"], "shared container")); continue
            if ok and not c:
                out = (c_uint64 * 2)()
                get_stats(ident.address, ctypes.addressof(out))
                ctl = c_uint64.from_address(ident.address + 0x98).value
                r = ctypes.c_int32.from_address(ctl + 8)
                if out[0] and r.value == 2:
                    r.value = 1
            want = Counter(tuple(p) for p in g["parts"][1:])
            got = Counter((t, i) for i, t in wdx_st.part_owners(ident.address))
            comp_ok = wdx_st.comp_index(ident.address) == g["parts"][0][1]
            vals = wdx_st.read_values(ident.address) or {}
            WdxGen.results.append((g["key"], "ok" if ok and comp_ok and want == got else "MISMATCH", len(vals),
                                   round(vals.get("weapon_damage", 0)), sorted((want - got).elements())[:4], sorted((got - want).elements())[:4]))
        except Exception as ex:
            WdxGen.results.append((g["key"], "error " + repr(ex)))
        finally:
            ident.destroy()
    WdxGen.ms.append((time.perf_counter() - t0) * 1000 / max(1, n))
