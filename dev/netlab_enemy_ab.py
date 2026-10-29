"""
A/B test of host-side settings for enemy smoothness on a co-op client.

Host = the game that hosts (bridge id, e.g. a PID), client = B (joined through tools/netlab_proxy.py).
Each arm: set the cvars on the host, keep B near the host, run dev/netlab_enemies.py on B and a
frame timer on the host for SECONDS, then print one block and append it to .bridge/netlab/enemy_ab.log.

    python dev/netlab_enemy_ab.py <host_id> <client_id> <seconds> default tuned default tuned
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATS = REPO / ".bridge" / "netlab" / "stats.jsonl"
LOG = REPO / ".bridge" / "netlab" / "enemy_ab.log"
PROBE = (REPO / "dev" / "netlab_enemies.py").as_posix()

ARMS = {
    "default": {"gbx.net.ReplicationBudgetSeconds": 0.0025, "gbx.nav.net.TransformPeriod": 0.25,
                "gbx.nav.net.PathPeriod": 0.25},
    "tuned": {"gbx.net.ReplicationBudgetSeconds": 0.005, "gbx.nav.net.TransformPeriod": 0.1,
              "gbx.nav.net.PathPeriod": 0.1},
    # No nav throttle: send an enemy's transform / path whenever replication runs
    "max": {"gbx.net.ReplicationBudgetSeconds": 0.005, "gbx.nav.net.TransformPeriod": 0.0,
            "gbx.nav.net.PathPeriod": 0.0},
}

HOST_SETUP = r"""
import time
_ABH_TICK = '/Script/Engine.CameraModifier:BlueprintModifyCamera'
def _abh_make():
    st = {'frames': [], 'last_call': 0.0, 'last_frame': 0.0}
    def tick(*_):
        now = time.perf_counter()
        gap = now - st['last_call']; st['last_call'] = now
        if gap < 0.004 and now - st['last_frame'] < 0.012:
            return
        st['last_frame'] = now; st['frames'].append(now)
    tick.st = st
    return tick
def abh_start():
    global ABH
    ABH = _abh_make()
    hooks.remove_hook(_ABH_TICK, hooks.Type.POST, 'netlab.ab_fps')
    hooks.add_hook(_ABH_TICK, hooks.Type.POST, 'netlab.ab_fps', ABH)
def abh_stop():
    hooks.remove_hook(_ABH_TICK, hooks.Type.POST, 'netlab.ab_fps')
    f = ABH.st['frames']
    g = sorted(b - a for a, b in zip(f, f[1:]))
    if len(g) < 3:
        return 'no frames'
    n = len(g)
    return (f'host fps avg {n / (f[-1] - f[0]):.0f}, frame p95 {g[int(n * .95)] * 1000:.0f} ms, p99 {g[int(n * .99)] * 1000:.0f} ms,'
            f' worst {g[-1] * 1000:.0f} ms, frames > 50 ms: {sum(1 for x in g if x > .05)}')
def abh_bring_client(max_m=25):
    import math
    me = get_pc().Pawn; ml = me.K2_GetActorLocation(); r = me.K2_GetActorRotation(); yaw = math.radians(r.Yaw)
    out = []
    for c in ENGINE.GameViewport.World.NetDriver.ClientConnections:
        p = c.PlayerController.Pawn if c.PlayerController else None
        if p is None:
            continue
        l = p.K2_GetActorLocation()
        d = math.dist((l.X, l.Y, l.Z), (ml.X, ml.Y, ml.Z)) / 100
        if d > max_m:
            p.K2_TeleportTo(unrealsdk.make_struct('Vector', X=ml.X - math.cos(yaw) * 300, Y=ml.Y - math.sin(yaw) * 300, Z=ml.Z + 60), r)
            out.append(f'moved client {d:.0f} m')
    return ', '.join(out) or 'client near'
def abh_set(values):
    KSL = unrealsdk.find_class('KismetSystemLibrary').ClassDefaultObject; pc = get_pc()
    for k, v in values.items():
        KSL.ExecuteConsoleCommand(pc, f'{k} {v}', pc)
    return {k: round(KSL.GetConsoleVariableFloatValue(k), 4) for k in values}
print('ab host ready')
"""


def bridge(bid: str, code: str, timeout: float = 20) -> str:
    out = subprocess.run(
        [sys.executable, "-I", str(REPO / "tools" / "bridge.py"), bid, code, "--timeout", str(timeout)],
        capture_output=True, text=True, check=False,
    )
    return (out.stdout + out.stderr).strip()


def proxy_window(since: float) -> str:
    rows = [json.loads(x) for x in STATS.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if r["t"] >= since]
    if not rows:
        return "proxy: no rows"
    n = len(rows)
    return (f"proxy: host->client {sum(r['s2c_bytes'] for r in rows) / n / 1024:.1f} KB/s, "
            f"{sum(r['s2c_pkts'] for r in rows) / n:.0f} pkt/s, worst gap {max((r['s2c_gap_max_ms'] or 0) for r in rows):.0f} ms, "
            f"dropped {sum(r['dropped'] for r in rows):.0f}")


def main() -> None:
    host, client, seconds, *arms = sys.argv[1:]
    seconds = float(seconds)
    print(bridge(host, HOST_SETUP))
    print(bridge(client, f"exec(open(r'{PROBE}').read())"))
    try:
        for i, arm in enumerate(arms, 1):
            print(f"== arm {i}/{len(arms)}: {arm}  set {bridge(host, f'print(abh_set({ARMS[arm]!r}))')}", flush=True)
            print("  " + bridge(host, "print(abh_bring_client())"), flush=True)
            time.sleep(5)  # settle
            t0 = time.time()
            bridge(client, "print(enemies_start())")
            bridge(host, "abh_start()")
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                time.sleep(min(10, max(0.0, end - time.monotonic())))
                moved = bridge(host, "print(abh_bring_client(40))")
                if moved != "client near":
                    print("  " + moved, flush=True)
            fps = bridge(host, "print(abh_stop())")
            rep = bridge(client, "print(enemies_report()); enemies_stop()")
            block = f"== {time.strftime('%H:%M:%S')} arm {i} {arm} {ARMS[arm]}\n{rep}\n  {fps}\n  {proxy_window(t0)}\n"
            print(block, flush=True)
            with LOG.open("a", encoding="utf-8") as f:
                f.write(block)
    finally:
        print("restored", bridge(host, f"print(abh_set({ARMS['default']!r}))"))
        bridge(client, "enemies_stop()")
        bridge(host, "abh_stop() if 'ABH' in globals() else None")


if __name__ == "__main__":
    main()
