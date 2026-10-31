"""
The mod's worker: backups, restores and the page's data, all off the game thread.

One thread runs jobs in order (submit()) and ends after IDLE_S without any. After each job it
rebuilds the page's view and writes it, if it changed, to `uiresources/bl4savebackup/view.json`
(the page reads that file when `W.rev` moves, so the game thread never serializes it). Small
live things the page shows at once (busy text, toast, the staged restore, the last result) are
plain fields here, read by page.state().

Plain Python, no SDK imports (logging goes through W.log, set by the mod).
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import restore, savefile, store

IDLE_S = 30.0
FIT = "All that fit in 1 GB"
KEEP_CHOICES = ["10", "20", "30", "50", "100", "250", "500", "1000", FIT]
DEFAULT_KEEP = "30"


class W:
    store: store.Store | None = None
    ui_dir: Path | None = None
    keep: str = DEFAULT_KEEP
    jobs: deque[tuple[str, Callable[[], None]]] = deque()
    lock = threading.Lock()
    thread: threading.Thread | None = None
    wake = threading.Event()
    busy = ""
    toast: dict[str, Any] = {"id": 0, "text": "", "error": False}  # noqa: RUF012
    rev = 0
    view_text = ""
    pending: list[dict[str, Any]] = []  # noqa: RUF012 - the staged restore's actions (what, target)
    result: dict[str, Any] | None = None  # what the helper did after the last exit, not yet dismissed
    account = ""
    last_launch = ""  # message of the launch backup
    changed: Callable[[], None] = staticmethod(lambda: None)  # tells the Mod Menu the state moved
    log: Callable[[str, bool], None] = staticmethod(lambda text, error=False: None)


def configure(game_dir: Path, settings_dir: Path, ui_dir: Path) -> None:
    if W.store is None:
        exe = game_dir / "OakGame/Binaries/Win64/Borderlands4.exe"
        keys = savefile.Keys(exe, settings_dir / "bl4_save_backup_key.json", (settings_dir / "weapondex_savekey.json",))
        W.store = store.Store(keys)
    W.ui_dir = ui_dir


def keep_count() -> int | None:
    return None if W.keep == FIT else int(W.keep)


def say(text: str, *, error: bool = False) -> None:
    W.toast = {"id": W.toast["id"] + 1, "text": text, "error": error}
    W.log(text, error)
    W.changed()


# region Worker


def submit(name: str, fn: Callable[[], None], busy: str = "") -> None:
    """Runs fn on the worker (after the jobs before it). A job of the same name already waiting is replaced."""
    with W.lock:
        W.jobs = deque(j for j in W.jobs if j[0] != name)
        W.jobs.append((name, fn if not busy else _with_busy(busy, fn)))
        if W.thread is None or not W.thread.is_alive():
            W.thread = threading.Thread(target=_run, name="bl4_save_backup", daemon=True)
            W.thread.start()
    W.wake.set()


def _with_busy(text: str, fn: Callable[[], None]) -> Callable[[], None]:
    def run() -> None:
        W.busy = text
        W.changed()
        try:
            fn()
        finally:
            W.busy = ""
            W.changed()

    return run


def _run() -> None:
    savefile.PACE = _breathe
    idle_since = time.monotonic()
    while True:
        with W.lock:
            job = W.jobs.popleft() if W.jobs else None
            if job is None and time.monotonic() - idle_since > IDLE_S:
                W.thread = None
                return
        if job is None:
            W.wake.wait(1.0)
            W.wake.clear()
            continue
        try:
            job[1]()
        except restore.RestoreError as ex:
            say(f"Can't restore that: {ex}.", error=True)
        except Exception as ex:  # noqa: BLE001 - one failed job mustn't stop the others
            say(f"Save Backups: {job[0]} failed ({ex or type(ex).__name__}).", error=True)
            W.log("".join(traceback.format_exception(ex)), True)
        try:
            _publish()
        except Exception as ex:  # noqa: BLE001
            W.log("".join(traceback.format_exception(ex)), True)
        idle_since = time.monotonic()


_last_breath = [0.0]


def _breathe() -> None:
    """Called inside long Python loops: after ~1 ms of work, let the game thread have the GIL for 1 ms."""
    now = time.perf_counter()
    if now - _last_breath[0] > 0.001:
        time.sleep(0.001)
        _last_breath[0] = time.perf_counter()


# endregion
# region Jobs


def _account() -> store.Account | None:
    if W.store is None:
        # The mod was never switched on this session (configure() runs at enable), but its page can
        # still be open: a bare assert showed the player "stage failed ()" (2026-10-31)
        raise restore.RestoreError("Save Backups is switched off. Switch it on in MODS first")
    return W.store.active()


def backup(label: str) -> None:
    """Every account's saves (the launch backup), or the active one's (by hand)."""
    st = W.store
    assert st is not None
    st.clean_partials()
    accounts = st.scan() if label == "launch" else [a for a in [_account()] if a]
    if not accounts:
        if label != "launch":
            say("No saves found to back up.", error=True)
        return
    for acc in accounts:
        name, text = st.backup(acc, label)
        pruned = st.prune(acc, keep_count()) if name else []
        if label == "launch":
            W.last_launch = text
            W.log(f"At launch ({acc.sid}): {text}" + (f" Removed {len(pruned)} old backups." if pruned else ""), False)
        else:
            say(text)


_now_ids = itertools.count(1)


def backup_now(reason: str, timeout: float = 10.0) -> bool:
    """
    For other mods (the Mod Menu, before it loads a mod from another modder's repository): copies the
    active account's saves now, the same way "Back up now" does (each file read until it decrypts
    whole, then plain copies into our own folder; the game's saves are only ever read), and waits up
    to `timeout` s for it. True once the saves are in a backup: a new one, or the newest already
    holds exactly these files. False when the mod isn't on or set up, there are no saves, or it took
    too long (the backup still finishes on the worker). Never call it on the game thread (it waits).
    """
    pkg = sys.modules.get(__package__ or "")
    mod = getattr(pkg, "mod", None)
    if W.store is None or (mod is not None and not getattr(mod, "is_enabled", True)):
        return False
    done = threading.Event()
    result = [False]

    def job() -> None:
        try:
            st, acc = W.store, _account()
            if st is None or acc is None:
                return
            st.clean_partials()
            name, text = st.backup(acc, "before_mod")
            if name:
                st.prune(acc, keep_count())
            W.log(f"Backed up {reason}: {text}", False)
            result[0] = name is not None or text.startswith("Nothing changed")
        finally:
            done.set()

    if threading.current_thread() is W.thread:
        job()  # already on the worker (a job asked): run it here rather than wait for ourselves
    else:
        submit(f"backup_now {next(_now_ids)}", job, "Backing up your saves...")
    return done.wait(max(0.0, timeout)) and result[0]


def refresh() -> None:
    """The live saves (if they changed on disk) and the backup folders (if any came or went)."""
    acc = _account()
    if acc is not None:
        assert W.store is not None
        W.store.refresh_live(acc)


def prune() -> None:
    st = W.store
    assert st is not None
    for acc in st.scan():
        gone = st.prune(acc, keep_count())
        if gone:
            W.log(f"Removed {len(gone)} old backups ({acc.sid}).", False)


def set_keep(name: str, keep: bool) -> None:
    acc = _account()
    if acc is not None and W.store is not None and W.store.set_keep(acc, name, keep):
        if not keep:
            W.store.prune(acc, keep_count())


def stage(kind: str, backup_name: str, fname: str = "") -> None:
    st, acc = W.store, _account()
    if st is None or acc is None:
        raise restore.RestoreError("no saves found")
    st.refresh_live(acc)
    if kind == "character":
        what = restore.stage_character(st, acc, backup_name, fname)
    elif kind == "bank":
        what = restore.stage_bank(st, acc, backup_name)
    elif kind == "profile":
        what = restore.stage_profile(st, acc, backup_name)
    else:
        what, skipped = restore.stage_all(st, acc, backup_name)
        if skipped:
            what += f" (left out: {'; '.join(skipped)})"
    if not restore.start_helper(st.base):
        restore.cancel(acc)
        raise restore.RestoreError("couldn't start the helper that restores after the game closes")
    say(f"Will restore {what} when you quit the game.")


def cancel(target: str | None) -> None:
    acc = _account()
    if acc is not None and restore.cancel(acc, target):
        say("Restore cancelled." if target is None else "Taken off the restore.")


def cancel_all_accounts() -> None:
    """The mod was switched off: a switched-off mod mustn't restore anything at exit."""
    st = W.store
    if st is None:
        return
    for acc in st.scan():
        if restore.cancel(acc):
            W.log("Switched off: the restore waiting for the game to close was cancelled.", False)


def dismiss_result() -> None:
    acc = _account()
    if acc is not None:
        restore.result_seen(acc)


def at_enable(launch: bool) -> None:
    """Enable: the launch backup (once per game process), and a helper for a restore staged earlier."""
    st = W.store
    assert st is not None
    if launch:
        submit("backup", lambda: backup("launch"), "Backing up your saves...")
    else:
        submit("refresh", refresh)

    def resume() -> None:
        if restore.any_pending(st.base):
            restore.start_helper(st.base)

    submit("resume", resume)


def open_folder() -> None:
    acc = _account()
    folder = acc.dir if acc is not None else W.store.base if W.store else None
    if folder is None:
        return
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(folder)  # noqa: S606 - Explorer on our own folder


# endregion
# region The page's view


def _publish() -> None:
    st = W.store
    if st is None:
        return
    acc = _account()
    if acc is None:
        view: dict[str, Any] = {"account": "", "count": 0, "characters": [], "bank": {"versions": []}, "sessions": []}
        W.pending, W.result = [], None
    else:
        if not acc.live:
            st.refresh_live(acc)
        view = st.view(acc, W.keep)
        plan = restore.pending(acc)
        W.pending = [{"what": a.get("what", ""), "target": a.get("target", "")} for a in (plan or {}).get("actions", [])]
        W.result = restore.last_result(acc)
        W.account = str(acc.sid)
    text = json.dumps(view, separators=(",", ":"))
    if text != W.view_text and W.ui_dir is not None:
        W.ui_dir.mkdir(parents=True, exist_ok=True)
        target = W.ui_dir / "view.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        W.view_text = text
        W.rev += 1
    W.changed()


# endregion
