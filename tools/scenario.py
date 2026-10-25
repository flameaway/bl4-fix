"""
Test-harness steps for driving BL4 instances through the desktop agent + bridge.

    python tools/scenario.py up A            launch A (if needed) and wait for its bridge
    python tools/scenario.py ingame A        click Continue until A has a pawn in a map
    python tools/scenario.py menu A          wait until A is idle at the main menu
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / ".bridge"
# Fixed window size so UI click positions are stable between runs
WINDOW_ARGS = ["-windowed", "-ResX=1600", "-ResY=900"]
CONTINUE_BUTTON = (200, 209)  # main menu "Continue" at 1600x900, in screenshot coordinates
WARNING_CONFIRM = (640, 665)  # photosensitivity warning "Confirm" at 1600x900


def py(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=REPO,
    )


def bridge(bridge_id: str, code: str, timeout: float = 15) -> str | None:
    res = py("tools/bridge.py", bridge_id, code, "--timeout", str(timeout), timeout=timeout + 10)
    return res.stdout if res.returncode == 0 else None


def pid_of(bridge_id: str) -> str:
    return (BRIDGE / bridge_id / "pid").read_text().strip()


def is_alive(pid: str) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False,
    ).stdout
    return "Borderlands4" in out


def up(bridge_id: str, extra: list[str]) -> None:
    pid_file = BRIDGE / bridge_id / "pid"
    if pid_file.exists() and is_alive(pid_file.read_text().strip()):
        print(f"{bridge_id} already running (pid {pid_of(bridge_id)})")
        return
    events = BRIDGE / bridge_id / "events.log"
    events.unlink(missing_ok=True)
    print(py("tools/desktop_agent.py", "launch", bridge_id, *WINDOW_ARGS, *extra).stdout.strip())
    deadline = time.time() + 300
    while time.time() < deadline:
        if events.exists():
            print(f"{bridge_id} bridge up (pid {pid_of(bridge_id)})")
            return
        time.sleep(2)
    raise SystemExit(f"{bridge_id}: bridge never came up")


STATE = (
    "w = world(); pc = get_pc()\n"
    "print(w.Name if w else None, '|', w.NetDriver.Class.Name if w and w.NetDriver else None,"
    " '|', bool(pc and pc.Pawn), '|', KSL.IsServer(w) if w else None)"
)


def state(bridge_id: str) -> list[str] | None:
    out = bridge(bridge_id, STATE)
    return [s.strip() for s in out.strip().split("|")] if out else None


def ingame(bridge_id: str, timeout: float = 900) -> None:
    deadline = time.time() + timeout
    last_click = 0.0
    while time.time() < deadline:
        if not is_alive(pid_of(bridge_id)):
            raise SystemExit(f"{bridge_id}: game process died")
        st = state(bridge_id)
        if st and st[2] == "True":
            print(f"{bridge_id} in game: {st}")
            return
        if time.time() - last_click > 20:
            py("tools/desktop_agent.py", "click", pid_of(bridge_id), *map(str, CONTINUE_BUTTON))
            last_click = time.time()
        time.sleep(5)
    raise SystemExit(f"{bridge_id}: never got in game")


def main() -> None:
    match sys.argv[1:]:
        case ["up", bridge_id, *extra]:
            up(bridge_id, extra)
        case ["ingame", bridge_id]:
            ingame(bridge_id)
        case ["state", bridge_id]:
            print(state(bridge_id))
        case _:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
