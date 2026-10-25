"""
Runs network experiment cases against a host (A) and client (B) connected through
tools/netlab_proxy.py, and prints one comparable row per case.

    python dev/netlab_run.py fps          # host fps caps vs update rate
    python dev/netlab_run.py conditions   # simulated internet conditions
    python dev/netlab_run.py budget       # replication budget (needs a busy scene)
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATS = REPO / ".bridge" / "netlab" / "stats.jsonl"
CONTROL = REPO / ".bridge" / "netlab" / "control.json"
SETTLE, MEASURE = 4, 10


def bridge(bid: str, code: str, timeout: float = 30) -> str:
    out = subprocess.run(
        [sys.executable, "-I", str(REPO / "tools" / "bridge.py"), bid, code, "--timeout", str(timeout)],
        capture_output=True, text=True, check=False,
    )
    return (out.stdout + out.stderr).strip()


def proxy_window(since: float) -> dict[str, float]:
    rows = []
    for line in STATS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["t"] >= since:
            rows.append(row)
    if not rows:
        return {}
    return {
        "s2c_pps": statistics.mean(r["s2c_pkts"] for r in rows),
        "s2c_Bps": statistics.mean(r["s2c_bytes"] for r in rows),
        "c2s_pps": statistics.mean(r["c2s_pkts"] for r in rows),
        "gap_avg": statistics.mean(r["s2c_gap_avg_ms"] or 0 for r in rows),
        "gap_max": max(r["s2c_gap_max_ms"] or 0 for r in rows),
    }


def run_case(name: str, host_code: str = "", client_code: str = "", conditions: dict | None = None) -> None:
    CONTROL.write_text(json.dumps(conditions or {"delay_ms": 0, "jitter_ms": 0, "loss": 0}), encoding="utf-8")
    if host_code:
        bridge("A", host_code)
    if client_code:
        bridge("B", client_code)
    time.sleep(SETTLE)
    bridge("A", "start_fps()")
    bridge("B", "start_track()")
    t0 = time.time()
    time.sleep(MEASURE)
    fps = bridge("A", "print(fps_report())")
    updates = bridge("B", "print(track_report()); print(freeze_report(120))")
    ping = bridge("B", "ps = get_pc().PlayerState; print(round(ps.GetPingInMilliseconds(), 1))")
    p = proxy_window(t0)
    print(f"\n=== {name}")
    print(f"  host: {fps}")
    if p:
        print(f"  wire: host->client {p['s2c_pps']:.0f} pkt/s {p['s2c_Bps'] / 1024:.1f} KB/s, gap avg "
              f"{p['gap_avg']:.1f} ms max {p['gap_max']:.0f} ms | client->host {p['c2s_pps']:.0f} pkt/s")
    print(f"  client ping (game's own figure): {ping} ms")
    print("  client receives position updates for:\n" + updates)


def main() -> None:
    which = sys.argv[1]
    bridge("A", "start_spin(2.0, walk=True)")
    try:
        if which == "fps":
            for cap in (0, 60, 30):
                run_case(f"host fps cap {cap or 'none'}", host_code=f"set_cvar('t.MaxFPS', {cap})")
            bridge("A", "set_cvar('t.MaxFPS', 0)")
        elif which == "conditions":
            for cond in (
                {"delay_ms": 0, "jitter_ms": 0, "loss": 0},
                {"delay_ms": 40, "jitter_ms": 0, "loss": 0},
                {"delay_ms": 40, "jitter_ms": 20, "loss": 0},
                {"delay_ms": 40, "jitter_ms": 20, "loss": 0.03},
            ):
                run_case(f"one-way delay {cond['delay_ms']}ms jitter ±{cond['jitter_ms']}ms loss {cond['loss']:.0%}",
                         conditions=cond)
        elif which == "budget":
            # 0.0001 starves replication on purpose: if the remote character gets choppy, the cvar
            # really caps what the host sends per frame (so raising it helps when fights hit it)
            for budget in (0.0025, 0.0001, 0.01):
                run_case(f"replication budget {budget * 1000:.1f} ms",
                         host_code=f"set_cvar('gbx.net.ReplicationBudgetSeconds', {budget})")
            bridge("A", "set_cvar('gbx.net.ReplicationBudgetSeconds', 0.0025)")
    finally:
        bridge("A", "stop_spin()")
        CONTROL.write_text(json.dumps({"delay_ms": 0, "jitter_ms": 0, "loss": 0}), encoding="utf-8")


if __name__ == "__main__":
    main()
