"""
Runs the co-op feel suite (dev/netlab_suite.py) across host settings. Host and client must already
be in one session (client through tools/netlab_proxy.py for a simulated link), the client standing
where it should shoot from, a Char_TargetDummy nearby.

    python dev/netlab_suite_run.py <host_id> <client_id> [--arms default,smooth,lagcomp,both]
        [--rounds 2] [--shoot 60] [--move 60] [--click-hz 3] [--dist 12]

Per arm: set the host's values, put the client back on its spot facing the dummy, then
- shoot phase: the dummy moves side to side dist m in front, the client aims at it on its own
  screen and fires; the host counts the client's shots and hits -> hit rate
- move phase: the client runs and jumps; the host counts corrections sent to it, the client counts
  snaps of its own character -> rubber-banding
One line per arm, plus .bridge/netlab/suite.jsonl. The host's original values come back at the end.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITE = (REPO / "dev" / "netlab_suite.py").as_posix()
OUT = REPO / ".bridge" / "netlab" / "suite.jsonl"

SMOOTH = {"gbx.nav.net.TransformPeriod": 0.0, "gbx.nav.net.PathPeriod": 0.0, "gbx.net.ReplicationBudgetSeconds": 0.005}
LAGCOMP = {
    # Bigger bullets for a laggy shooter (game: radius 0.2 -> 4.0 between 0 and 0.25 s of ping)
    "gbx.LagComp.ProjectileScalingMaxRadius": 20.0,
    "gbx.LagComp.ProjectileScalingMaxPing": 0.15,
    # More ping-based magnetism (game: 0 -> 5 degrees between 30 and 200 ms)
    "gbx.LightProjectile.LagCompMagnetismMaxAngle": 8.0,
    "gbx.LightProjectile.LagCompMagnetismMaxPing": 150.0,
}
GAME = {
    "gbx.nav.net.TransformPeriod": 0.25, "gbx.nav.net.PathPeriod": 0.25, "gbx.net.ReplicationBudgetSeconds": 0.0025,
    "gbx.LagComp.ProjectileScalingMaxRadius": 4.0, "gbx.LagComp.ProjectileScalingMaxPing": 0.25,
    "gbx.LightProjectile.LagCompMagnetismMaxAngle": 5.0, "gbx.LightProjectile.LagCompMagnetismMaxPing": 200.0,
}
ARMS = {
    "default": dict(GAME),
    "smooth": {**GAME, **SMOOTH},
    "lagcomp": {**GAME, **LAGCOMP},
    "both": {**GAME, **SMOOTH, **LAGCOMP},
    "trust": dict(GAME),  # + movement trust on the client's character (TRUST_ARMS)
    # Lag compensation split up, and a stronger version
    "radius": {**GAME, "gbx.LagComp.ProjectileScalingMaxRadius": 20.0, "gbx.LagComp.ProjectileScalingMaxPing": 0.15},
    "magnet": {**GAME, "gbx.LightProjectile.LagCompMagnetismMaxAngle": 8.0,
               "gbx.LightProjectile.LagCompMagnetismMaxPing": 150.0},
    "strong": {**GAME, "gbx.LagComp.ProjectileScalingMaxRadius": 40.0, "gbx.LagComp.ProjectileScalingMaxPing": 0.10,
               "gbx.LightProjectile.LagCompMagnetismMaxAngle": 12.0, "gbx.LightProjectile.LagCompMagnetismMaxPing": 100.0},
}
TRUST_ARMS = {"trust"}
SMOOTH_HOLD = "import sys; m = sys.modules.get('bl4_direct.smooth'); print(m is not None and (setattr(m, 'HOLD', {0}), {0} or m.restore()))"


def bridge(bid: str, code: str, timeout: float = 20) -> str:
    out = subprocess.run(
        [sys.executable, "-I", str(REPO / "tools" / "bridge.py"), bid, code, "--timeout", str(timeout)],
        capture_output=True, text=True, check=False,
    )
    return (out.stdout + out.stderr).strip()


def value(bid: str, expr: str) -> object:
    """Evaluates expr on the game thread and returns it (printed with repr, read back with literal_eval)."""
    text = bridge(bid, f"print(repr({expr}))")
    try:
        return ast.literal_eval(text.splitlines()[-1])
    except (ValueError, SyntaxError, IndexError) as ex:
        raise RuntimeError(f"{bid}: {expr} -> {text}") from ex


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("client")
    ap.add_argument("--arms", default="default,smooth,lagcomp,both")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--shoot", type=float, default=60)
    ap.add_argument("--move", type=float, default=60)
    ap.add_argument("--click-hz", type=float, default=3.0)
    ap.add_argument("--dist", type=float, default=12.0)
    ap.add_argument("--amp", type=float, default=None, help="dummy swing in cm (0 = standing still)")
    a = ap.parse_args()
    arms = a.arms.split(",") * a.rounds

    print(bridge(a.host, f"exec(open(r'{SUITE}').read())"))
    print(bridge(a.client, f"exec(open(r'{SUITE}').read())"))
    # BL4 Direct holds its smoother-enemies values on a host with friends: stand it down for the run
    print("smooth hold off", bridge(a.host, SMOOTH_HOLD.format(False)))
    original = value(a.host, f"suite_get({list(GAME)!r})")
    spot = value(a.client, "(lambda l: (l.X, l.Y, l.Z))(get_pc().Pawn.K2_GetActorLocation())")
    print("client spot", tuple(round(x) for x in spot), "| host values before", original, flush=True)
    try:
        for i, arm in enumerate(arms, 1):
            print(f"== {i}/{len(arms)} {arm}: {bridge(a.host, f'print(suite_set({ARMS[arm]!r}))')}", flush=True)
            print("   trust", bridge(a.host, f"print(suite_trust({arm in TRUST_ARMS}))"), flush=True)
            # shoot phase
            r = value(a.host, f"suite_mover_start({tuple(spot)!r}, {a.dist}, {a.amp!r})")
            if r[1] is None:
                raise SystemExit(f"mover: {r[0]}")
            base = r[1]
            bridge(a.host, f"suite_place_client({tuple(spot)!r}, {tuple(base)!r})")
            time.sleep(2)
            bridge(a.host, "suite_host_start()")
            bridge(a.client, f"suite_shoot_start({tuple(base)!r}, {a.click_hz})")
            time.sleep(a.shoot)
            shoot_c = value(a.client, "suite_client_report()")
            shoot_h = value(a.host, "suite_host_report()")
            bridge(a.client, "suite_client_stop()")
            bridge(a.host, "suite_mover_stop(); suite_host_stop()")
            # move phase
            bridge(a.host, "suite_host_start()")
            bridge(a.client, f"suite_move_start({spot[0]}, {spot[1]}, 1000)")
            time.sleep(a.move)
            move_c = value(a.client, "suite_client_report()")
            move_h = value(a.host, "suite_host_report()")
            bridge(a.client, "suite_client_stop()")
            bridge(a.host, "suite_host_stop()")
            shots = shoot_h["shots"] or 0
            hit_rate = shoot_h["hit_shots"] / shots if shots else 0
            row = {"t": time.time(), "arm": arm, "values": ARMS[arm], "shoot_client": shoot_c, "shoot_host": shoot_h,
                   "move_client": move_c, "move_host": move_h, "hit_rate": round(hit_rate, 3)}
            with OUT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            print(f"   shots that hit {shoot_h['hit_shots']}/{shots:.0f} = {hit_rate:.0%} (hits {shoot_h['hits']}, "
                  f"clicks {shoot_c['clicks']}, aimed "
                  f"{shoot_c['aimed_share']:.0%}) | moving: corrections {move_h['corr_per_min']}/min, own snaps "
                  f"{move_c['self_snaps_per_min']}/min (p50 {move_c['snap_p50_cm']} cm, max {move_c['snap_max_cm']}), "
                  f"jumps {move_c['jumps']} | host fps {shoot_h['host_fps']}/{move_h['host_fps']}", flush=True)
    finally:
        bridge(a.client, "suite_client_stop()")
        bridge(a.host, "suite_mover_stop(); suite_host_stop()")
        print("trust off", bridge(a.host, "print(suite_trust(False))"))
        print("restored", bridge(a.host, f"print(suite_set({original!r}))"))
        print("smooth hold on", bridge(a.host, SMOOTH_HOLD.format(True)))


if __name__ == "__main__":
    main()
