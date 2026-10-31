"""
"Report a problem" on the mods screen: makes a bug report zip (report.py collects it) and opens
GitHub's new-issue page; plus the helper that keeps the last session's SDK log.

- The SDK truncates OakGame/Binaries/Win64/Plugins/unrealsdk.log at every launch, so after a crash
  the evidence was gone by the time the player started the game again to report it.
  start_log_keeper() starts one hidden PowerShell per game session that waits for this process to
  exit (Wait-Process: no polling) and then copies the log to unrealsdk.prev.log. It does nothing
  while another copy of the game still runs (that one owns the log by then).
- make(): the live part (mods, versions, health, this session's errors, map, role) is read here on
  the game thread (small); report.build() runs on a worker and writes the zip; the page shows the
  result and the buttons to show the zip in Explorer and to open the issue page.

Nothing runs per frame: web.tick reads R.revision (an int) and R.note.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import errors

LOG_DIR = "OakGame/Binaries/Win64/Plugins"
LOG_NAME = "unrealsdk.log"
PREV_NAME = "unrealsdk.prev.log"
ISSUES = "https://github.com/flameaway/bl4-fix/issues/new"


class R:
    status = "idle"  # idle / building / done / error
    message = ""
    zip_path: Path | None = None
    url = ""
    revision = 0
    # A line for web.say on the game thread (the worker can't touch the page's state safely)
    note: tuple[str, bool] | None = None
    explorer: subprocess.Popen[bytes] | None = None


def _set(status: str, message: str, *, error: bool = False) -> None:
    R.status, R.message = status, message
    R.revision += 1
    R.note = (message, error)


# region The last session's log


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def log_keeper_script(game_dir: Path, pid: int, game_name: str = "Borderlands4") -> str:
    log = Path(game_dir) / LOG_DIR / LOG_NAME
    prev = log.with_name(PREV_NAME)
    return (
        # Idle for the whole session: give back what PowerShell's start-up used (~74 -> ~38 MB)
        "[System.GC]::Collect(); $me = [System.Diagnostics.Process]::GetCurrentProcess(); "
        "try { $me.MinWorkingSet = [IntPtr]204800; $me.MaxWorkingSet = [IntPtr]409600 } catch { }; "
        f"try {{ Wait-Process -Id {pid} -ErrorAction Stop }} catch {{ }}; "
        # Another copy started after us truncated the log and writes it now: leave it
        f"if (Get-Process -Name {game_name} -ErrorAction SilentlyContinue | Where-Object {{ $_.Id -ne {pid} }}) {{ exit }}; "
        f"for ($i = 0; $i -lt 20; $i++) {{ try {{ "
        f"Copy-Item -LiteralPath {_ps_quote(str(log))} -Destination {_ps_quote(str(prev))} -Force -ErrorAction Stop; break "
        "} catch { Start-Sleep 1 } }"
    )


def start_log_keeper(game_dir: Path) -> bool:
    """Once per game session (kept across reloads of this module). Returns whether it runs."""
    hub = errors.H
    if getattr(hub, "log_keeper", None) == os.getpid():
        return True
    try:
        # The game runs in a job that doesn't allow breaking away, but the helper still outlives a
        # normal game exit (the update helper and the page modules do the same)
        process = subprocess.Popen(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", log_keeper_script(game_dir, os.getpid())],  # noqa: S607
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as ex:
        logging.warning(f"[Mod Menu] Couldn't start the log keeper (bug reports get only this session's log): {ex}")
        return False
    hub.log_keeper = os.getpid()
    hub.log_keeper_process = process  # kept, or Python warns that it's still running when it's dropped
    return True


# endregion
# region The report


def _report() -> Any:
    """report.py, imported when first needed (a missing or broken one mustn't break the menu)."""
    return importlib.import_module(f"{__package__}.report")


def live_state() -> dict[str, Any]:
    """What only the running game knows, for the report (game thread)."""
    from . import health, model  # noqa: PLC0415

    import pyunrealsdk  # noqa: PLC0415
    import unrealsdk  # noqa: PLC0415

    counts = errors.counts()
    mods: list[dict[str, Any]] = []
    for mod in model.mods():
        try:
            settings = getattr(mod, "settings_file", None)
            mod_id = Path(settings).stem if settings is not None else ""
            mods.append({"id": mod_id, "name": mod.name, "version": mod.version or "", "enabled": bool(mod.is_enabled),
                         "status": model.status(mod)[0], "errors": counts.get(mod_id, 0)})  # fmt: skip
        except Exception as ex:  # noqa: BLE001
            mods.append({"id": "", "name": str(getattr(mod, "name", "?")), "version": "", "enabled": False,
                         "status": f"unreadable: {ex}", "errors": 0})  # fmt: skip
    try:
        problems = health.problems()
    except Exception as ex:  # noqa: BLE001
        problems = {"error": str(ex)}
    world_name, role = errors._context()  # noqa: SLF001
    return {
        "mods": mods,
        "health": problems,
        "errors": errors.entries(),
        "session": errors.H.session,
        "map": world_name,
        "role": role,
        "sdk": {"unrealsdk": str(getattr(unrealsdk, "__version__", "")),
                "pyunrealsdk": str(getattr(pyunrealsdk, "__version__", "")),
                "python": sys.version.split()[0]},  # fmt: skip
    }


def make(game_dir: Path) -> bool:
    """Starts a bug report (game thread). False if one is being made already."""
    if R.status == "building":
        return False
    try:
        report = _report()
    except Exception as ex:  # noqa: BLE001
        _set("error", f"Couldn't make a bug report: the report module is missing or broken ({ex}).", error=True)
        return True
    live = live_state()
    errors.flush()  # the counts that moved, so the file in the zip matches
    _set("building", "Making a bug report...")
    threading.Thread(target=_build, args=(report, Path(game_dir), live), name="bl4_mod_menu.report", daemon=True).start()
    return True


def _build(report: Any, game_dir: Path, live: dict[str, Any]) -> None:
    # Let a pending write of the errors file land first (a worker; normally done at once)
    for _ in range(40):
        if not errors.H.writing:
            break
        time.sleep(0.05)
    # The errors only the SDK's log holds (uncaught in hooks, the SDK's own, before we loaded)
    try:
        if errors.scan_log():
            live["errors"] = errors.entries()
            counts = errors.counts()
            for row in live["mods"]:
                row["errors"] = counts.get(row["id"], 0)
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"[Mod Menu] Reading unrealsdk.log for errors failed: {ex!r}")
    try:
        path = Path(report.build(game_dir, None, live))
        try:
            url = str(report.issue_url(path, live))
        except Exception:  # noqa: BLE001
            url = ISSUES
        R.zip_path, R.url = path, url
        _set("done", f"Bug report saved: {path.name}. Attach it to a new issue on GitHub (Open issue page).")
        show_zip()
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"[Mod Menu] The bug report failed: {ex!r}")  # a warning: not an error of its own to record
        _set("error", f"Couldn't make the bug report: {ex}", error=True)


def show_zip() -> None:
    """Explorer with the zip selected (or the reports folder)."""
    path = R.zip_path
    if path is None:
        return
    try:
        if path.is_file():
            # One string, not a list: subprocess quotes the whole "/select,<path with spaces>", and
            # Explorer ignores a quoted /select and opens Documents. Kept: a dropped Popen warns
            # that it still runs (explorer hands off and exits)
            R.explorer = subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"', close_fds=True)  # noqa: S603, S607
        else:
            os.startfile(path.parent)  # noqa: S606
    except OSError as ex:
        R.note = (f"Couldn't open Explorer: {ex}", True)


def open_issue() -> None:
    """GitHub's new-issue page in the browser, filled in from the last report (if there is one)."""
    url = R.url or ISSUES
    threading.Thread(target=_open_url, args=(url,), name="bl4_mod_menu.issue", daemon=True).start()


def _open_url(url: str) -> None:
    try:
        os.startfile(url)  # noqa: S606
    except OSError as ex:
        R.note = (f"Couldn't open the browser: {ex}. The page: {ISSUES}", True)


def snapshot(full: bool) -> dict[str, Any]:
    """The mods screen's "Report a problem" page: errors this session and the report's state."""
    out = errors.summary() if full else {"rev": errors.revision(), "errors": len(errors.H.records)}
    out |= {"status": R.status, "message": R.message, "zip": R.zip_path.name if R.zip_path else "",
            "folder": str(R.zip_path.parent) if R.zip_path else "", "can_issue": True}  # fmt: skip
    return out


# endregion
