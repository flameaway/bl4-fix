"""
State shared between the in-game mod and the BL4 Direct companion app.

Kept free of any SDK imports so the app can import it as plain Python. The friends file lives in
the SDK's settings folder; the app writes it, the mod reads it (and may update `last_joined`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

MOD_VERSION = "0.5.1"
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
    # What the firewall rule was last built from (see firewall_signature); written by the setup app
    firewall_applied: str = ""

    def firewall_signature(self) -> str:
        """Everything the hosting firewall rule depends on. Only the host needs the rule."""
        ips = sorted({f.address for f in self.friends if is_ipv4(f.address)})
        return f"{self.host_port}|{self.allow_lan}|{','.join(ips)}"

    def firewall_up_to_date(self) -> bool:
        return self.firewall_applied == self.firewall_signature()

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
