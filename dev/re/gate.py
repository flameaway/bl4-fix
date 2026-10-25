# Arrival gate inputs (see disassembly of 0x14dc7b2), read on the live game.
import sys, struct, ctypes
_ar = [v for k, v in sys.modules.items() if k.endswith('bl4_direct.arrival')][0]
_base = int(_ar._kernel32.GetModuleHandleW(None))
_q = lambda a: struct.unpack('<Q', _ar._read(a, 8))[0]
_b = lambda a: _ar._read(a, 1)[0]
_role = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_base + 0x487fd02)
_gi_of = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(_base + 0x3ca102)


def gate(pc):
    a = pc._get_address()
    vt = _q(a)
    v6c8 = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(_q(vt + 0x6c8))(a)
    gi = _gi_of(a)
    gi_state = _b(gi + 0x210) if gi else None
    return {'pc': pc.Name[-6:], 'local': pc.IsLocalController(), 'role_fn(=3)': _role(a), 'vt6c8': v6c8,
            'gi_state(=8)': gi_state, 'ready': _b(a + 0x32B9), 'tp_in_progress': _b(a + 0x3250),
            'pending': _b(a + 0x3251), 'b23bb': _b(a + 0x23bb), 'bf38': _b(a + 0xf38)}


def gates():
    return [gate(pc) for pc in unrealsdk.find_all('OakPlayerController', exact=False)
            if not pc.Name.startswith('Default__')]


print(gates())
