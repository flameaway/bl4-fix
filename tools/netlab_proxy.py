"""
UDP relay for network experiments: per-second traffic stats, plus optional simulated internet
conditions (delay, jitter, loss), changeable while running.

    python tools/netlab_proxy.py <listen_port> <target_host> <target_port> <stats_file> [control_file]

Clients send to 127.0.0.1:<listen_port>. Every second a JSON line is appended to stats_file:
packets/bytes per second each way, and the average/max gap between server->client packets.
The control file (JSON, re-read each second) may set {"delay_ms": 40, "jitter_ms": 15, "loss": 0.02},
applied to both directions.
"""

from __future__ import annotations

import heapq
import json
import random
import select
import socket
import sys
import time
from pathlib import Path


def main() -> None:
    listen_port, target_host, target_port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    stats_path = Path(sys.argv[4])
    control_path = Path(sys.argv[5]) if len(sys.argv) > 5 else None

    front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    front.bind(("127.0.0.1", listen_port))
    back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Fixed outgoing port, so restarting the relay doesn't look like a new client to the host
    back.bind(("127.0.0.1", listen_port + 1000))
    target = (target_host, target_port)
    client: tuple[str, int] | None = None

    conditions = {"delay_ms": 0.0, "jitter_ms": 0.0, "loss": 0.0, "reorder": False}
    queue: list[tuple[float, int, str, bytes]] = []  # (release time, seq, direction, data)
    last_release = {"c2s": 0.0, "s2c": 0.0}
    seq = 0
    window = {"c2s_pkts": 0, "c2s_bytes": 0, "s2c_pkts": 0, "s2c_bytes": 0, "dropped": 0}
    s2c_gaps: list[float] = []
    last_s2c = None
    window_start = time.monotonic()

    def enqueue(direction: str, data: bytes) -> None:
        nonlocal seq
        if random.random() < conditions["loss"]:
            window["dropped"] += 1
            return
        delay = conditions["delay_ms"] + random.uniform(-1, 1) * conditions["jitter_ms"]
        release = time.monotonic() + max(0.0, delay) / 1000
        if not conditions.get("reorder"):
            # Real links jitter but rarely reorder: a packet never overtakes the previous one
            release = max(release, last_release[direction])
        last_release[direction] = release
        heapq.heappush(queue, (release, seq, direction, data))
        seq += 1

    while True:
        now = time.monotonic()
        timeout = 0.002 if queue else 0.05
        ready, _, _ = select.select([front, back], [], [], timeout)
        for sock in ready:
            try:
                data, addr = sock.recvfrom(65535)
            except ConnectionResetError:
                # Windows reports an ICMP "port unreachable" (a game closed its socket: a client
                # left, the host reloaded) as a reset on the next recvfrom. UDP has no connection.
                continue
            if sock is front:
                client = addr
                window["c2s_pkts"] += 1
                window["c2s_bytes"] += len(data) + 28
                enqueue("c2s", data)
            elif addr == target:
                now = time.monotonic()
                window["s2c_pkts"] += 1
                window["s2c_bytes"] += len(data) + 28
                if last_s2c is not None:
                    s2c_gaps.append(now - last_s2c)
                last_s2c = now
                enqueue("s2c", data)

        now = time.monotonic()
        while queue and queue[0][0] <= now:
            _, _, direction, data = heapq.heappop(queue)
            if direction == "c2s":
                back.sendto(data, target)
            elif client is not None:
                front.sendto(data, client)

        if now - window_start >= 1.0:
            span = now - window_start
            line = {
                "t": round(time.time(), 1),
                **{k: round(v / span, 1) for k, v in window.items()},
                "s2c_gap_avg_ms": round(1000 * sum(s2c_gaps) / len(s2c_gaps), 1) if s2c_gaps else None,
                "s2c_gap_max_ms": round(1000 * max(s2c_gaps), 1) if s2c_gaps else None,
                **conditions,
            }
            with stats_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
            window = dict.fromkeys(window, 0)
            s2c_gaps = []
            window_start = now
            if control_path and control_path.exists():
                try:
                    conditions.update(json.loads(control_path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass


if __name__ == "__main__":
    main()
