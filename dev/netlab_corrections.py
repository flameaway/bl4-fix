"""
Rubber-banding experiment: the client (B) walks in circles while the host (A) counts how many
movement corrections it sends, under different simulated connections and tolerances.

    python dev/netlab_corrections.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netlab_run import CONTROL, bridge  # noqa: E402

MEASURE = 15
BAD = {"delay_ms": 40, "jitter_ms": 20, "loss": 0.03}
CLEAN = {"delay_ms": 0, "jitter_ms": 0, "loss": 0}
NM = "world().NetworkManager"


def case(name: str, conditions: dict, tolerance_sq: float | None) -> None:
    CONTROL.write_text(json.dumps(conditions), encoding="utf-8")
    if tolerance_sq is not None:
        bridge("A", f"{NM}.MAXPOSITIONERRORSQUARED = {tolerance_sq}")
    time.sleep(3)
    bridge("A", "start_server_corr()")
    time.sleep(MEASURE)
    report = bridge("A", f"print(server_corr_report()); print('  tolerance (squared units):', {NM}.MAXPOSITIONERRORSQUARED)")
    ping = bridge("B", "print(round(get_pc().PlayerState.GetPingInMilliseconds(), 1))")
    print(f"\n=== {name}  (client ping {ping} ms)\n{report}")


def main() -> None:
    original = bridge("A", f"print({NM}.MAXPOSITIONERRORSQUARED)")
    print("host default MAXPOSITIONERRORSQUARED =", original)
    bridge("B", "start_spin(2.0, walk=True)")
    try:
        case("clean", CLEAN, None)
        case("delay 40ms each way only", {"delay_ms": 40, "jitter_ms": 0, "loss": 0}, None)
        case("delay 40ms, jitter ±20ms in order", {"delay_ms": 40, "jitter_ms": 20, "loss": 0}, None)
        case("delay 40ms, jitter ±20ms REORDERING", {"delay_ms": 40, "jitter_ms": 20, "loss": 0, "reorder": True}, None)
        case("delay 40ms, 3% loss only", {"delay_ms": 40, "jitter_ms": 0, "loss": 0.03}, None)
        bridge("A", f"{NM}.ClientAuthorativePosition = True")
        case("delay 40ms, jitter in order, 3% loss, CLIENT-AUTHORITATIVE position",
             {"delay_ms": 40, "jitter_ms": 20, "loss": 0.03}, None)
        bridge("A", f"{NM}.ClientAuthorativePosition = False")
        case("delay 40ms, jitter in order, 3% loss, default", {"delay_ms": 40, "jitter_ms": 20, "loss": 0.03}, None)
    finally:
        bridge("B", "stop_spin()")
        bridge("A", f"{NM}.MAXPOSITIONERRORSQUARED = {original}")
        bridge("A", f"{NM}.ClientAuthorativePosition = False")
        CONTROL.write_text(json.dumps(CLEAN), encoding="utf-8")


if __name__ == "__main__":
    main()
