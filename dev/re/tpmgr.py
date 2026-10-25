# Inspect teleport-manager tickables found by heapscan (addresses in TPM_ADDRS).
import sys, struct
_ar = [v for k, v in sys.modules.items() if k.endswith('bl4_direct.arrival')][0]
_i32 = lambda a: struct.unpack('<i', _ar._read(a, 4))[0]
_q = lambda a: struct.unpack('<Q', _ar._read(a, 8))[0]
_by_index = {}


def _obj(index):
    if not _by_index:
        for o in unrealsdk.find_all('Object', exact=False):
            _by_index[o.InternalIndex] = o
    return _by_index.get(index)


def tpm(this):
    owner_idx, owner_serial = _i32(this - 0x18), _i32(this - 0x14)
    owner = _obj(owner_idx)
    data, count = _q(this + 0x210), _i32(this + 0x218)
    out = [f"tpm {this:#x} owner idx={owner_idx} -> {owner.Class.Name + ' ' + owner.Name if owner else None} records={count}"]
    for i in range(max(0, min(count, 8))):
        rec = data + i * 0x98
        raw = _ar._read(rec, 0x98)
        pc = _obj(_i32(rec + 0x10))
        out.append(f"  [{i}] pc={pc.Name if pc else None} raw={raw.hex()}")
    return '\n'.join(out)


for _a in TPM_ADDRS:
    print(tpm(_a))
