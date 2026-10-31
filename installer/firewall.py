"""
Windows Firewall rule that limits who can reach a hosted game.

The rule itself (a block rule for the co-op port covering every address but the allowlist) is built
in shared.py (the bundled copy of mods/bl4_direct/shared.py), so the setup app and the mod's own
Update button make the same rule with the same netsh command. Run here directly when this app is
admin, else through `run` (core.run_as_admin): the UAC prompt then names netsh ("Network Command
Shell", Microsoft Windows) with the rule in plain words, not this app.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable

import shared  # the payload copy (core puts it on sys.path first)

RULE_GROUP = shared.FIREWALL_GROUP
blocked_ranges = shared.blocked_ranges

# run(exe, params) -> exit code, with a UAC prompt (core.run_as_admin)
Runner = Callable[[str, str], int]


def _apply(spec: dict, run: Runner | None) -> str:
    args = shared.netsh_command(spec, [name for name, _ in shared.find_rules()])
    if args is None:
        return "nothing to change"
    if run is not None:
        if code := run(str(shared.NETSH), args):
            raise RuntimeError(f"firewall update failed (netsh exit code {code})")
        return "ok"
    res = subprocess.run(
        f'"{shared.NETSH}" {args}',
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if res.returncode != 0:
        raise RuntimeError(f"firewall update failed: {res.stdout.strip() or res.stderr.strip()}")
    return res.stdout.strip()


def apply_rule(port: int, allowed_ips: Iterable[str], allow_lan: bool, run: Runner | None = None) -> str:
    """Adds or updates our firewall rule. Needs admin: this app's, or `run`'s prompt."""
    return _apply(shared.firewall_rule(port, allowed_ips, allow_lan), run)


def rule_is_current(spec: dict) -> bool:
    """
    Whether Windows already has exactly this rule (no admin needed to read it): our one rule, by its
    name, with this spec's description, which spells out the port, the local network choice and
    every allowed IP the blocked ranges are made from. Then applying it again would ask for UAC
    to change nothing. False when the firewall can't be read.
    """
    try:
        rules = shared.find_rules()
    except RuntimeError:
        return False
    return rules == [(spec["name"], spec["description"])]


def remove_rule(run: Runner | None = None) -> str:
    """Deletes our firewall rule, if there is one. Needs admin: this app's, or `run`'s prompt."""
    return _apply({"group": RULE_GROUP, "port": 0}, run)


def rule_summary() -> str | None:
    """Description of the current rule (an old PowerShell-made one too), or None if there isn't one."""
    try:
        rules = shared.find_rules()
    except RuntimeError:
        return None
    return "; ".join(description or name for name, description in rules) or None
