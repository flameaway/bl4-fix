"""Who owns a heap: for every resident page of the NT-heap segments of <heap>, counts the qwords that
point into each loaded module (vtables, type objects). Read-only, ~1 min for 2.5 GB:
    python dev/perf/heap_owners.py <pid> <heap base hex>   (the default heap = PEB+0x30; heaps.py lists them)"""
import collections, sys, struct, ctypes
import numpy as np
sys.path.insert(0, 'dev/perf')
import vmmap, proc
pid = int(sys.argv[1]); heap_base = int(sys.argv[2], 16)
p = proc.Process(pid); h = p.h
allocs = collections.defaultdict(list)
for base, abase, size, state, prot, typ in vmmap.regions(h):
    if state == 0x1000 and typ == 0x20000:
        allocs[abase].append((base, size))
segs = []
for ab, regs in allocs.items():
    if not p.resident(ab): continue
    hdr = p.read(ab, 0x30)
    if len(hdr) == 0x30 and struct.unpack_from('<I', hdr, 0x10)[0] == 0xFFEEFFEE and struct.unpack_from('<Q', hdr, 0x28)[0] == heap_base:
        segs.append(regs)
mods = sorted((m.base, m.end, m.name) for m in p.modules)
starts = np.array([m[0] for m in mods], dtype=np.uint64); ends = np.array([m[1] for m in mods], dtype=np.uint64)
words = collections.Counter(); pages_dom = collections.Counter(); bytes_text = 0; pages = 0; zero_pages = 0
psapi = ctypes.WinDLL('psapi')
for regs in segs:
    for b, s in regs:
        n = s // 4096
        q = (ctypes.c_uint64 * (2 * n))()
        for i in range(n): q[2 * i] = b + 4096 * i
        psapi.QueryWorkingSetEx(h, q, 16 * n)
        for i in range(n):
            if not (q[2 * i + 1] & 1): continue
            d = p.read(b + 4096 * i, 4096)
            if len(d) < 4096: continue
            pages += 1
            a = np.frombuffer(d, dtype=np.uint64)
            if not a.any(): zero_pages += 1; continue
            idx = np.searchsorted(starts, a, side='right') - 1
            ok = (idx >= 0) & (a < ends[np.clip(idx, 0, None)])
            if ok.any():
                c = collections.Counter(idx[ok].tolist())
                for k, v in c.items(): words[mods[k][2]] += v
                pages_dom[mods[c.most_common(1)[0][0]][2]] += 1
            else:
                bb = np.frombuffer(d, dtype=np.uint8)
                if ((bb >= 32) & (bb < 127)).mean() > 0.8: pages_dom['<text>'] += 1
                else: pages_dom['<no module pointers>'] += 1
print('resident pages', pages, f'({pages*4096/2**30:.2f} GB), zero pages', zero_pages)
print('pages by module most pointed to:')
for k, v in pages_dom.most_common(20): print(f'  {k:32} {v:8d} pages {v*4096/2**20:8.0f} MB')
print('pointer words by module:')
for k, v in words.most_common(15): print(f'  {k:32} {v}')
