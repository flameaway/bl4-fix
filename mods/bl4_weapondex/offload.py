"""
The pictures' pure-Python work (picture.compose for a boss, the colour swap for an item, the PNGs,
the small copies, the folder indexes) on interpreters of their own (Python 3.14's
concurrent.interpreters, the game's Python): each has its own GIL, so the work runs on other cores
at full speed and never makes the game thread wait for the GIL.

Why: on a worker thread of the game's interpreter, compose shared one GIL with the game thread and
the items' writer. It let go of it every millisecond (picture._Breather) so frames didn't stall,
and took ~0.6 s a boss instead of ~0.22 s: in a full title-screen run (118 bosses, 318 items) the
next boss shot waited for it in 87% of frames, and the run lasted as long as compose (85 s;
2026-11-03). Here a boss takes 223 ms, byte for byte the same picture (two real captures, checked
against the thread's compose). Creating an interpreter takes ~120 ms (on its worker thread).

How: WORKERS interpreters, each run by a thread of ours (blocked in Interpreter.exec while its
interpreter works, holding no GIL of the game's), all reading one job queue. Each loads picture.py
from its source (the package itself imports unrealsdk, which only the main interpreter has) and
loops over jobs until it gets None. Jobs and results are tuples of str / int / bytes (copied
across). poll() (from bake.tick, every frame during a run, else every 0.25 s) collects results,
logs failures, writes the indexes once the queue drains (one job, so one writer), and stops the
workers IDLE_S after the last job: no interpreter is left between runs.

Not available (an older Python, an error starting one): available() says so and the callers use
their own worker thread as before (bake._convert_all, thumbs._write_all).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from unrealsdk import logging

WORKERS = max(1, min(3, (os.cpu_count() or 4) // 4))  # 3 on 12+ logical cores; 32 here
IDLE_S = 5.0  # the workers stop this long after the last job
HANG_S = 300.0  # a worker nobody stopped (the module's state lost) stops by itself after this
STUCK_S = 30.0  # no worker alive and no result this long: the jobs still counted are given up
THREAD = "weapondex-offload"

# Runs in each worker interpreter: `src` (picture.py), `path`, `jobs`, `results`, `hang` given
_SCRIPT = """
import sys, types
from concurrent import interpreters
from pathlib import Path
pic = types.ModuleType("kdx_picture")
pic.__file__ = path
exec(compile(src, path, "exec"), pic.__dict__)
sys.modules["kdx_picture"] = pic
pic.SLICE_S = 1e9  # its own GIL: nothing to let go of


def save(png, width, height, rgba):
    png.parent.mkdir(parents=True, exist_ok=True)
    pic.write_png(png, width, height, rgba)
    w, h, small = pic.shrink(width, height, rgba)
    target = pic.small_path(png)
    target.parent.mkdir(parents=True, exist_ok=True)
    pic.write_png(target, w, h, small)


while True:
    try:
        job = jobs.get(timeout=hang)
    except interpreters.QueueEmpty:
        break
    if job is None:
        break
    kind, ident = job[0], job[1]
    try:
        if kind == "boss":
            png, width, height, hdr, *passes = job[2:]
            if len(passes) == 3:
                rgba = pic.compose(width, height, hdr, *passes)
            else:
                rgba = pic.from_capture(width, height, hdr)
            save(Path(png), width, height, rgba)
        elif kind == "item":
            png, width, height, bgra = job[2:]
            rgba = bytearray(len(bgra))  # FColor order (B, G, R, A) as RGBA
            rgba[0::4] = bgra[2::4]
            rgba[1::4] = bgra[1::4]
            rgba[2::4] = bgra[0::4]
            rgba[3::4] = bgra[3::4]
            save(Path(png), width, height, bytes(rgba))
        elif kind == "index":
            for folder in job[2]:
                pic.write_index(Path(folder))
                pic.write_small_index(Path(folder))
        results.put(("ok", kind, ident, ""))
    except BaseException as ex:
        results.put(("error", kind, ident, repr(ex)))
"""


class _State:
    def __init__(self) -> None:
        self.checked = False
        self.unavailable: str | None = None
        self.interpreters: Any = None  # the concurrent.interpreters module
        self.src = ""
        self.path = ""
        self.jobs: Any = None
        self.results: Any = None
        self.threads: list[threading.Thread] = []
        self.outstanding: dict[str, int] = {}  # kind -> jobs submitted, no result yet
        self.dirty: set[str] = set()  # folders whose indexes are behind
        self.last = 0.0  # the last submit or result
        self.active = False  # workers started and not yet told to stop


# Survives a hot reload (the workers hold its queues)
_ST: _State = globals().get("_ST") or _State()


def log_error(msg: str) -> None:
    logging.error(f"[Kairosdex] {msg}")


def available() -> bool:
    """Whether jobs can go to interpreters of their own (checked once)."""
    st = _ST
    if not st.checked:
        st.checked = True
        try:
            from concurrent import interpreters  # noqa: PLC0415 - Python 3.14+

            from . import picture  # noqa: PLC0415

            st.src = picture.__loader__.get_source(picture.__name__) or ""
            st.path = str(getattr(picture, "__file__", "picture.py"))
            if not st.src:
                raise RuntimeError("picture.py's source can't be read")
            st.interpreters = interpreters
            st.jobs = interpreters.create_queue()
            st.results = interpreters.create_queue()
        except Exception as ex:  # noqa: BLE001
            st.unavailable = repr(ex)
            logging.dev_warning(f"[Kairosdex] pictures on worker threads (no interpreters of their own: {ex!r})")
    return st.unavailable is None


def submit(kind: str, ident: str, png: Path, *payload: Any) -> None:
    """A "boss" (width, height, hdr, *passes) or "item" (width, height, bgra) picture to make into png."""
    st = _ST
    st.jobs.put((kind, ident, str(png), *payload))
    st.outstanding[kind] = st.outstanding.get(kind, 0) + 1
    st.dirty.add(str(png.parent))
    st.last = time.monotonic()
    _ensure_workers()


def dirty(folder: Path) -> None:
    """This folder's indexes are written again once the queue drains."""
    if not available():
        return
    _ST.dirty.add(str(folder))
    _ST.last = time.monotonic()
    _ST.active = True


def backlog(kind: str) -> int:
    """Jobs of this kind still being made (0 when no worker is alive to make them)."""
    poll()
    if not any(t.is_alive() for t in _ST.threads):
        return 0
    return _ST.outstanding.get(kind, 0)


def busy() -> bool:
    """Jobs are still being made."""
    poll()
    return any(_ST.outstanding.values())


def _ensure_workers() -> None:
    st = _ST
    st.threads = [t for t in st.threads if t.is_alive()]
    st.active = True
    while len(st.threads) < WORKERS:
        t = threading.Thread(target=_work, args=(st, len(st.threads)), name=THREAD, daemon=True)
        st.threads.append(t)
        t.start()


def _work(st: _State, index: int) -> None:
    """A worker thread of the game's interpreter: runs one interpreter of its own until it's told to stop."""
    interp = None
    try:
        interp = st.interpreters.create()
        interp.prepare_main(src=st.src, path=st.path, jobs=st.jobs, results=st.results, hang=HANG_S)
        interp.exec(_SCRIPT)
    except Exception as ex:  # noqa: BLE001
        st.results.put(("error", "worker", str(index), repr(ex)))
    finally:
        if interp is not None:
            try:
                interp.close()
            except Exception as ex:  # noqa: BLE001
                st.results.put(("error", "worker", str(index), f"closing: {ex!r}"))


def poll() -> None:
    """Game thread: results in, failures logged, indexes written when the queue drains, idle workers stopped."""
    st = _ST
    if not st.active:
        return
    now = time.monotonic()
    while True:
        try:
            status, kind, ident, why = st.results.get_nowait()
        except st.interpreters.QueueEmpty:
            break
        st.last = now
        if kind in st.outstanding:
            st.outstanding[kind] = max(0, st.outstanding[kind] - 1)
        if status != "ok":
            if kind == "worker":
                log_error(f"picture worker {ident}: {why}")
            else:
                log_error(f"Couldn't make the picture of {ident}: {why}")
    alive = [t for t in st.threads if t.is_alive()]
    if any(st.outstanding.values()):
        if not alive and now - st.last > STUCK_S:
            log_error(f"picture workers gone with {sum(st.outstanding.values())} picture(s) unmade")
            st.outstanding.clear()
        elif not alive:
            _ensure_workers()  # one stopped (an error): the queue still holds jobs
        return
    if st.dirty:
        folders = sorted(st.dirty)
        st.dirty.clear()
        st.jobs.put(("index", "indexes", folders))
        st.outstanding["index"] = 1
        st.last = now
        _ensure_workers()
        return
    if now - st.last >= IDLE_S:
        stop()


def stop() -> None:
    """Tells every worker to stop once the jobs before it are done (each takes one None)."""
    st = _ST
    st.threads = [t for t in st.threads if t.is_alive()]
    for _ in st.threads:
        st.jobs.put(None)
    st.threads = []
    st.active = False
