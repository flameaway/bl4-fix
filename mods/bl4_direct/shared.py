"""
State shared between the in-game mod and the BL4 Direct companion app.

Kept free of any SDK imports so the app can import it as plain Python. The friends file lives in
the SDK's settings folder; the app writes it, the mod reads it (and may update `last_joined`).
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MOD_VERSION = "0.5.5"
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
        """What netsh_command needs to make our rule match this config (port 0: no rule)."""
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


# STUN (RFC 8489), what games and voice chat use to learn their public address. Not a "what's my
# IP" website: antivirus sandboxes count an exe looking itself up on one of those as spyware
STUN_SERVERS = (("stun.cloudflare.com", 3478), ("stun.l.google.com", 19302))
STUN_MAGIC = 0x2112A442


def public_ip(timeout: float = 3.0) -> str | None:
    """
    This PC's public IPv4 address as the internet sees its UDP traffic (the address friends join):
    one 20-byte Binding Request to a STUN server, the reply's XOR-MAPPED-ADDRESS. None when no
    server answers. Blocks up to `timeout` per server: call it off the game/UI thread.
    """
    for server in STUN_SERVERS:
        txn = os.urandom(12)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(struct.pack("!HHI", 0x0001, 0, STUN_MAGIC) + txn, server)
                data = sock.recv(512)
        except OSError:
            continue
        if len(data) < 20 or data[:2] != b"\x01\x01" or data[8:20] != txn:  # Binding Success, ours
            continue
        pos = 20
        while pos + 4 <= len(data):
            kind, size = struct.unpack_from("!HH", data, pos)
            if kind == 0x0020 and size >= 8 and data[pos + 5] == 1:  # XOR-MAPPED-ADDRESS, IPv4
                return socket.inet_ntoa(struct.pack("!I", struct.unpack_from("!I", data, pos + 8)[0] ^ STUN_MAGIC))
            pos += 4 + (size + 3) // 4 * 4
    return None


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
#
# The change runs as one netsh command (2026-10-30), so the UAC prompt reads "Network Command Shell"
# (Microsoft Windows) and "Show more details" shows the whole rule in plain words. It used to be a
# hidden PowerShell with -EncodedCommand: the prompt read "Windows PowerShell" running a blob, which
# looks exactly like malware, and players were wary of it. netsh can't choose between adding,
# updating and removing by itself, so find_rules (no admin needed) looks first.

# PowerShell-made rules (BL4 Direct 0.5.2 and older, old setup apps) are in this group; netsh can't
# give a rule a group, so newer rules are found by FIREWALL_RULE_NAME
FIREWALL_GROUP = "BL4 Direct"
FIREWALL_RULE_NAME = "BL4 Direct - only friends may join"
_SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
# Full paths: never a program found on PATH (netsh runs elevated)
NETSH = _SYSTEM32 / "netsh.exe"
# Said right before the prompt (game page, panel, setup app): the name the UAC prompt shows for netsh
UAC_NAME = "Network Command Shell"
UAC_HINT = f'Windows will ask if "{UAC_NAME}" may make changes: that\'s the firewall update. Choose Yes.'
# Join refuses until the title screen has reached the main menu (see bl4_direct.before_main_menu)
WAIT_FOR_MENU = "Wait for the main menu (answer any prompts on the title screen first), then join."

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
    """The spec netsh_command turns into our block rule."""
    allowed_ips = list(allowed_ips)
    who = ", ".join(allowed_ips) or "no friends"
    return {
        "group": FIREWALL_GROUP,
        "name": FIREWALL_RULE_NAME,
        "description": (
            f"Blocks inbound UDP {port} except from: this PC"
            + (", local network/VPN" if allow_lan else "")
            + f", {who}. Made by BL4 Fix (BL4 Direct Connect)."
        ),
        "port": port,
        "blocked": blocked_ranges(allowed_ips, allow_lan),
    }


# region COM, in process through ctypes (no PowerShell: hidden PowerShell is what antivirus flags)

_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
_ole32.CoInitializeEx.restype = ctypes.c_long  # not HRESULT: RPC_E_CHANGED_MODE is fine (COM is on)
_ole32.CoCreateInstance.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)
_ole32.CoCreateInstance.restype = ctypes.HRESULT
_ole32.CoUninitialize.argtypes = ()
_ole32.CoUninitialize.restype = None
_ole32.CLSIDFromString.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p)
_ole32.CLSIDFromString.restype = ctypes.HRESULT
_oleaut32 = ctypes.WinDLL("oleaut32")
_oleaut32.SysFreeString.argtypes = (ctypes.c_void_p,)
_oleaut32.SysFreeString.restype = None
_oleaut32.VariantClear.argtypes = (ctypes.c_void_p,)
_oleaut32.VariantClear.restype = ctypes.HRESULT


@functools.cache
def com_guid(text: str) -> ctypes.Array:
    guid = (ctypes.c_byte * 16)()
    _ole32.CLSIDFromString("{" + text + "}", ctypes.byref(guid))
    return guid


@functools.cache
def _com_proto(restype: type, types: tuple[type, ...]) -> type:
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)


def com_call(obj: ctypes.c_void_p, index: int, *args: Any, restype: type = ctypes.HRESULT) -> Any:
    """Calls method `index` of a COM object's vtable (str: LPCWSTR, int: int, else a pointer). An HRESULT failure raises OSError."""
    types = tuple(ctypes.c_wchar_p if isinstance(a, str) else ctypes.c_int if isinstance(a, int) else ctypes.c_void_p for a in args)
    method = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0][index]
    return _com_proto(restype, types)(method)(obj, *args)


def com_release(obj: ctypes.c_void_p) -> None:
    if obj:
        com_call(obj, 2, restype=ctypes.c_ulong)


def com_query(obj: ctypes.c_void_p, iid: str) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    com_call(obj, 0, ctypes.byref(com_guid(iid)), ctypes.byref(out))
    return out


def com_bstr(obj: ctypes.c_void_p, index: int) -> str:
    """A BSTR property getter's value ("" for none)."""
    out = ctypes.c_void_p()
    com_call(obj, index, ctypes.byref(out))
    try:
        return ctypes.wstring_at(out.value) if out.value else ""
    finally:
        _oleaut32.SysFreeString(out)


@contextlib.contextmanager
def com_object(clsid: str, iid: str) -> Iterator[ctypes.c_void_p]:
    """A new COM object (COM started on this thread for the duration), released afterwards. Raises OSError."""
    init = _ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    obj = ctypes.c_void_p()
    try:
        _ole32.CoCreateInstance(ctypes.byref(com_guid(clsid)), None, 5, ctypes.byref(com_guid(iid)), ctypes.byref(obj))  # in process or local
        yield obj
    finally:
        com_release(obj)
        if init >= 0:  # S_OK / S_FALSE: this call's to balance
            _ole32.CoUninitialize()


class _Variant(ctypes.Structure):
    _fields_ = (("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p))


# endregion


# Windows Firewall's own policy object (HNetCfg.FwPolicy2, what netsh and the Firewall window use).
# Reading it needs no admin (checked with a Basic User token, 2026-10-30)
_FW_POLICY = "e2b3c97f-6ae1-41ac-817a-f6f92166d7dd"
_IID_FW_POLICY2 = "98325047-c671-4174-8d81-defcd3f03186"
_IID_FW_RULE = "af230d27-baba-4e42-aced-f524f22cfce2"
_IID_ENUM_VARIANT = "00020404-0000-0000-c000-000000000046"
# vtable slots (netfw.h; IUnknown 0-2 and IDispatch 3-6 first)
_POLICY_GET_RULES = 18
_RULES_GET_NEW_ENUM = 11
_ENUM_NEXT = 3
_RULE_GET_NAME, _RULE_GET_DESCRIPTION, _RULE_GET_GROUPING = 7, 9, 35


def find_rules(name: str = FIREWALL_RULE_NAME, group: str = FIREWALL_GROUP) -> list[tuple[str, str]]:
    """
    Our rules in Windows Firewall as (display name, description): the netsh-made one (by name) and
    any old PowerShell-made one (by group), both matched ignoring case. No admin needed; walks every
    rule (~1300 on a gaming PC): never on the game thread. Raises RuntimeError if the firewall can't
    be read.
    """
    name, group = name.casefold(), group.casefold()
    rules = []
    try:
        with com_object(_FW_POLICY, _IID_FW_POLICY2) as policy:
            all_rules, enum_unknown, enum = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            try:
                com_call(policy, _POLICY_GET_RULES, ctypes.byref(all_rules))
                com_call(all_rules, _RULES_GET_NEW_ENUM, ctypes.byref(enum_unknown))
                enum = com_query(enum_unknown, _IID_ENUM_VARIANT)
                batch, fetched = (_Variant * 256)(), ctypes.c_ulong()
                while True:
                    com_call(enum, _ENUM_NEXT, len(batch), ctypes.byref(batch), ctypes.byref(fetched))
                    if not fetched.value:
                        break
                    for i in range(fetched.value):
                        item = batch[i]
                        try:
                            if item.vt != 9:  # VT_DISPATCH
                                continue
                            rule = com_query(ctypes.c_void_p(item.ptr), _IID_FW_RULE)
                            try:
                                rule_name = com_bstr(rule, _RULE_GET_NAME)
                                if rule_name.casefold() == name or com_bstr(rule, _RULE_GET_GROUPING).casefold() == group:
                                    rules.append((rule_name.strip(), com_bstr(rule, _RULE_GET_DESCRIPTION).strip()))
                            finally:
                                com_release(rule)
                        finally:
                            _oleaut32.VariantClear(ctypes.byref(item))  # the enumerator's reference
            finally:
                com_release(enum)
                com_release(enum_unknown)
                com_release(all_rules)
    except OSError as ex:
        raise RuntimeError(f"couldn't read the firewall rules: {ex}") from ex
    return rules


# Windows' limit for a whole command line is 32767 characters
MAX_COMMAND_LINE = 32000


def _netsh_value(text: str) -> str:
    # Names and descriptions are ours (no quotes in them); netsh has no escape for one anyway
    return '"' + str(text).replace('"', "'") + '"'


def netsh_command(spec: dict, existing: Iterable[str], name: str = FIREWALL_RULE_NAME) -> str | None:
    """
    netsh arguments that make our rule match `spec` (a firewall_spec(); port 0: no rule), given the
    display names of our rules there now (find_rules). None when nothing needs changing. Always one
    command, so one UAC prompt: add the rule, update the one there (an old PowerShell rule gets
    `name` on the way), or delete it.
    """
    existing = list(dict.fromkeys(existing))
    if len(existing) > 1:
        raise ValueError(
            "found several BL4 Direct firewall rules (" + ", ".join(existing) + "): delete them in Windows "
            "Defender Firewall (Inbound Rules), then update again"
        )  # fmt: skip
    blocked = spec.get("blocked") or []
    if int(spec.get("port") or 0) <= 0 or not blocked:
        # No rule wanted (or nothing left to block)
        return f"advfirewall firewall delete rule name={_netsh_value(existing[0])}" if existing else None
    settings = (
        f"name={_netsh_value(name)} dir=in action=block protocol=UDP localport={int(spec['port'])} profile=any "
        f"description={_netsh_value(spec.get('description', ''))} remoteip={','.join(blocked)}"
    )
    if existing:
        args = f"advfirewall firewall set rule name={_netsh_value(existing[0])} new {settings} enable=yes"
    else:
        args = f"advfirewall firewall add rule {settings}"
    if len(args) > MAX_COMMAND_LINE:
        raise ValueError("too many friends for one firewall rule")
    return args


# endregion
