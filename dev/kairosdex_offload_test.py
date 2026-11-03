"""
Checks Kairosdex's picture work on interpreters of their own (mods/bl4_weapondex/offload.py)
offline. Python 3.11 has no concurrent.interpreters, so a stand-in runs each worker's script in a
thread of this interpreter, with queues that behave like the real ones; what's checked is offload's
own logic and its worker script:
- a boss job writes the same picture as picture.compose called directly, plus its small copy;
- an item job writes the BGRA read back as RGBA, plus its small copy;
- once every job is done, one index job writes index.json and small/index.json for both folders;
- a bad job is logged and counted done (the run never waits on it); backlog() counts jobs;
- the worker's copy of picture.py doesn't breathe (SLICE_S), the game's copy still does;
- idle workers are told to stop and do.
(Checked in game too, 2026-11-03: two real captures byte for byte, and a full title-screen run.)

    python dev/kairosdex_offload_test.py
"""

from __future__ import annotations

import importlib.util
import json
import queue
import struct
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_weapondex"
PKG = "kdx_offload_test"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: object, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# region Stand-ins

logs: list[str] = []
unrealsdk = types.ModuleType("unrealsdk")
unrealsdk.logging = types.SimpleNamespace(info=logs.append, error=logs.append, warning=logs.append, dev_warning=logs.append)
sys.modules["unrealsdk"] = unrealsdk


class QueueEmpty(Exception):  # noqa: N818 - the real module's name
    pass


class FakeQueue:
    """concurrent.interpreters.Queue's calls offload uses."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()

    def put(self, item: object) -> None:
        self.q.put(item)

    def get(self, timeout: float | None = None) -> object:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            raise QueueEmpty from None

    def get_nowait(self) -> object:
        try:
            return self.q.get_nowait()
        except queue.Empty:
            raise QueueEmpty from None

    def empty(self) -> bool:
        return self.q.empty()

    def qsize(self) -> int:
        return self.q.qsize()


created: list[FakeInterp] = []


class FakeInterp:
    """Runs the script in the calling thread, with prepare_main's names as its globals."""

    def __init__(self) -> None:
        self.ns: dict[str, object] = {"__name__": "__main__"}
        self.closed = False
        created.append(self)

    def prepare_main(self, **names: object) -> None:
        self.ns.update(names)

    def exec(self, code: str) -> None:
        exec(compile(code, "<worker>", "exec"), self.ns)  # noqa: S102

    def close(self) -> None:
        self.closed = True


interpreters = types.ModuleType("concurrent.interpreters")
interpreters.QueueEmpty = QueueEmpty
interpreters.create_queue = FakeQueue
interpreters.create = FakeInterp
import concurrent  # noqa: E402

concurrent.interpreters = interpreters
sys.modules["concurrent.interpreters"] = interpreters

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(MOD)]
sys.modules[PKG] = pkg


def load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", MOD / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    setattr(pkg, name, module)
    spec.loader.exec_module(module)
    return module


picture = load("picture")
offload = load("offload")
# endregion


def floats(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def capture(width: int, height: int) -> tuple[bytes, bytes, bytes, bytes]:
    """A square boss in the middle: lit, painted, facing the camera, with a depth step for an ink line."""
    hdr, base, normal, depth = [], [], [], []
    for y in range(height):
        for x in range(width):
            inside = 3 <= x < width - 3 and 3 <= y < height - 3
            hdr += [0.4 + 0.02 * x, 0.3, 0.2, 0.0] if inside else [0.0, 0.0, 0.0, 1.0]
            base += [0.6, 0.4 - 0.01 * y, 0.2, 1.0] if inside else [0.0, 0.0, 0.0, 1.0]
            normal += [0.0, 0.0, 1.0, 1.0]
            depth += [(100.0 if x < width // 2 else 140.0) if inside else 65504.0, 0.0, 0.0, 1.0]
    return floats(hdr), floats(base), floats(normal), floats(depth)


def wait(cond, timeout: float = 20.0) -> bool:  # noqa: ANN001
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        offload.poll()
        if cond():
            return True
        time.sleep(0.01)
    return False


def main() -> int:
    offload.IDLE_S = 0.3
    check("available with interpreters", offload.available(), str(offload._ST.unavailable))  # noqa: SLF001
    check("the workers get picture.py's source", "def compose(" in offload._ST.src)  # noqa: SLF001
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bosses, items = root / "bosses", root / "items"
        w = h = 16
        cap = capture(w, h)
        offload.submit("boss", "Test Boss", bosses / "test-boss.png", w, h, *cap)
        bgra = bytes(b for i in range(8 * 4) for b in (10 + i, 20 + i, 30 + i, 255))  # B, G, R, A
        offload.submit("item", "test-item", items / "test.item.png", 8, 4, bgra)
        check("backlog counts boss jobs", offload.backlog("boss") <= 1)
        done = wait(lambda: not offload.busy() and (bosses / "index.json").is_file() and (items / "small" / "index.json").is_file())
        check("every job and the indexes done", done, f"outstanding {offload._ST.outstanding}")  # noqa: SLF001

        want = picture.compose(w, h, *cap)
        got = picture.read_png(bosses / "test-boss.png")
        check("boss: the same picture as compose called directly", got == (w, h, want), str(got and got[:2]))
        small = picture.read_png(bosses / "small" / "test-boss.png")
        check("boss: its small copy", small is not None and small[:2] == (w // 4, h // 4), str(small and small[:2]))
        item = picture.read_png(items / "test.item.png")
        rgba = bytearray(bgra)
        rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]
        check("item: BGRA written as RGBA", item == (8, 4, bytes(rgba)), str(item and item[:2]))
        check("item: its small copy", (items / "small" / "test.item.png").is_file())
        index = {f: json.loads((root / f / "index.json").read_text()) for f in ("bosses", "items", "bosses/small", "items/small")}
        check("indexes list each picture", list(index["bosses"]) == ["test-boss"] and list(index["items"]) == ["test.item"]
              and list(index["bosses/small"]) == ["test-boss"] and list(index["items/small"]) == ["test.item"], str(index))  # fmt: skip

        logs.clear()
        offload.submit("boss", "Short Boss", bosses / "short.png", w, h, cap[0], cap[1][:-16], cap[2], cap[3])
        done = wait(lambda: not offload.busy())
        check("a bad job: logged, counted done", done and any("Short Boss" in line for line in logs), str(logs))
        check("a bad job: no file", not (bosses / "short.png").exists())
        check("the game's picture.py still breathes", picture.SLICE_S < 1)
        workers = [i for i in created if "pic" in i.ns]
        check("the workers' copy doesn't", workers and all(i.ns["pic"].SLICE_S > 1 for i in workers), str(len(workers)))

        threads = list(offload._ST.threads)  # noqa: SLF001
        check("idle workers told to stop", wait(lambda: not offload._ST.active, 5.0))  # noqa: SLF001
        for t in threads:
            t.join(5.0)
        check("and they stop", not any(t.is_alive() for t in threads), str([t.is_alive() for t in threads]))
        check("their interpreters closed", all(i.closed for i in created), str([i.closed for i in created]))
        check("no worker thread left", not any(t.name == offload.THREAD for t in threading.enumerate()))

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
