"""
Windows Firewall rule that limits who can reach a hosted game.

Steam's install already adds an "allow anything inbound" rule for Borderlands4.exe, so a narrow
allow rule would change nothing. Instead we add a *block* rule for the co-op port that covers every
address except the allowlist; Windows evaluates block rules ahead of allow rules.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

RULE_GROUP = "BL4 Direct"

LOOPBACK = ["127.0.0.0/8", "::1/128"]
# Home networks plus VPN overlays (Tailscale/ZeroTier use 100.64.0.0/10; only devices you've
# added to your own VPN network can have those addresses)
LOCAL_NETWORKS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "fe80::/10",
    "fc00::/7",
]

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _complement(allowed: Iterable[Network], version: int) -> list[str]:
    """Every address of the given IP version that's not in `allowed`, as 'first-last' ranges."""
    remaining: list[Network] = [ipaddress.ip_network("0.0.0.0/0" if version == 4 else "::/0")]
    for net in allowed:
        if net.version != version:
            continue
        next_remaining: list[Network] = []
        for block in remaining:
            if net.subnet_of(block):  # type: ignore[arg-type]
                next_remaining.extend(block.address_exclude(net))  # type: ignore[arg-type]
            elif block.subnet_of(net):  # type: ignore[arg-type]
                pass
            else:
                next_remaining.append(block)
        remaining = next_remaining

    ranges: list[list[ipaddress.IPv4Address | ipaddress.IPv6Address]] = []
    for net in sorted(ipaddress.collapse_addresses(remaining)):  # type: ignore[type-var]
        if ranges and int(ranges[-1][1]) + 1 == int(net.network_address):
            ranges[-1][1] = net.broadcast_address
        else:
            ranges.append([net.network_address, net.broadcast_address])
    return [f"{first}-{last}" for first, last in ranges]


def blocked_ranges(allowed_ips: Iterable[str], allow_lan: bool) -> list[str]:
    allowed = [ipaddress.ip_network(n) for n in LOOPBACK]
    if allow_lan:
        allowed += [ipaddress.ip_network(n) for n in LOCAL_NETWORKS]
    for ip in allowed_ips:
        try:
            allowed.append(ipaddress.ip_network(ip.strip()))
        except ValueError:
            # Not a valid address: leaving it out only means that "friend" stays blocked
            continue
    return _complement(allowed, 4) + _complement(allowed, 6)


APPLY_SCRIPT = r"""
param([string]$SpecFile)
$spec = Get-Content -Raw $SpecFile | ConvertFrom-Json
Remove-NetFirewallRule -Group $spec.group -ErrorAction SilentlyContinue
if ($spec.port -gt 0) {
    New-NetFirewallRule -DisplayName $spec.name -Group $spec.group -Description $spec.description `
        -Direction Inbound -Action Block -Protocol UDP -LocalPort $spec.port `
        -RemoteAddress $spec.blocked -Profile Any | Out-Null
}
"Rules in group: " + @(Get-NetFirewallRule -Group $spec.group -ErrorAction SilentlyContinue).Count
"""


def _run_ps(script: str, spec: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "apply.ps1"
        spec_path = Path(tmp) / "spec.json"
        script_path.write_text(script, encoding="utf-8")
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        res = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script_path), "-SpecFile", str(spec_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if res.returncode != 0:
        raise RuntimeError(f"firewall update failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def apply_rule(port: int, allowed_ips: Iterable[str], allow_lan: bool) -> str:
    """Replaces our firewall rule. Needs admin."""
    allowed_ips = list(allowed_ips)
    who = ", ".join(allowed_ips) or "no friends"
    return _run_ps(
        APPLY_SCRIPT,
        {
            "group": RULE_GROUP,
            "name": f"BL4 Direct - only friends may join (UDP {port})",
            "description": (
                f"Blocks inbound UDP {port} except from: loopback"
                + (", local network/VPN" if allow_lan else "")
                + f", {who}. Managed by the BL4 Direct app."
            ),
            "port": port,
            "blocked": blocked_ranges(allowed_ips, allow_lan),
        },
    )


def remove_rule() -> str:
    """Deletes our firewall rule. Needs admin."""
    return _run_ps(APPLY_SCRIPT, {"group": RULE_GROUP, "port": 0})


def rule_summary() -> str | None:
    """Description of the current rule, or None if there isn't one."""
    res = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            f"Get-NetFirewallRule -Group '{RULE_GROUP}' -ErrorAction SilentlyContinue"
            " | Select-Object -ExpandProperty Description",
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return res.stdout.strip() or None
