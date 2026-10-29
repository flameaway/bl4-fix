"""
Windows Firewall rule that limits who can reach a hosted game.

The rule itself (a block rule for the co-op port covering every address but the allowlist) is built
in shared.py (the bundled copy of mods/bl4_direct/shared.py), so the setup app and the mod's own
Update button make the same rule. This module runs it from an elevated setup app.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

import shared  # the payload copy (core puts it on sys.path first)

RULE_GROUP = shared.FIREWALL_GROUP
blocked_ranges = shared.blocked_ranges

APPLY_SCRIPT = (
    "param([string]$SpecFile)\n$spec = Get-Content -Raw $SpecFile | ConvertFrom-Json\n" + shared.FIREWALL_SCRIPT
)


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
    return _run_ps(APPLY_SCRIPT, shared.firewall_rule(port, allowed_ips, allow_lan))


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
