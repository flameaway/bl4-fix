"""
Records every error any mod logs, for bug reports (report.py zips them) and the "N errors" on the
mods list.

Every mod logs through `from unrealsdk import logging` + `logging.error(...)`, which looks `error`
up on the module at call time, so one wrapper around `unrealsdk.logging.error` sees them all (ours
and other modders'). The wrapper calls the real function with the same arguments; recording is
inside try/except and never stops the log line.

- Who: the innermost traceback frame inside a mod folder (`sdk_mods/<id>.sdkmod/`, `dev/<id>/`,
  `mods/<id>/`, ...), shared package copies (frametick, health, uilink) and the SDK's own packages
  only if nothing else matches; else the caller of logging.error; else a "[Name]" prefix.
- Same error again: one signature (exception type + innermost frames, or the message with numbers
  blanked), counted. At most MAX_SIGS signatures a session; the rest only count as "other".
- Our own mods (bl4_*): after QUIET_AFTER identical errors in a session, further repeats stay out
  of the log (one line says so) and are only counted. An error every frame flooded the log with
  92k tracebacks once.
- File: settings/bl4fix_errors.jsonl. A line per new signature per session (written on the next
  game tick, with the map and co-op role read there), count lines at most every FLUSH_S while
  counts moved. Capped at MAX_FILE (the newest half kept, temp + rename, on a worker thread).

Cost: nothing until an error is logged (the tick is one list check and, while counts are dirty,
one clock compare). State lives on sys.modules[HUB], so a hot reload keeps the session's records
and replaces the wrapper instead of stacking a second one.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Any

import unrealsdk.logging as sdk_logging

HUB = "_bl4fix_errors"
FILE_NAME = "bl4fix_errors.jsonl"
MAX_SIGS = 200
MAX_FILE = 512 * 1024
FLUSH_S = 30.0
QUIET_AFTER = 20
MAX_TRACE = 8000
MAX_MSG = 300
# Packages every one of our mods carries a copy of: an error in them belongs to whoever called in
SHARED = frozenset({"frametick", "health", "uilink"})
# The SDK's own packages: a mod's error often ends inside them
FRAMEWORK = frozenset({"mods_base", "unrealsdk", "pyunrealsdk", "keybinds", "ui_utils", "networking",
                       "save_options", "console_mod_menu", "sdk_mods", "bl4_bridge"})  # fmt: skip
_PATH = re.compile(r"[\\/](?:sdk_mods|dev|mods|ported)[\\/]([A-Za-z_]\w*?)(?:\.sdkmod)?[\\/](.+)$")
_TB_FILE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_NUMBERS = re.compile(r"0x[0-9a-fA-F]+|\d+")
_PREFIX = re.compile(r"^\s*\[([^\]]{2,40})\]")


def _hub() -> types.ModuleType:
    hub = sys.modules.get(HUB)
    if hub is None:
        hub = types.ModuleType(HUB)
        hub.original = None  # the real unrealsdk.logging.error
        hub.wrapper = None  # ours, installed
        hub.active = False
        hub.lock = threading.Lock()
        hub.session = f"{datetime.datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}"
        hub.records = {}  # sig -> record dict (the jsonl keys + count)
        hub.by_mod = {}  # mod id (or "") -> set of sigs
        hub.other = 0  # errors past MAX_SIGS
        hub.pending = []  # new records waiting for the game tick (map, role, file line)
        hub.dirty = set()  # sigs whose count moved since the last count flush
        hub.flushed_at = 0.0
        hub.revision = 0  # moves with each new signature (the mods list's "N errors")
        hub.path = None
        hub.writing = False
        hub.queue = []  # lines for the writer thread
        sys.modules[HUB] = hub
    # Added later: a hub an older copy made gets them now (fields are only ever added)
    for name, value in (("installed_utc", ""), ("log_path", None), ("log_offset", 0), ("log_block", None),
                        ("scan_event", None), ("scanning", False), ("scan_asked", 0.0),
                        ("scan_lock", threading.Lock())):  # fmt: skip
        if not hasattr(hub, name):
            setattr(hub, name, value)
    return hub


H = _hub()

# region Attribution


def _mod_of(path: str) -> tuple[str, str] | None:
    """(package, path inside it) for a file in a mod folder."""
    match = _PATH.search(path)
    if match is None:
        return None
    package, rest = match.group(1), match.group(2).replace("\\", "/")
    # An .sdkmod holds one folder named like it: sdk_mods/x.sdkmod/x/...
    return package, rest.removeprefix(package + "/")


def _pick(paths: list[str]) -> str | None:
    """The mod that owns the innermost of these files (innermost last): ours or a modder's first,
    then a shared package copy (named after the mod carrying it), then the SDK."""
    fallback: str | None = None
    for path in reversed(paths):
        found = _mod_of(path)
        if found is None:
            continue
        package, rest = found
        if package in FRAMEWORK:
            fallback = fallback or package
            continue
        if rest.split("/", 1)[0] in SHARED:
            fallback = package if fallback is None or fallback in FRAMEWORK else fallback
            continue
        return package
    return fallback


_names: tuple[int, dict[str, tuple[str, str]], dict[str, str]] = (0, {}, {})


def _registry() -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """package -> (mod name, version); lowercase name -> package. Cached while the list is the same."""
    global _names  # noqa: PLW0603
    try:
        mod_list = sys.modules["mods_base.mod_list"].mod_list
    except (KeyError, AttributeError):
        return {}, {}
    key = hash(tuple(id(m) for m in mod_list))
    if key != _names[0]:
        by_pkg: dict[str, tuple[str, str]] = {}
        by_name: dict[str, str] = {}
        for mod in list(mod_list):
            settings = getattr(mod, "settings_file", None)
            package = Path(settings).stem if settings is not None else ""
            if package:
                by_pkg[package] = (str(mod.name), str(getattr(mod, "version", "") or ""))
                by_name[str(mod.name).lower()] = package
        _names = (key, by_pkg, by_name)
    return _names[1], _names[2]


def _by_prefix(message: str) -> str | None:
    """A "[BL4 Direct] ..." / "[bl4_x] ..." line's mod."""
    match = _PREFIX.match(message)
    if match is None:
        return None
    word = match.group(1).strip().lower()
    by_pkg, by_name = _registry()
    if word in by_pkg:
        return word
    for candidate in (word, "bl4 " + word, word.removeprefix("bl4 ")):
        if candidate in by_name:
            return by_name[candidate]
    for name, package in by_name.items():
        if word in name:
            return package
    return None


# endregion
# region Recording (any thread)


def _frames_of(exc: BaseException) -> list[tuple[str, int, str]]:
    """(file, line, function) of each frame, without reading source lines: linecache looks a
    missing file up on every sys.path entry (every .sdkmod zip), ~4 ms a call measured in game."""
    try:
        return [(frame.f_code.co_filename, line or 0, frame.f_code.co_name)
                for frame, line in traceback.walk_tb(exc.__traceback__)]  # fmt: skip
    except Exception:  # noqa: BLE001
        return []


def _short(path: str) -> str:
    found = _mod_of(path)
    return f"{found[0]}/{found[1]}" if found is not None else os.path.basename(path)


# What the wrapper does with the log line after recording it
LOG, QUIET, LOG_AND_NOTE = 0, 1, 2


def _record(args: tuple[Any, ...]) -> int:
    """Notes one logged error. Returns LOG, QUIET (a repeat: leave it out) or LOG_AND_NOTE."""
    message = " ".join(str(a) for a in args)
    exc = sys.exc_info()[1]
    frames: list[tuple[str, int, str]] = []
    if exc is not None:
        frames = _frames_of(exc)
    elif "Traceback (most recent call last)" in message:
        frames = [(m.group(1), int(m.group(2)), m.group(3)) for m in _TB_FILE.finditer(message)]
    mod = _pick([f[0] for f in frames])
    if mod is None or mod in FRAMEWORK:
        # Logged without a traceback (or one only in the SDK): whoever called logging.error
        try:
            caller = sys._getframe(2)  # noqa: SLF001 - _record <- the wrapper <- the caller
            outer: list[str] = []
            while caller is not None and len(outer) < 8:
                outer.append(caller.f_code.co_filename)
                caller = caller.f_back
            found = _pick(outer[::-1])
            if found is not None and (mod is None or found not in FRAMEWORK):
                mod = found
        except Exception:  # noqa: BLE001, S110
            pass
    mod = mod or _by_prefix(message)

    if exc is not None or frames:
        kind = type(exc).__name__ if exc is not None else (message.strip().splitlines() or [""])[-1].split(":", 1)[0]
        basis = kind + "|" + "|".join(f"{_short(f[0])}:{f[1]}:{f[2]}" for f in frames[-3:])
    else:
        basis = _NUMBERS.sub("#", message)[:200]
    sig = hashlib.sha1(f"{mod}|{basis}".encode("utf-8", "replace")).hexdigest()[:12]

    trace = message
    if sig not in H.records and exc is not None and "Traceback (most recent call last)" not in message:
        # A new error: its full traceback (with source lines) once; repeats only count
        trace = message + "\n" + "".join(traceback.format_exception(exc))
    first = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if exc is not None and (not first or first.startswith("Traceback")):
        first = f"{type(exc).__name__}: {exc}"
    count = _note(sig, mod, first, trace)
    if count <= 1 or not (mod and mod.startswith("bl4_")) or count < QUIET_AFTER:
        return LOG
    return LOG_AND_NOTE if count == QUIET_AFTER else QUIET


def _note(sig: str, mod: str | None, first: str, trace: str, *, when: str | None = None,
          source: str | None = None) -> int:  # fmt: skip
    """Counts one error under its signature, making the record if it's new. Returns its count
    (1 = new, 0 = past MAX_SIGS)."""
    hub = H
    with hub.lock:
        record = hub.records.get(sig)
        if record is not None:
            record["count"] += 1
            hub.dirty.add(sig)
            return record["count"]
        if len(hub.records) >= MAX_SIGS:
            hub.other += 1
            return 0
        by_pkg, _ = _registry()
        record = {
            "t": when or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "session": hub.session,
            "mod": mod,
            "version": by_pkg.get(mod or "", ("", None))[1] or None,
            "sig": sig,
            "msg": first[:MAX_MSG],
            "trace": trace[-MAX_TRACE:],
            "map": None,
            "role": None,
            "count": 1,
        }
        if source is not None:
            record["from"] = source
        hub.records[sig] = record
        hub.by_mod.setdefault(mod or "", set()).add(sig)
        hub.pending.append(record)
        hub.revision += 1
        return 1


def _trampoline(original: Any, args: Any, kwargs: Any) -> Any:
    return original(*args, **kwargs)  # must stay on the line right after the def (see _call_as)


# (file, line) of a logging.error call -> a copy of _trampoline that claims to be that line
_sites: dict[tuple[str, int], Any] = {}
MAX_SITES = 1024


def _site(caller: types.FrameType | None) -> Any:
    """
    A copy of the trampoline that looks like the caller's line, or None. The SDK writes the calling
    Python frame's file@line into each log line; called from the wrapper, every error in the log
    said errors.py@<line> (seen 2026-10-29). Calling through this keeps the log as it was.
    """
    if caller is None:
        return None
    key = (caller.f_code.co_filename, caller.f_lineno or 1)
    fn = _sites.get(key)
    if fn is None:
        code = _trampoline.__code__.replace(co_filename=key[0], co_firstlineno=max(1, key[1] - 1),
                                            co_name=caller.f_code.co_name)  # fmt: skip
        fn = types.FunctionType(code, {})
        if len(_sites) < MAX_SITES:
            _sites[key] = fn
    return fn


def _error(*args: Any, **kwargs: Any) -> Any:
    """unrealsdk.logging.error, recorded."""
    hub = H
    original = hub.original
    what = LOG
    if hub.active:
        try:
            what = _record(args)
        except Exception:  # noqa: BLE001, S110 - recording must never cost the log line
            pass
    if what == QUIET:
        return None
    try:
        site = _site(sys._getframe(1))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        site = None
    result = site(original, args, kwargs) if site is not None else original(*args, **kwargs)
    if what == LOG_AND_NOTE:
        original(f"[Mod Menu] The error above happened {QUIET_AFTER} times this session: further repeats"
                 " are only counted (for bug reports), not logged.")  # fmt: skip
    logged = getattr(hub, "on_logged", None)  # reporting.copy_log_soon: the log copy for bug reports
    if logged is not None:
        logged()
    return result


# endregion
# region Install


def install(settings_dir: Path, log_path: Path | None = None) -> None:
    """Wraps unrealsdk.logging.error (once: a reload replaces our wrapper, never stacks one).
    `log_path`: the SDK's log, read by scan_log for the errors that don't pass through the wrapper."""
    hub = H
    hub.path = Path(settings_dir) / FILE_NAME
    if log_path is not None:
        hub.log_path = Path(log_path)
    if not hub.installed_utc:
        # The log's own clock (UTC, ms): its error lines before this never passed through the wrapper
        hub.installed_utc = f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%S.%f}"[:-3] + "Z"
    current = sdk_logging.error
    if hub.original is None:
        hub.original = current
    elif current is not hub.wrapper and current is not hub.original:
        # Someone wrapped ours since: theirs calls our old wrapper, which keeps recording (same hub).
        # Wrapping theirs again would make a loop through it.
        hub.active = True
        return
    hub.wrapper = _error
    hub.active = True
    sdk_logging.error = _error


def uninstall() -> None:
    """Puts the real function back (or, if another wrapper went on top of ours, just stops recording)."""
    hub = H
    hub.active = False
    flush(force=True)
    if sdk_logging.error is hub.wrapper and hub.original is not None:
        sdk_logging.error = hub.original
        hub.wrapper = None


# endregion
# region Game thread


def _context() -> tuple[str | None, str | None]:
    """The map and the co-op role, read on the game thread."""
    world_name = None
    try:
        from mods_base import ENGINE  # noqa: PLC0415

        world = ENGINE.GameViewport.World
        if world is not None:
            world_name = str(world.Name)
    except Exception:  # noqa: BLE001, S110 - a loading screen: no map
        pass
    role = None
    try:
        coop = sys.modules.get("bl4_direct.coop")
        if coop is not None:
            role = str(coop.role())
    except Exception:  # noqa: BLE001, S110
        pass
    return world_name, role


def tick() -> None:
    """Every frame on the game thread: new errors get their map / role and go to the file; counts
    go at most every FLUSH_S. Nothing to do (the usual case) costs a list check."""
    hub = H
    if hub.pending:
        world_name, role = _context()
        with hub.lock:
            new, hub.pending = hub.pending, []
            for record in new:
                if record.get("from") != "log":  # read from the log later: where it happened is unknown
                    record["map"], record["role"] = world_name, role
        _write([{k: v for k, v in r.items() if k != "count"} for r in new])
    if hub.dirty and time.monotonic() - hub.flushed_at >= FLUSH_S:
        flush()


def flush(*, force: bool = False) -> None:
    """Writes the counts that moved (also at disable and before a bug report)."""
    hub = H
    if not hub.dirty:
        return
    hub.flushed_at = time.monotonic()
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with hub.lock:
        sigs, hub.dirty = hub.dirty, set()
        lines = [{"t": now, "session": hub.session, "sig": s, "count": hub.records[s]["count"]}
                 for s in sigs if s in hub.records]  # fmt: skip
    _write(lines, wait=force)


def _write(entries: list[dict[str, Any]], *, wait: bool = False) -> None:
    hub = H
    if not entries or hub.path is None:
        return
    text = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for e in entries)
    with hub.lock:
        hub.queue.append(text)
        if hub.writing:
            return
        hub.writing = True
    if wait:
        _drain()
    else:
        threading.Thread(target=_drain, name="bl4_mod_menu.errors", daemon=True).start()


def _drain() -> None:
    """Appends what's queued (a worker: file I/O stays off the game thread), trimming when too big."""
    hub = H
    try:
        while True:
            with hub.lock:
                if not hub.queue:
                    hub.writing = False
                    return
                text, hub.queue = "".join(hub.queue), []
            try:
                path: Path = hub.path
                with path.open("a", encoding="utf-8", newline="\n") as file:
                    file.write(text)
                if path.stat().st_size > MAX_FILE:
                    _trim(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        with hub.lock:
            hub.writing = False


def _trim(path: Path) -> None:
    data = path.read_bytes()
    cut = data.find(b"\n", len(data) - MAX_FILE // 2)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data[cut + 1 :] if cut >= 0 else b"")
    os.replace(temp, path)


# endregion
# region The SDK's own error lines (a worker thread)
#
# Some errors never pass through logging.error: an exception a hook didn't catch (pyunrealsdk formats
# it in C++, pybind11 style: "Type: message", "", "At:", "  file(line): function"; probed in game
# 2026-10-29: no Python function runs, so there's nothing to wrap), the SDK's own C++ errors
# (location "namespace::function@line"), and anything logged before install (a mod that failed to
# import at start). They're in unrealsdk.log, so that's read: only while the mods screen asks
# (scan_soon, at most every SCAN_S) and before a bug report, only the bytes added since, on a worker.
# Lines the wrapper already recorded are skipped (logged after install from Python).

SCAN_S = 3.0
SCAN_CHUNK = 32 * 1024  # one piece's GIL time stays ~1 ms even in a log full of tracebacks
SCAN_IDLE_S = 60.0
_AT_FRAME = re.compile(r"^\s+(.+)\((\d+)\): (\S+)\s*$")
_ERR = b" ERR| "
# (location, a block's text) -> its signature: repeats skip the parsing (scanner thread only)
_block_sigs: dict[tuple[str, str], str] = {}


def scan_soon() -> None:
    """Asks the scanner to read the log's new lines (any thread; cheap: an Event at most every SCAN_S)."""
    hub = H
    if hub.log_path is None:
        return
    now = time.monotonic()
    if now - hub.scan_asked < SCAN_S:
        return
    hub.scan_asked = now
    with hub.lock:
        if hub.scan_event is None:
            hub.scan_event = threading.Event()
        hub.scan_event.set()
        if hub.scanning:
            return
        hub.scanning = True
    threading.Thread(target=_scanner, name="bl4_mod_menu.errors_scan", daemon=True).start()


def _scanner() -> None:
    """Scans whenever asked; ends after SCAN_IDLE_S without an ask (the next ask starts it again)."""
    hub = H
    event: threading.Event = hub.scan_event
    try:
        while event.wait(SCAN_IDLE_S):
            event.clear()
            scan_log()
    except Exception as ex:  # noqa: BLE001
        sdk_logging.warning(f"[Mod Menu] Reading unrealsdk.log for errors failed: {ex!r}")
    finally:
        with hub.lock:
            hub.scanning = False


def scan_log() -> int:
    """Records the error lines added to the log since the last scan. Returns how many blocks were
    taken. Any thread but the game's (file reads, ~1 ms pieces)."""
    hub = H
    path: Path | None = hub.log_path
    lock: threading.Lock = hub.scan_lock
    if path is None or not lock.acquire(blocking=False):
        return 0
    taken = 0
    try:
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if size < hub.log_offset:  # truncated: another game instance started (it shares the file)
            hub.log_offset, hub.log_block = 0, None
        with path.open("rb") as file:
            while hub.log_offset < size:
                file.seek(hub.log_offset)
                data = file.read(min(SCAN_CHUNK, size - hub.log_offset))
                cut = data.rfind(b"\n")
                if cut < 0:
                    if len(data) < SCAN_CHUNK:
                        break  # a line still being written
                    cut = len(data) - 1  # a line longer than a piece: skip through it
                taken += _scan_piece(data[: cut + 1])
                hub.log_offset += cut + 1
                time.sleep(0.001)  # hands the GIL back between pieces (not sleep(0), see CLAUDE.md)
        taken += _end_block()
    finally:
        lock.release()
    return taken


def _scan_piece(data: bytes) -> int:
    """The ERR lines of whole log lines, grouped into one block per message (a message's lines share
    the time, thread and location). The last block stays open: it may go on in the next piece."""
    taken = 0
    # End of the last ERR line seen: a block only goes on in the very next line (-1: the previous
    # piece ended with one, so the first line here may continue it)
    last_end = -1 if H.log_block is not None else -2
    pos = data.find(_ERR)
    while pos >= 0:
        start = data.rfind(b"\n", 0, pos) + 1
        end = data.find(b"\n", pos)
        end = len(data) if end < 0 else end
        line = data[start:end].rstrip(b"\r").decode("utf-8", "replace")
        head, _, text = line.partition(" ERR| ")
        parts = head.split(None, 3)  # date, time, thread, location (may start "~ " and hold spaces)
        if len(parts) == 4:
            key = (f"{parts[0]} {parts[1]}", parts[2], parts[3].strip().removeprefix("~ ").strip())
            block = H.log_block
            if block is not None and block[0] == key and start == last_end + 1:
                block[1].append(text)
            else:
                taken += _end_block()
                H.log_block = (key, [text])
            last_end = end
        pos = data.find(_ERR, end)
    if H.log_block is not None and last_end < len(data) - 1:
        taken += _end_block()  # other lines came after it: it's whole
    return taken


def _end_block() -> int:
    """Records the open block if it's one the wrapper didn't see. Returns 1 if it was taken."""
    hub = H
    block, hub.log_block = hub.log_block, None
    if block is None:
        return 0
    (when, _thread, location), lines = block
    uncaught = any(line.strip() == "At:" for line in lines)
    native = "::" in location  # C++: "unrealsdk::...@59"; Python frames are "file@line"
    if not (uncaught or native or (hub.installed_utc and when < hub.installed_utc)):
        return 0
    text = "\n".join(lines)
    seen = _block_sigs.get((location, text))
    if seen is not None:
        # The same block again (an error every frame): only its count moves, ~2 us instead of ~30
        _note(seen, None, "", "")
        return 1
    first = next((line.strip() for line in lines if line.strip()), "")
    frames: list[tuple[str, int, str]] = []
    if uncaught:
        at = lines.index(next(line for line in lines if line.strip() == "At:"))
        for line in lines[at + 1 :]:
            if (match := _AT_FRAME.match(line)) is not None:
                frames.append((match.group(1), int(match.group(2)), match.group(3)))
        frames.reverse()  # pybind11 lists the innermost frame first
    elif "Traceback (most recent call last)" in text:
        frames = [(m.group(1), int(m.group(2)), m.group(3)) for m in _TB_FILE.finditer(text)]
    where = location.rsplit("@", 1)[0]
    mod = _pick([where, *(f[0] for f in frames)]) if not native else "unrealsdk"
    mod = mod or _by_prefix(first)
    if frames:
        kind = first.split(":", 1)[0] if uncaught else (text.strip().splitlines() or [""])[-1].split(":", 1)[0]
        basis = kind + "|" + "|".join(f"{_short(f[0])}:{f[1]}:{f[2]}" for f in frames[-3:])
    else:
        basis = (location if native else "") + "|" + _NUMBERS.sub("#", first)[:200]
    sig = hashlib.sha1(f"{mod}|{basis}".encode("utf-8", "replace")).hexdigest()[:12]
    try:
        # By hand: strptime's first call imports _strptime and builds locale tables (~10 ms, GIL held)
        stamp = datetime.datetime(int(when[0:4]), int(when[5:7]), int(when[8:10]), int(when[11:13]),
                                  int(when[14:16]), int(when[17:19]), tzinfo=datetime.timezone.utc)  # fmt: skip
        local = stamp.astimezone().isoformat(timespec="seconds")
    except ValueError:
        local = None
    label = "Uncaught in a hook" if uncaught else ("SDK" if native else "Logged before the Mod Menu loaded")
    if _note(sig, mod, first, f"[{label}, {location}]\n{text}", when=local, source="log") and len(_block_sigs) < MAX_SIGS * 4:
        _block_sigs[(location, text)] = sig
    return 1


# endregion
# region Reading (any thread)


def counts() -> dict[str, int]:
    """mod package -> how many different errors it logged this session."""
    with H.lock:
        return {mod: len(sigs) for mod, sigs in H.by_mod.items() if mod and sigs}


def revision() -> int:
    return H.revision


def entries(limit: int = MAX_SIGS) -> list[dict[str, Any]]:
    """This session's errors, newest first (copies: the jsonl keys + count)."""
    with H.lock:
        records = [dict(r) for r in H.records.values()]
    return records[::-1][:limit]


def summary(limit: int = 12) -> dict[str, Any]:
    """The small view the mods screen shows: totals and the newest errors' first lines."""
    hub = H
    with hub.lock:
        recent = list(hub.records.values())[-limit:][::-1]
        return {
            "rev": hub.revision,
            "errors": len(hub.records) + (1 if hub.other else 0),
            "times": sum(r["count"] for r in hub.records.values()) + hub.other,
            "recent": [{"mod": r["mod"], "msg": r["msg"], "count": r["count"]} for r in recent],
        }


# endregion
