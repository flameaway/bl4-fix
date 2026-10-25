# Records arrival-gate inputs whenever they change, plus teleport RPCs, into GLOG.
# Needs gate.py exec'd first (gate/gates).
import time
from unrealsdk import hooks

GLOG = globals().setdefault('GLOG', [])
_last = {'sig': None, 't': 0.0}
_P = '/Script/OakGame.OakPlayerController:'


def _snap(tag):
    try:
        g = gates()
        w = world()
        sig = (w.Name if w else None, tuple(tuple(sorted(x.items())) for x in g))
        if tag != 'tick' or sig != _last['sig']:
            _last['sig'] = sig
            GLOG.append((round(time.time() % 1000, 2), tag, w.Name if w else None, g))
    except Exception as e:  # noqa: BLE001
        GLOG.append((round(time.time() % 1000, 2), 'err', tag, repr(e)[:120]))


def _tick(*_):
    now = time.monotonic()
    if now - _last['t'] >= 0.2:
        _last['t'] = now
        _snap('tick')


for _fn in ('ClientNotifyTeleporting', 'ServerNotifyTeleportScreenActive', 'ServerNotifyReadyForTeleport',
            'ServerNotifyCharacterReadyForGameplay', 'RequestTeleportToStation', 'ServerNotifyTeleportFailed'):
    hooks.remove_hook(_P + _fn, hooks.Type.POST, 'gaterec')
    hooks.add_hook(_P + _fn, hooks.Type.POST, 'gaterec', lambda o, a, r, f, _n=_fn: _snap(_n + ':' + o.Name[-6:]))
hooks.remove_hook('/Script/Engine.CameraModifier:BlueprintModifyPostProcess', hooks.Type.POST, 'gaterec')
hooks.add_hook('/Script/Engine.CameraModifier:BlueprintModifyPostProcess', hooks.Type.POST, 'gaterec', _tick)


def show(since=0):
    for t, tag, w, g in [x for x in GLOG if isinstance(x[0], float) and x[0] >= since]:
        if isinstance(g, list):
            print(t, tag, w, ' | '.join(f"{x['pc']}{'L' if x['local'] else 'R'} fn={x['role_fn(=3)']} gi={x['gi_state(=8)']} rdy={x['ready']} tp={x['tp_in_progress']} pend={x['pending']} 6c8={int(x['vt6c8'])} 23bb={x['b23bb']} f38={x['bf38']}" for x in g))
        else:
            print(t, tag, w, g)


print('recorder on')
