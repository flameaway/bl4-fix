"""Private memory of a process split into GPU (write-combined), NT heaps, small and large blocks,
committed vs resident (read-only):  python dev/perf/mem_parts.py <pid> <default heap hex, from heap_owners.py>"""
import collections, sys, struct
sys.path.insert(0, 'dev/perf')
import vmmap, proc
pid = int(sys.argv[1]); heap_base = int(sys.argv[2], 16)
p = proc.Process(pid); h = p.h
allocs = collections.defaultdict(lambda: [0, set(), []])
for base, abase, size, state, prot, typ in vmmap.regions(h):
    if state == 0x1000 and typ == 0x20000:
        a = allocs[abase]; a[0] += size; a[1].add(prot); a[2].append((base, size))
g = collections.defaultdict(lambda: [0, 0, 0])
for ab, (comm, prots, regs) in allocs.items():
    kind = None
    if p.resident(ab):
        hdr = p.read(ab, 0x30)
        if len(hdr) == 0x30 and struct.unpack_from('<I', hdr, 0x10)[0] == 0xFFEEFFEE:
            kind = 'NT heap: default (malloc)' if struct.unpack_from('<Q', hdr, 0x28)[0] == heap_base else 'NT heap: other'
    if kind is None:
        if any(pr & 0x400 for pr in prots): kind = 'write-combined (GPU upload/readback)'
        elif any(pr & 0x200 for pr in prots): kind = 'uncached'
        elif comm < (1 << 20): kind = 'other <1MB (UE binned allocator etc.)'
        else: kind = 'other >=1MB'
    res = sum(vmmap.resident_bytes(h, b, s) for b, s in regs)
    x = g[kind]; x[0] += 1; x[1] += comm; x[2] += res
for k, (c, comm, res) in sorted(g.items(), key=lambda kv: -kv[1][1]):
    print(f'{k:40} {c:6d} allocs committed {comm/2**30:6.2f} GB resident {res/2**30:5.2f} GB')
