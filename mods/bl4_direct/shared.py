"""
State shared between the in-game mod and the BL4 Direct companion app.

Kept free of any SDK imports so the app can import it as plain Python. The friends file lives in
the SDK's settings folder; the app writes it, the mod reads it (and may update `last_joined`).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

MOD_VERSION = "0.5.2"
SDK_VERSION = "0.3"
DEFAULT_PORT = 7777
FRIENDS_FILE_NAME = "bl4_direct_friends.json"
STEAM_APP_ID = "1285190"


@dataclass
class Friend:
    name: str
    address: str
    # The port to use when joining this friend's game (their host port)
    port: int = DEFAULT_PORT


@dataclass
class Config:
    host_port: int = DEFAULT_PORT
    # Also let anyone on your local network join your hosted game (firewall allowlist)
    allow_lan: bool = True
    friends: list[Friend] = field(default_factory=list)
    # Keep a firewall rule that blocks the host port to everyone but the allowlist. Off (the default
    # on a new install; the setup app recommends it on): no rule at all (the setup app removes
    # ours), and hosting never asks Windows for admin
    firewall: bool = False
    # What the firewall rule was last built from (see firewall_signature); written by whoever applied
    # it (the setup app, or the mod through its Update button)
    firewall_applied: str = ""

    def firewall_signature(self) -> str:
        """Everything the hosting firewall rule depends on. Only the host needs the rule."""
        if not self.firewall:
            return "off"
        ips = sorted({f.address for f in self.friends if is_ipv4(f.address)})
        return f"{self.host_port}|{self.allow_lan}|{','.join(ips)}"

    def firewall_up_to_date(self) -> bool:
        return self.firewall_applied == self.firewall_signature()

    def firewall_spec(self) -> dict:
        """What FIREWALL_SCRIPT needs to make our rule match this config (port 0: no rule)."""
        if not self.firewall:
            return {"group": FIREWALL_GROUP, "port": 0}
        ips = list(dict.fromkeys(f.address for f in self.friends if is_ipv4(f.address)))
        return firewall_rule(self.host_port, ips, self.allow_lan)

    @staticmethod
    def load(path: Path) -> Config:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Config()
        if not isinstance(raw, dict):
            return Config()
        # A hand-edited file must never stop the mod from loading: skip what doesn't make sense
        friends = []
        entries = raw.get("friends")
        for f in entries if isinstance(entries, list) else []:
            try:
                if isinstance(f, dict) and f.get("name") and f.get("address"):
                    address = str(f["address"])
                    friends.append(Friend(str(f["name"]), normalize_ipv4(address) or address,
                                          int(f.get("port", DEFAULT_PORT))))
            except (TypeError, ValueError):
                continue
        try:
            host_port = int(raw.get("host_port", DEFAULT_PORT))
        except (TypeError, ValueError):
            host_port = DEFAULT_PORT
        return Config(
            host_port=host_port,
            allow_lan=bool(raw.get("allow_lan", True)),
            friends=friends,
            # Files from before the switch existed: on if the setup app had made the rule
            firewall=bool(raw.get("firewall", bool(raw.get("firewall_applied")))),
            firewall_applied=str(raw.get("firewall_applied", "")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=4), encoding="utf-8")
        tmp.replace(path)


def normalize_ipv4(address: str) -> str | None:
    """
    Canonical form of a dotted IPv4 address, or None if it isn't one.

    Leading zeros are dropped ("21.142.145.01" -> "21.142.145.1"): people type them, but Python's
    ipaddress (used for the firewall rule) rejects them.
    """
    parts = address.strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 < len(p) <= 3 for p in parts):
        return None
    nums = [int(p) for p in parts]
    if any(n > 255 for n in nums):
        return None
    return ".".join(map(str, nums))


def is_ipv4(address: str) -> bool:
    return normalize_ipv4(address) is not None


def game_build_id(game_dir: Path) -> str:
    """
    Identifies the installed game build, so two players can check they match.

    Uses Steam's build id when the game is a Steam install, otherwise a hash of the exe's size.
    """
    manifest = game_dir.parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    try:
        if match := re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8")):
            return match.group(1)
    except OSError:
        pass
    exe = game_dir / "OakGame" / "Binaries" / "Win64" / "Borderlands4.exe"
    try:
        return "x" + hashlib.sha1(str(exe.stat().st_size).encode()).hexdigest()[:8]
    except OSError:
        return "unknown"


def compat_code(game_dir: Path) -> str:
    """A short string both players can compare: game build, SDK and mod versions."""
    return f"game {game_build_id(game_dir)} / sdk {SDK_VERSION} / mod {MOD_VERSION}"


# region Firewall rule
#
# Steam's install already adds an "allow anything inbound" rule for Borderlands4.exe, so a narrow
# allow rule would change nothing. Instead we add a *block* rule for the co-op port that covers every
# address except the allowlist; Windows evaluates block rules ahead of allow rules. The setup app
# (installer/firewall.py) and the mod (the page's Update button, Host) both apply it from here.

FIREWALL_GROUP = "BL4 Direct"

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


def _complement(allowed: list, version: int) -> list[str]:
    """Every address of the given IP version that's not in `allowed`, as 'first-last' ranges."""
    remaining = [ipaddress.ip_network("0.0.0.0/0" if version == 4 else "::/0")]
    for net in allowed:
        if net.version != version:
            continue
        next_remaining = []
        for block in remaining:
            if net.subnet_of(block):
                next_remaining.extend(block.address_exclude(net))
            elif block.subnet_of(net):
                pass
            else:
                next_remaining.append(block)
        remaining = next_remaining

    ranges: list[list] = []
    for net in sorted(ipaddress.collapse_addresses(remaining)):
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


def firewall_rule(port: int, allowed_ips: Iterable[str], allow_lan: bool) -> dict:
    """The spec FIREWALL_SCRIPT turns into our block rule."""
    allowed_ips = list(allowed_ips)
    who = ", ".join(allowed_ips) or "no friends"
    return {
        "group": FIREWALL_GROUP,
        "name": f"BL4 Direct - only friends may join (UDP {port})",
        "description": (
            f"Blocks inbound UDP {port} except from: loopback"
            + (", local network/VPN" if allow_lan else "")
            + f", {who}. Managed by the BL4 Direct app."
        ),
        "port": port,
        "blocked": blocked_ranges(allowed_ips, allow_lan),
    }


# Runs elevated with `$spec` set to a firewall_spec(): replaces our rule (port 0: removes it)
FIREWALL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Remove-NetFirewallRule -Group $spec.group -ErrorAction SilentlyContinue
if ($spec.port -gt 0) {
    New-NetFirewallRule -DisplayName $spec.name -Group $spec.group -Description $spec.description `
        -Direction Inbound -Action Block -Protocol UDP -LocalPort $spec.port `
        -RemoteAddress $spec.blocked -Profile Any | Out-Null
}
"Rules in group: " + @(Get-NetFirewallRule -Group $spec.group -ErrorAction SilentlyContinue).Count
"""

# Windows' limit for a whole command line is 32767 characters
MAX_COMMAND_LINE = 32000


def firewall_command(spec: dict) -> str:
    """
    PowerShell arguments that apply `spec` (the script and the spec in one -EncodedCommand, so the
    elevated process runs nothing from a file another program could change first).
    """
    data = json.dumps(spec, separators=(",", ":")).replace("'", "''")
    script = f"$spec = '{data}' | ConvertFrom-Json\n{FIREWALL_SCRIPT}"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    args = f"-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    if len(args) > MAX_COMMAND_LINE:
        raise ValueError("too many friends for one firewall rule")
    return args


# endregion
