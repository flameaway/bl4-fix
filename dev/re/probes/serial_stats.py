# Like serial_stats.py, with Weapondex's reader, keeping our identities alive to re-check later.
import ctypes
from ctypes import c_void_p, c_uint64
from mods_base import get_pc
from bl4_reroll import native as wdx_rn
from bl4_weapondex import stats as wdx_st

class WdxKeep:
    items = {}

def _wdx_u64(a):
    return c_uint64.from_address(a).value

def wdx_build(slot_index):
    a = wdx_rn.api()
    wdx_st.resolve_defs(get_pc())
    get_stats = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(a.base + 0xC65C56)
    src = get_pc().PlayerState.BackpackItems.items[slot_index]._get_address() + 0x18
    ident = wdx_rn._Identity(src)
    ok = ident.load(ident.serial())
    if not ok or _wdx_u64(ident.address + 0x90) == _wdx_u64(src + 0x90):
        print(slot_index, "not clean"); ident.destroy(); return
    out = (c_uint64 * 2)()
    if not _wdx_u64(ident.address + 0x90):
        get_stats(ident.address, ctypes.addressof(out))
        ctl = _wdx_u64(ident.address + 0x98)
        r = ctypes.c_int32.from_address(ctl + 8)
        if out[0] == _wdx_u64(ident.address + 0x90) and r.value == 2:
            r.value = 1
        else:
            print("refcount", r.value, "left alone")
    WdxKeep.items[slot_index] = (ident, src)
    wdx_compare(slot_index)

def wdx_compare(slot_index):
    ident, src = WdxKeep.items[slot_index]
    mine, game = wdx_st.read_values(ident.address) or {}, wdx_st.read_values(src) or {}
    diff = {k: (mine.get(k), game.get(k)) for k in set(mine) | set(game) if mine.get(k) != game.get(k)}
    print(slot_index, ident.serial()[:20], "mine", len(mine), "game", len(game), "diff", len(diff), list(diff.items())[:6])

def wdx_free():
    for ident, _ in WdxKeep.items.values():
        ident.destroy()
    WdxKeep.items = {}
    print("freed")
