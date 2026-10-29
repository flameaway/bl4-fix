"""
Runs in YOUR desktop session so test automation can start BL4 there instead of in the hidden,
elevated session the automation shell runs in (launching from there breaks Steam).

Start it yourself in a normal (non-admin) terminal and close it when you're done testing:

    python tools/desktop_agent.py serve

It only understands these fixed requests, nothing else:
    steam                      start Steam if it isn't already running
    launch <id> [extra args]   start Borderlands4.exe -bridgeid=<id> ...
    kill                       end all Borderlands4.exe processes
    shot <pid> <out.png>       screenshot a game window
    click <pid> <x> <y>        post a mouse click to a game window (screenshot coordinates)
    rclick <pid> <x> <y>       post a right click
    move <pid> <x> <y>         post a mouse move (hover) only
    key <pid> <vk>             post a key press to a game window
    type <pid> <text>          post typed characters to a game window

Clients send requests with e.g. `python tools/desktop_agent.py launch A`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
QUEUE = REPO / ".bridge" / "agent"
GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4\OakGame\Binaries\Win64")
GAME_EXE = GAME_DIR / "Borderlands4.exe"
STEAM_EXE = Path(r"C:\Program Files (x86)\Steam\steam.exe")

DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def is_running(image: str) -> bool:
    res = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return image.lower() in res.stdout.lower()


def handle(req: dict) -> dict:
    match req["action"]:
        case "steam":
            if is_running("steam.exe"):
                return {"ok": True, "already_running": True}
            subprocess.Popen([str(STEAM_EXE)], cwd=STEAM_EXE.parent, creationflags=DETACHED)
            return {"ok": True, "already_running": False}
        case "launch":
            bridge_id = str(req["id"])
            if not bridge_id.isalnum():
                return {"ok": False, "error": "id must be alphanumeric"}
            extra = [str(a) for a in req.get("args", [])]
            proc = subprocess.Popen(
                [str(GAME_EXE), f"-bridgeid={bridge_id}", *extra],
                cwd=GAME_DIR,
                creationflags=DETACHED,
            )
            return {"ok": True, "pid": proc.pid}
        case "kill":
            res = subprocess.run(
                ["taskkill", "/F", "/IM", "Borderlands4.exe"],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"ok": True, "output": res.stdout + res.stderr}
        case "shot":
            res = subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(REPO / "tools" / "shot.ps1"),
                    "-ProcessId", str(int(req["pid"])),
                    "-Out", str(Path(req["out"]).resolve()),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"ok": res.returncode == 0, "output": res.stdout + res.stderr}
        case "input":
            if "x" in req:
                extra = ["-X", str(int(req["x"])), "-Y", str(int(req["y"]))]
                if req.get("button") == "right":
                    extra.append("-Right")
                elif req.get("button") == "none":
                    extra.append("-Move")
                if req.get("wheel"):
                    extra += ["-Wheel", str(int(req["wheel"]))]
            elif "text" in req:
                extra = ["-Text", str(req["text"])]
            else:
                extra = ["-Key", str(int(req["key"]))]
            res = subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(REPO / "tools" / "input.ps1"),
                    "-ProcessId", str(int(req["pid"])),
                    *extra,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"ok": res.returncode == 0, "output": res.stdout + res.stderr}
        case other:
            return {"ok": False, "error": f"unknown action {other!r}"}


def serve() -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    (QUEUE / "alive").write_text(str(os.getpid()), encoding="utf-8")
    print(f"desktop agent running (pid {os.getpid()}), watching {QUEUE}. Ctrl+C to stop.")
    own_mtime = Path(__file__).stat().st_mtime
    try:
        while True:
            # Pick up edits to this file (or the scripts it calls) without needing a manual restart
            if Path(__file__).stat().st_mtime != own_mtime:
                print("agent source changed, restarting")
                subprocess.Popen([sys.executable, __file__, "serve"])
                return
            for path in sorted(QUEUE.glob("*.req.json")):
                req = json.loads(path.read_text(encoding="utf-8"))
                path.unlink()
                try:
                    resp = handle(req)
                except Exception as ex:  # noqa: BLE001
                    resp = {"ok": False, "error": repr(ex)}
                print(f"{time.strftime('%H:%M:%S')} {req} -> {resp}")
                out = QUEUE / path.name.replace(".req.json", ".resp.json")
                out.write_text(json.dumps(resp), encoding="utf-8")
            (QUEUE / "alive").touch()
            time.sleep(0.2)
    finally:
        (QUEUE / "alive").unlink(missing_ok=True)


def request(req: dict, timeout: float = 30) -> dict:
    alive = QUEUE / "alive"
    if not alive.exists() or time.time() - alive.stat().st_mtime > 5:
        raise SystemExit("desktop agent isn't running - start `python tools/desktop_agent.py serve`")
    name = f"r{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    tmp = QUEUE / f"{name}.tmp"
    tmp.write_text(json.dumps(req), encoding="utf-8")
    tmp.replace(QUEUE / f"{name}.req.json")
    resp_path = QUEUE / f"{name}.resp.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if resp_path.exists():
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            resp_path.unlink()
            return resp
        time.sleep(0.1)
    raise SystemExit("timed out waiting for the desktop agent")


def main() -> None:
    match sys.argv[1:]:
        case ["serve"]:
            serve()
        case ["steam"]:
            print(request({"action": "steam"}))
        case ["launch", bridge_id, *extra]:
            print(request({"action": "launch", "id": bridge_id, "args": extra}))
        case ["kill"]:
            print(request({"action": "kill"}))
        case ["shot", pid, out]:
            print(request({"action": "shot", "pid": pid, "out": out}, timeout=60))
        case ["click", pid, x, y]:
            print(request({"action": "input", "pid": pid, "x": x, "y": y}))
        case ["rclick", pid, x, y]:
            print(request({"action": "input", "pid": pid, "x": x, "y": y, "button": "right"}))
        case ["move", pid, x, y]:
            print(request({"action": "input", "pid": pid, "x": x, "y": y, "button": "none"}))
        case ["wheel", pid, x, y, delta]:
            print(request({"action": "input", "pid": pid, "x": x, "y": y, "wheel": delta}))
        case ["type", pid, text]:
            print(request({"action": "input", "pid": pid, "text": text}))
        case ["key", pid, key]:
            print(request({"action": "input", "pid": pid, "key": int(key, 0)}))
        case _:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
