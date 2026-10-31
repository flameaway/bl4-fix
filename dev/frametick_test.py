"""
Offline test of shared/frametick (no game): fake unrealsdk + mods_base, two mods' copies.

    python dev/frametick_test.py
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "shared" / "frametick" / "__init__.py"

# region Fakes


class Clock:
    t = 100.0
    frame = 0


hooks_on: dict[tuple[str, object, str], object] = {}
logged: list[str] = []


def _fake_sdk() -> None:
    unrealsdk = types.ModuleType("unrealsdk")
    hooks = types.ModuleType("unrealsdk.hooks")

    class Type:
        PRE, POST = "PRE", "POST"

    hooks.Type = Type
    hooks.add_hook = lambda f, k, i, fn: hooks_on.__setitem__((f, k, i), fn)
    hooks.remove_hook = lambda f, k, i: hooks_on.pop((f, k, i), None)
    hooks.has_hook = lambda f, k, i: (f, k, i) in hooks_on
    log = types.ModuleType("unrealsdk.logging")
    log.error = logged.append

    class _Cdo:
        @staticmethod
        def GetFrameCount() -> int:  # noqa: N802
            return Clock.frame

    unrealsdk.find_class = lambda name: types.SimpleNamespace(ClassDefaultObject=_Cdo)

    def find_object(cls: str, name: str) -> object:
        raise ValueError("not in this test")  # no game image: the counter read stays off

    unrealsdk.find_object = find_object
    unrealsdk.logging = log
    unrealsdk.hooks = hooks
    sys.modules.update({"unrealsdk": unrealsdk, "unrealsdk.hooks": hooks, "unrealsdk.logging": log})

    mods_base = types.ModuleType("mods_base")

    @dataclasses.dataclass
    class HookType:  # the shape of mods_base's (hook.py)
        __wrapped__: object
        hook_identifier: str
        hook_funcs: list = dataclasses.field(default_factory=list)

    mods_base.HookType = HookType
    sys.modules["mods_base"] = mods_base


def _load(package: str, version: int | None = None, src: Path = SRC) -> types.ModuleType:
    """Loads a copy as <package>.frametick (optionally with another VERSION)."""
    if package not in sys.modules:
        parent = types.ModuleType(package)
        parent.__path__ = []
        sys.modules[package] = parent
    name = f"{package}.frametick"
    code = src.read_text(encoding="utf-8")
    if version is not None:
        code = re.sub(r"\nVERSION = \d+\n", f"\nVERSION = {version}\n", code, count=1)
    module = types.ModuleType(name)
    module.__file__ = str(src)
    sys.modules[name] = module
    exec(compile(code, str(src), "exec"), module.__dict__)  # noqa: S102
    return module


# endregion

failures = 0
passed = 0


def check(what: str, ok: bool) -> None:
    global failures, passed
    if ok:
        passed += 1
    else:
        failures += 1
        print(f"FAIL: {what}")


def calls_per_frame(n: int = 3, gap: float = 0.0003, frame_s: float = 0.016) -> None:
    """One engine frame: n hook calls close together."""
    Clock.frame += 1
    for _ in range(n):
        for fn in list(hooks_on.values()):
            fn(None, None, None, None)
        Clock.t += gap
    Clock.t += frame_s


def main() -> int:
    _fake_sdk()
    import time

    time.perf_counter = lambda: Clock.t  # type: ignore[assignment]

    a = _load("mod_a")
    b = _load("mod_b")
    hub_key = a.HUB
    check("no hook before any callback", not hooks_on)

    seen: list[str] = []
    ta = a.tick("a_tick")(lambda: seen.append("a"))
    tb = b.tick("b_tick")(lambda: seen.append("b"))
    check("FrameTick is a HookType with no hook_funcs", isinstance(ta, sys.modules["mods_base"].HookType) and ta.hook_funcs == [])
    ta.enable()
    tb.enable()
    check("one hook for two mods", len(hooks_on) == 1)
    check("entries record their mod", sys.modules[hub_key].entries["a_tick"].mod == "mod_a" and sys.modules[hub_key].entries["b_tick"].mod == "mod_b")
    check("get_active_count", ta.get_active_count() == 1)

    for _ in range(10):
        calls_per_frame()
    check("each callback once a frame", seen.count("a") == 10 and seen.count("b") == 10)
    check("in add order", seen[:2] == ["a", "b"])

    # Two camera updates far apart inside one frame: the frame counter stops the second tick
    seen.clear()
    Clock.frame += 1
    for fn in list(hooks_on.values()):
        fn(None, None, None, None)
    Clock.t += 0.008
    for fn in list(hooks_on.values()):
        fn(None, None, None, None)
    Clock.t += 0.016
    check("a slow frame's second update doesn't tick twice", seen == ["a", "b"])
    check("frame() is the engine frame", a.frame() == Clock.frame and b.frame() == Clock.frame)

    # every=
    slow: list[float] = []
    a.add("a_slow", lambda: slow.append(Clock.t), every=0.25)
    for _ in range(62):  # ~1 s at 16 ms
        calls_per_frame()
    check(f"every=0.25 runs ~4 times a second (got {len(slow)})", 4 <= len(slow) <= 5)
    n = len(slow)
    calls_per_frame()
    check("every= waits between runs", len(slow) == n)
    a.soon("a_slow")
    calls_per_frame()
    check("soon() runs it at the next frame", len(slow) == n + 1)
    calls_per_frame()
    check("then at its pace again", len(slow) == n + 1)
    a.soon("no_such_key")  # harmless
    a.remove("a_slow")

    # An error in one callback: logged once, the others still run
    seen.clear()

    def bad() -> None:
        raise ValueError("boom")

    a.add("a_bad", bad)
    for _ in range(5):
        calls_per_frame()
    check("others still run after an error", seen.count("a") == 5 and seen.count("b") == 5)
    check(f"error logged once (got {sum('a_bad' in m for m in logged)})", sum("a_bad" in m for m in logged) == 1)
    a.remove("a_bad")

    # Newer copy takes the hook over, callbacks kept
    old_fn = next(iter(hooks_on.values()))
    c = _load("mod_c", version=99)
    seen.clear()
    tc = c.tick("c_tick")(lambda: seen.append("c"))
    tc.enable()
    check("still one hook", len(hooks_on) == 1)
    check("newer copy owns the hook", next(iter(hooks_on.values())) is not old_fn and sys.modules[hub_key].owner is c)
    calls_per_frame()
    check("old callbacks kept after takeover", sorted(seen) == ["a", "b", "c"])

    # An older copy loaded later doesn't take over
    d = _load("mod_d", version=1)
    d.add("d_tick", lambda: seen.append("d"))
    check("older copy doesn't take over", sys.modules[hub_key].owner is c)

    # Hot reload of the owner's own copy (same module object re-executed) takes over from itself
    owner_hook = next(iter(hooks_on.values()))
    code = re.sub(r"\nVERSION = \d+\n", "\nVERSION = 99\n", SRC.read_text(encoding="utf-8"), count=1)
    exec(compile(code, str(SRC), "exec"), c.__dict__)  # noqa: S102 - what importlib.reload does
    tc2 = c.tick("c_tick")(lambda: seen.append("c2"))
    tc2.enable()
    check("reloaded owner re-hooks its new dispatcher", next(iter(hooks_on.values())) is not owner_hook)
    seen.clear()
    calls_per_frame()
    check("reloaded callback replaced the old one", "c2" in seen and "c" not in seen)

    # Disable everything: the hook goes away
    for t in (ta, tb, tc2):
        t.disable()
    d.remove("d_tick")
    check("no hook once no callbacks are left", not hooks_on)
    seen.clear()
    calls_per_frame()
    check("nothing runs", not seen)

    # Re-enable after everything was removed
    ta.enable()
    check("hook back on enable", len(hooks_on) == 1)
    calls_per_frame()
    check("runs again", seen == ["a"])
    ta.disable()

    # A callback that removes itself while ticking
    def once() -> None:
        seen.append("once")
        a.remove("once")

    seen.clear()
    a.add("once", once)
    calls_per_frame()
    calls_per_frame()
    check("self-removal mid-tick is safe", seen == ["once"] and not hooks_on)

    # v2: the memory read of the frame counter was tried once (no game here: stays on GetFrameCount)
    hub = sys.modules[hub_key]
    check("counter read tried once, not found", hub.frame_ptr_tried and hub.frame_ptr is None)

    # ... and when found, frames come from it, with no GetFrameCount call
    class _Ptr:
        @property
        def value(self) -> int:
            return Clock.frame

    hub.frame_ptr = _Ptr()
    hub.frame_count = types.SimpleNamespace(__call__=None)  # would fail if called
    seen.clear()
    ta.enable()
    start = hub.ticks
    for _ in range(4):
        calls_per_frame(n=4)
    calls_per_frame(n=2, gap=0.006)  # a slow frame's second camera update: same frame number
    check(f"counter read: once per frame (ticks {hub.ticks - start})", seen == ["a"] * 5 and hub.frame == Clock.frame)
    ta.disable()
    hub.frame_ptr = None

    # Frame counter failing: time gate alone
    import unrealsdk

    sys.modules[hub_key].frame_count = None
    unrealsdk.find_class = lambda name: (_ for _ in ()).throw(RuntimeError("no class"))
    seen.clear()
    ta.enable()
    for _ in range(3):
        calls_per_frame()
    check("works without the frame counter", seen == ["a", "a", "a"])
    ta.disable()

    print(f"{passed} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
