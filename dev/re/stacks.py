# Capture native call stacks (game-module frames only) when teleport RPCs fire.
import ctypes, sys, time
from unrealsdk import hooks

_ar = [v for k, v in sys.modules.items() if k.endswith('bl4_direct.arrival')][0]
_BASE = int(_ar._kernel32.GetModuleHandleW(None))
_END = _BASE + 0x26e12000
_cap = ctypes.WinDLL('kernel32').RtlCaptureStackBackTrace
_cap.argtypes = (ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong))
_cap.restype = ctypes.c_ushort
STACKS = globals().setdefault('STACKS', [])


def _stack():
    frames = (ctypes.c_void_p * 62)()
    n = _cap(0, 62, frames, None)
    return [hex(f - _BASE) for f in frames[:n] if f and _BASE <= f < _END]


_P = '/Script/OakGame.OakPlayerController:'
for _fn in ('ServerNotifyCharacterReadyForGameplay', 'ServerNotifyTeleportScreenActive', 'ServerNotifyReadyForTeleport',
            'ClientNotifyTeleporting'):
    hooks.remove_hook(_P + _fn, hooks.Type.PRE, 'stk')
    hooks.add_hook(_P + _fn, hooks.Type.PRE, 'stk',
                   lambda o, a, r, f, _n=_fn: STACKS.append((round(time.time() % 1000, 2), _n, o.Name[-6:], _stack()[:14])))
print('stacks on')
