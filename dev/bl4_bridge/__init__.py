"""
Dev-only remote control for a running BL4 instance.

Each game process gets a folder under `<repo>/.bridge/<id>/`, where `<id>` comes from a
`-bridgeid=<id>` launch arg (falls back to the PID). Drop Python files into `in/`:

- `*.thread.py` runs immediately on the bridge's own thread. Do not touch unreal objects from
  these, they're for meta operations like toggling call logging or adding hooks.
- any other `*.py` runs on the game thread, the next time one of the tick hooks fires.

Output (stdout, the repr of a `result` variable, and any traceback) is written to `out/<name>.txt`.
Code runs in one persistent namespace per instance, so state carries over between files.
"""

from __future__ import annotations

import ctypes
import io
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import hooks, logging

REPO_ROOT = Path(__file__).resolve().parents[2]


def _get_bridge_id() -> str:
    try:
        ctypes.windll.kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
        cmdline: str = ctypes.windll.kernel32.GetCommandLineW()
    except Exception:  # noqa: BLE001
        cmdline = ""
    if match := re.search(r"-bridgeid=(\w+)", cmdline, flags=re.I):
        return match.group(1)
    return str(os.getpid())


BRIDGE_ID = _get_bridge_id()
BRIDGE_DIR = REPO_ROOT / ".bridge" / BRIDGE_ID
IN_DIR = BRIDGE_DIR / "in"
OUT_DIR = BRIDGE_DIR / "out"
EVENTS_FILE = BRIDGE_DIR / "events.log"
EVENTS_MAX = 4 << 20  # bytes; then the log moves to events.log.1 and starts over

_pending: queue.Queue[tuple[str, str]] = queue.Queue()
_events_lock = threading.Lock()
_tick_hooks: set[str] = set()
_HOOK_ID = "bl4_bridge.tick"

namespace: dict[str, Any] = {}


def event(msg: str) -> None:
    """Appends a timestamped line to this instance's events log. Safe from any thread."""
    line = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}\n"
    with _events_lock:
        try:
            if EVENTS_FILE.stat().st_size > EVENTS_MAX:
                EVENTS_FILE.replace(EVENTS_FILE.with_name(EVENTS_FILE.name + ".1"))
        except OSError:
            pass
        with EVENTS_FILE.open("a", encoding="utf-8") as file:
            file.write(line)


class _PerThread:
    """
    A sys.stdout / sys.stderr that sends each thread's output to that thread's buffer, if it has
    one, and everything else to the real stream.

    contextlib.redirect_stdout swaps the process-wide stream, so a thread script and a game-thread
    script running at once could unwind each other's swap and leave every later print going
    nowhere.
    """

    def __init__(self, real: Any) -> None:
        self.real = real
        self.local = threading.local()

    def write(self, text: str) -> int:
        return (getattr(self.local, "buffer", None) or self.real).write(text)

    def flush(self) -> None:
        if getattr(self.local, "buffer", None) is None:
            self.real.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)  # encoding, isatty, ...


def _capture(name: str) -> _PerThread:
    stream = getattr(sys, name)
    if not isinstance(stream, _PerThread):
        stream = _PerThread(stream)
        setattr(sys, name, stream)
    return stream


def _run(name: str, code: str) -> None:
    buffer = io.StringIO()
    streams = (_capture("stdout"), _capture("stderr"))
    namespace.pop("result", None)
    try:
        for stream in streams:
            stream.local.buffer = buffer
        exec(compile(code, name, "exec"), namespace)  # noqa: S102
        if "result" in namespace:
            buffer.write(f"\nresult = {namespace['result']!r}\n")
    except BaseException:  # noqa: BLE001
        buffer.write(traceback.format_exc())
    finally:
        for stream in streams:
            stream.local.buffer = None
    tmp = OUT_DIR / f"{name}.tmp"
    tmp.write_text(buffer.getvalue(), encoding="utf-8")
    tmp.replace(OUT_DIR / f"{name}.txt")


def _on_tick(*_: Any) -> None:
    # Called on every camera hook call (6 a frame): raising queue.Empty each time was ~400
    # exceptions a second, so look first
    while not _pending.empty():
        try:
            name, code = _pending.get_nowait()
        except queue.Empty:
            return
        _run(name, code)


def add_tick_hook(func: str) -> bool:
    """Drains the game-thread queue whenever the given unreal function is called."""
    _tick_hooks.add(func)
    return hooks.add_hook(func, hooks.Type.PRE, _HOOK_ID, _on_tick)


def remove_tick_hook(func: str) -> None:
    _tick_hooks.discard(func)
    hooks.remove_hook(func, hooks.Type.PRE, _HOOK_ID)


def _watch() -> None:
    while True:
        try:
            for path in sorted(IN_DIR.glob("*.py"), key=lambda p: p.stat().st_mtime):
                code = path.read_text(encoding="utf-8")
                path.unlink()
                if path.name.endswith(".thread.py"):
                    _run(path.name.removesuffix(".thread.py"), code)
                else:
                    _pending.put((path.stem, code))
        except Exception:  # noqa: BLE001
            event("watch error: " + traceback.format_exc())
        time.sleep(0.1)


def _start() -> None:
    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    namespace.update(
        {
            "unrealsdk": unrealsdk,
            "hooks": hooks,
            "logging": logging,
            "event": event,
            "add_tick_hook": add_tick_hook,
            "remove_tick_hook": remove_tick_hook,
            "BRIDGE_ID": BRIDGE_ID,
            "BRIDGE_DIR": BRIDGE_DIR,
        },
    )

    helpers_file = Path(__file__).with_name("helpers.py")

    def reload_helpers() -> None:
        exec(compile(helpers_file.read_text(encoding="utf-8"), str(helpers_file), "exec"), namespace)  # noqa: S102

    namespace["reload_helpers"] = reload_helpers
    # Helpers touch unreal objects, so load them on the game thread with the first tick
    _pending.put(("_load_helpers", "reload_helpers()"))

    tick_file = REPO_ROOT / "dev" / "tick_hooks.txt"
    if tick_file.exists():
        for line in tick_file.read_text(encoding="utf-8").splitlines():
            if (func := line.strip()) and not func.startswith("#"):
                try:
                    add_tick_hook(func)
                except ValueError as ex:
                    event(f"tick hook {func} failed: {ex}")

    (BRIDGE_DIR / "pid").write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=_watch, name="bl4_bridge", daemon=True).start()
    event(f"bridge started pid={os.getpid()} tick_hooks={sorted(_tick_hooks)}")
    logging.info(f"[bl4_bridge] listening in {BRIDGE_DIR}")


_start()
