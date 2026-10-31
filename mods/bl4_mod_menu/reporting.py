"""
"Report a problem" on the mods screen: makes a bug report zip (report.py collects it) and opens
GitHub's new-issue page; plus a copy of the SDK log that outlives the next launch.

- The SDK truncates OakGame/Binaries/Win64/Plugins/unrealsdk.log at every launch, before any mod
  runs, so after a crash the evidence was gone by the time the player started the game again to
  report it. start_log_copy() keeps a copy of it, unrealsdk.session.log, appended from inside the
  game when something happens (the first world, each map change, every logged error: no
  timer), and renames last session's copy to unrealsdk.prev.log at launch. After a hard crash the
  copy lacks the info lines since the last of those moments; errors are always in it. (Until
  2026-10-31 a hidden PowerShell waited for the game to exit and copied the log.)
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

SESSION_NAME = "unrealsdk.session.log"  # this session's copy; renamed to PREV_NAME at the next launch
COPY_DELAY_S = 0.5  # the SDK writes a line from its own thread a moment after the logging call
COPY_PIECE = 1 << 20  # read and written at most this much at a time
COPY_MAX = 16 << 20  # past this the copy starts over with the log's last COPY_KEEP bytes
COPY_KEEP = 4 << 20  # (a bug report takes the last 2 MB of each log)


class _LogCopy:
    """One per game process, kept on the errors hub so a reload of this module keeps it."""

    def __init__(self, log: Path, out: Any) -> None:
        self.pid = os.getpid()
        self.log = log
        self.out = out  # open for the whole session: another game copy can't rename it meanwhile
        self.offset = 0  # how far into the SDK's log the copy has got
        self.written = 0  # the copy's size
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stopped = False


def start_log_copy(game_dir: Path) -> bool:
    """
    Once per game session: last session's copy becomes unrealsdk.prev.log and this session's copy
    is opened. Nothing is copied until copy_log_soon() asks. Returns whether this session is copied.
    """
    hub = errors.H
    copy = getattr(hub, "log_copy", None)
    if copy is not None and copy.pid == os.getpid():
        return not copy.stopped
    log = Path(game_dir) / LOG_DIR / LOG_NAME
    session = log.with_name(SESSION_NAME)
    try:
        if session.exists():
            os.replace(session, log.with_name(PREV_NAME))
        out = session.open("wb")
    except OSError as ex:
        # Another copy of the game still runs and holds its copy open: it was wiped with the log
        # when we started, and the log is ours now, but its copy isn't, so this session isn't kept
        logging.info(f"[Mod Menu] Not keeping a copy of this session's log (another game copy runs?): {ex}")
        return False
    hub.log_copy = _LogCopy(log, out)
    hub.on_logged = copy_log_soon  # errors._error calls it after every logged error
    return True


def copy_log_soon() -> None:
    """
    Copies what the SDK's log gained since the last copy, COPY_DELAY_S from now on a worker. Any
    thread, never raises, cheap (an Event). Asked at the first world and each map change
    (menu._note_world) and on every logged error (errors._error).
    """
    try:
        copy = getattr(errors.H, "log_copy", None)
        if copy is None or copy.stopped:
            return
        copy.event.set()
        with copy.lock:
            if copy.thread is None:
                # Waits on the Event for the rest of the session: no CPU, no GIL while it waits
                copy.thread = threading.Thread(target=_copier, args=(copy,), name="bl4_mod_menu.log_copy", daemon=True)
                copy.thread.start()
    except Exception:  # noqa: BLE001, S110 - called from the error logger: must never raise
        pass


def _copier(copy: _LogCopy) -> None:
    while not copy.stopped:
        copy.event.wait()
        time.sleep(COPY_DELAY_S)  # also folds a burst of asks into one copy
        copy.event.clear()
        try:
            _copy_new(copy)
        except Exception as ex:  # noqa: BLE001
            copy.stopped = True
            # A warning, not an error: an error would ask for another copy
            logging.warning(f"[Mod Menu] Stopped copying this session's log for bug reports: {ex!r}")


def _copy_new(copy: _LogCopy) -> None:
    """Appends the log's new bytes to the copy (file reads and writes: they let go of the GIL)."""
    size = copy.log.stat().st_size
    if size < copy.offset:
        # Shorter than what we copied: another copy of the game started and wiped it (the SDK's
        # log is one file for every game copy). What it writes now is that game's session.
        copy.stopped = True
        copy.out.close()
        return
    if size == copy.offset:
        return
    if copy.written + size - copy.offset > COPY_MAX:
        copy.out.seek(0)
        copy.out.truncate()
        copy.written = 0
        copy.offset = max(copy.offset, size - COPY_KEEP)
    with copy.log.open("rb") as src:
        while copy.offset < size:
            src.seek(copy.offset)
            data = src.read(min(COPY_PIECE, size - copy.offset))
            if not data:
                break
            copy.out.write(data)
            copy.offset += len(data)
            copy.written += len(data)
    copy.out.flush()


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
