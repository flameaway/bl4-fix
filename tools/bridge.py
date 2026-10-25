"""
Host-side client for the bl4_bridge mod.

    python tools/bridge.py <id> "<code>"            run on the game thread
    python tools/bridge.py <id> -f script.py        run a file on the game thread
    python tools/bridge.py <id> --thread "<code>"   run on the bridge thread
    python tools/bridge.py <id> --events            print the events log
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[1] / ".bridge"


def send(bridge_id: str, code: str, *, thread: bool = False, timeout: float = 30) -> str:
    base = BRIDGE_ROOT / bridge_id
    if not (base / "in").exists():
        raise SystemExit(f"no bridge folder for '{bridge_id}' (is the game running?)")
    name = f"c{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    suffix = ".thread.py" if thread else ".py"
    tmp = base / "in" / f"{name}.tmp"
    tmp.write_text(code, encoding="utf-8")
    tmp.replace(base / "in" / f"{name}{suffix}")

    out = base / "out" / f"{name}.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists():
            return out.read_text(encoding="utf-8")
        time.sleep(0.05)
    raise SystemExit(f"timed out after {timeout}s waiting for {out.name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("id")
    parser.add_argument("code", nargs="?")
    parser.add_argument("-f", "--file")
    parser.add_argument("--thread", action="store_true")
    parser.add_argument("--events", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_intermixed_args()

    if args.events:
        print((BRIDGE_ROOT / args.id / "events.log").read_text(encoding="utf-8"))
        return

    code = Path(args.file).read_text(encoding="utf-8") if args.file else args.code
    if code is None:
        code = sys.stdin.read()
    print(send(args.id, code, thread=args.thread, timeout=args.timeout), end="")


if __name__ == "__main__":
    main()
