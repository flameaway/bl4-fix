# Online-status inputs of the local readiness check (0xe19c60), read live via the game's own getters.
import ctypes
_VP = ctypes.c_void_p
_gi_of_pc = ctypes.CFUNCTYPE(_VP, _VP)(_base + 0x47210f0)
_f6f32 = ctypes.CFUNCTYPE(_VP, _VP)(_base + 0xf6f32)
_d27df6 = ctypes.CFUNCTYPE(_VP, _VP, _VP, _VP)(_base + 0xd27df6)
_2d8968 = ctypes.CFUNCTYPE(ctypes.c_int, _VP, _VP)(_base + 0x2d8968)
_10d53f0 = ctypes.CFUNCTYPE(_VP, _VP, _VP)(_base + 0x10d53f0)


def online_chk(pc):
    a = pc._get_address()
    gi = _gi_of_pc(a)
    x = _f6f32(gi) if gi else None
    sub = _q(x + 0x68) if x else None
    out = {'pc': pc.Name[-6:], 'local': pc.IsLocalController(), 'gi': hex(gi or 0), 'f6f32': hex(x or 0), 'sub': hex(sub or 0)}
    if sub:
        h = ctypes.create_string_buffer(0x40)
        _d27df6(sub, ctypes.addressof(h), a)
        out['handle'] = h.raw[:8].hex()
        out['handle_valid(>=0)'] = struct.unpack('<h', h.raw[:2])[0] >= 0
        if out['handle_valid(>=0)']:
            out['status(want 0xb)'] = hex(_2d8968(sub, ctypes.addressof(h)))
    if gi:
        sp = ctypes.create_string_buffer(0x10)
        _10d53f0(ctypes.addressof(sp), gi)
        ptr, ref = struct.unpack('<QQ', sp.raw)
        out['10d53f0.ptr'] = hex(ptr)
    return out


for _pc in unrealsdk.find_all('OakPlayerController', exact=False):
    if not _pc.Name.startswith('Default__'):
        print(online_chk(_pc))
