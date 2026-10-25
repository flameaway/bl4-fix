"""
End-to-end check of the BL4 Direct firewall rule, from a real second machine (over SSH).

Uses its own port and rule group, so the live "BL4 Direct" rule and the game's port are untouched.
An allow-everything rule on the test port stands in for Steam's Borderlands4.exe allow rule; the
block rule must win over it. Run elevated:  python dev/fwtest/run_test.py

Set these first (the second machine needs python3 and key-based SSH):
    FWTEST_SSH          user@host of the second machine
    FWTEST_REMOTE_LAN   the second machine's LAN IP (used as the "friend" address)
    FWTEST_LOCAL_LAN    this PC's LAN IP
    FWTEST_LOCAL_VPN    this PC's Tailscale/VPN IP
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "installer"))
import firewall  # noqa: E402

PORT = 7799
SSH_TARGET = os.environ["FWTEST_SSH"]
REMOTE_LAN = os.environ["FWTEST_REMOTE_LAN"]
THIS_PC = {"LAN": os.environ["FWTEST_LOCAL_LAN"], "Tailscale": os.environ["FWTEST_LOCAL_VPN"]}
TEST_GROUP = "BL4 Direct TEST"
ALLOW_GROUP = "BL4 Direct TEST allow"

PROBE = """
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
s.sendto(b'probe', (sys.argv[1], int(sys.argv[2])))
try:
    s.recvfrom(100); print('ALLOWED')
except socket.timeout:
    print('BLOCKED')
"""


def ps(command: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True, capture_output=True)


def set_block_rule(allowed: list[str] | None, allow_lan: bool = False) -> None:
    if allowed is None:
        firewall._run_ps(firewall.APPLY_SCRIPT, {"group": TEST_GROUP, "port": 0})  # noqa: SLF001
        return
    firewall._run_ps(firewall.APPLY_SCRIPT, {  # noqa: SLF001
        "group": TEST_GROUP,
        "name": f"BL4 Direct TEST block (UDP {PORT})",
        "description": "temporary test rule",
        "port": PORT,
        "blocked": firewall.blocked_ranges(allowed, allow_lan),
    })
    time.sleep(1.0)


def probe() -> dict[str, str]:
    results = {}
    for label, dst in THIS_PC.items():
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", SSH_TARGET, "python3", "-c", f'"{PROBE}"', dst, str(PORT)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        results[label] = (out.stdout.strip() or out.stderr.strip())[-40:]
    return results


def main() -> int:
    ps(
        f"New-NetFirewallRule -DisplayName 'BL4 Direct TEST allow {PORT}' -Group '{ALLOW_GROUP}' "
        f"-Direction Inbound -Action Allow -Protocol UDP -LocalPort {PORT} -Profile Any | Out-Null",
    )
    echo = subprocess.Popen([sys.executable, "-I", str(HERE / "echo.py"), str(PORT)])
    time.sleep(1.0)

    cases = [
        ("1 control: no block rule", None, False, {"LAN": "ALLOWED", "Tailscale": "ALLOWED"}),
        ("2 block, no friends, LAN off", [], False, {"LAN": "BLOCKED", "Tailscale": "BLOCKED"}),
        ("3 friend = remote LAN IP", [REMOTE_LAN], False, {"LAN": "ALLOWED", "Tailscale": "BLOCKED"}),
        ("4 no friends, LAN/VPN on", [], True, {"LAN": "ALLOWED", "Tailscale": "ALLOWED"}),
    ]
    failures = 0
    try:
        for name, allowed, allow_lan, expected in cases:
            set_block_rule(allowed, allow_lan)
            got = probe()
            ok = got == expected
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got}" + ("" if ok else f", expected {expected}"))
    finally:
        set_block_rule(None)
        ps(f"Remove-NetFirewallRule -Group '{ALLOW_GROUP}' -ErrorAction SilentlyContinue")
        echo.kill()
    print("cleaned up test rules")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
