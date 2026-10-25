# Per-frame recorder of a teleport manager's records (address in TPM). Logs raw records on change.
import time, struct
from unrealsdk import hooks
RLOG = globals().setdefault('RLOG', [])
_rl = {'sig': None}


def _rec_tick(*_):
    try:
        data, count = _q(TPM + 0x210), _i32(TPM + 0x218)
        recs = [_ar._read(data + i * 0x98, 0x98).hex() for i in range(min(max(count, 0), 8))]
        sig = (count, tuple(recs))
        if sig != _rl['sig']:
            _rl['sig'] = sig
            RLOG.append((round(time.time() % 1000, 3), count, recs))
    except Exception as e:  # noqa: BLE001
        RLOG.append((round(time.time() % 1000, 3), 'err', repr(e)[:100]))


hooks.remove_hook('/Script/Engine.CameraModifier:BlueprintModifyPostProcess', hooks.Type.POST, 'recrec')
hooks.add_hook('/Script/Engine.CameraModifier:BlueprintModifyPostProcess', hooks.Type.POST, 'recrec', _rec_tick)


def rshow():
    prev = None
    for t, c, recs in RLOG:
        if c == 'err':
            print(t, 'err', recs); continue
        print(t, 'count', c)
        for i, r in enumerate(recs):
            b = bytes.fromhex(r)
            if prev and i < len(prev):
                pb = bytes.fromhex(prev[i])
                diff = [f"+{j:#x}:{pb[j]:02x}->{b[j]:02x}" for j in range(len(b)) if b[j] != pb[j]]
                print('   ', i, 'changed', ' '.join(diff[:24]))
            else:
                print('   ', i, ' '.join(r[k:k + 16] for k in range(0, len(r), 16)))
        prev = recs


print('record recorder on', hex(TPM))
