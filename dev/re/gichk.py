# GameInstance-side inputs of the client teleport handler (0x41ffea2), read live.
import ctypes
_gi_get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(_base + 0x497e828)
_f21f720 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(_base + 0x21f720)
_fd27a2a = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(_base + 0xd27a2a)


def _name(addr):
    for cn in ('Object',):
        for o in unrealsdk.find_all(cn, exact=False):
            if o._get_address() == addr:
                return o.Class.Name + ' ' + o.Name
    return hex(addr)


def gichk():
    pc = get_pc(); a = pc._get_address()
    gi = _gi_get(a)
    o21 = _f21f720(gi); od2 = _fd27a2a(gi)
    print('GI', _name(gi), 'state+0x210 =', _b(gi + 0x210), ' GI+0x448 =', _b(gi + 0x448))
    print('0x21f720(GI) ->', _name(o21) if o21 else None, ' +0xa1 =', _b(o21 + 0xa1) if o21 else None)
    print('0xd27a2a(GI) ->', _name(od2) if od2 else None, ' +0xb0 =', _b(od2 + 0xb0) if od2 else None)
    print('PC +0x23b8 =', _b(a + 0x23b8), '+0x23b9 =', _b(a + 0x23b9), '+0x23ba =', _b(a + 0x23ba), '+0x2368 =', _b(a + 0x2368),
          '+0x23bb =', _b(a + 0x23bb))


gichk()
