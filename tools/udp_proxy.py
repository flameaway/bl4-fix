"""
Logging UDP relay for watching the BL4 connection handshake.

    python tools/udp_proxy.py <listen_port> <target_host> <target_port> [log_file]

Clients send to 127.0.0.1:<listen_port>; every packet is forwarded to the target, and replies are
sent back to whichever client spoke last. Each packet is logged with direction, size and a hex
prefix.
"""

from __future__ import annotations

import select
import socket
import sys
import time


def main() -> None:
    listen_port, target_host, target_port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    log = open(sys.argv[4], "a", buffering=1, encoding="utf-8") if len(sys.argv) > 4 else sys.stdout  # noqa: SIM115

    front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    front.bind(("0.0.0.0", listen_port))
    back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    back.bind(("0.0.0.0", 0))
    target = (target_host, target_port)
    client: tuple[str, int] | None = None
    print(f"relay :{listen_port} -> {target}, back socket :{back.getsockname()[1]}", file=log)

    while True:
        ready, _, _ = select.select([front, back], [], [], 1.0)
        for sock in ready:
            data, addr = sock.recvfrom(65535)
            stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
            if sock is front:
                client = addr
                back.sendto(data, target)
                print(f"{stamp} C->S {len(data):5d} {data[:24].hex()}", file=log)
            else:
                if addr != target:
                    print(f"{stamp} ?? from {addr} {len(data)}", file=log)
                    continue
                if client:
                    front.sendto(data, client)
                print(f"{stamp} S->C {len(data):5d} {data[:24].hex()}", file=log)


if __name__ == "__main__":
    main()
