"""
Measures what our mods cost in a running game, through the bridge (game thread). Exec it into the
bridge namespace, start a probe, play for a while, then read the report:

    python tools/bridge.py <id> "exec(open(r'<repo>/dev/perf_probe.py').read())"
    python tools/bridge.py <id> "probe_start(['bl4_item_cards.skill_stats:tick', 'bl4_mod_menu.web:_build_snapshot'])"
    ... 30 s or more of real play, with the feature active ...
    python tools/bridge.py <id> "print(probe_report()); probe_stop()"

Two kinds of numbers:
- Per function ("module:attr" or "module:Class.attr"): calls, average, worst, and calls over 1 ms
  and 5 ms. The function is swapped for a timing wrapper in its module or class, so it's only seen
  when callers look it up there at call time (`module.func()` or a global name inside that module).
  A function a hook registered directly can't be wrapped this way; time what it calls instead.
- Frame gaps: the time between frames, taken from the per-frame camera hook, with percentiles and
  how many frames took over twice the median (hitches). Run once with the old code and once with
  the new to compare; the game's own frame time is in there too.

Hot-reloading a module drops its wrappers: probe_stop() before a reload, probe_start() after.
"""

# ruff: noqa: F821  - `unrealsdk` comes from the bridge namespace

import importlib
import time

from unrealsdk import hooks

_PROBE_HOOK = "/Script/Engine.CameraModifier:BlueprintModifyPostProcess"
_PROBE_ID = "perf_probe"
# Hook calls closer together than this belong to the same frame (the hook fires once per camera
# modifier, so several times a frame)
_SAME_FRAME_S = 0.002


class _Probe:
    stats: dict[str, list[float]] = {}  # name -> [calls, total s, worst s, over 1 ms, over 5 ms]
    restore: list[tuple[object, str, object, object]] = []  # owner, attr, original, our wrapper
    frames: list[float] = []
    last_call = 0.0
    frame_start = 0.0
    started = 0.0


def _record(name: str, took: float) -> None:
    entry = _Probe.stats[name]
    entry[0] += 1
    entry[1] += took
    entry[2] = max(entry[2], took)
    entry[3] += took > 0.001
    entry[4] += took > 0.005


def _timed(name: str, func):
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            _record(name, time.perf_counter() - start)

    wrapper.__wrapped__ = func
    return wrapper


def _wrap(target: str) -> None:
    module_name, _, attr_path = target.partition(":")
    owner: object = importlib.import_module(module_name)
    *parents, attr = attr_path.split(".")
    for parent in parents:
        owner = getattr(owner, parent)
    raw = owner.__dict__[attr] if isinstance(owner, type) else getattr(owner, attr)
    if isinstance(raw, staticmethod):
        new = staticmethod(_timed(target, raw.__func__))
    elif isinstance(raw, classmethod):
        new = classmethod(_timed(target, raw.__func__))
    else:
        new = _timed(target, raw)
    _Probe.stats[target] = [0, 0.0, 0.0, 0, 0]
    _Probe.restore.append((owner, attr, raw, new))
    setattr(owner, attr, new)


def _on_camera(*_args) -> None:
    now = time.perf_counter()
    if now - _Probe.last_call > _SAME_FRAME_S:
        if _Probe.frame_start:
            _Probe.frames.append(now - _Probe.frame_start)
        _Probe.frame_start = now
    _Probe.last_call = now


def probe_stop() -> None:
    """Puts every wrapped function back and removes the frame hook.

    Only where our wrapper is still in place: if the module was reloaded meanwhile (another
    session's hot reload), putting the old function back would replace its new code with stale
    code (it broke the Minimap once).
    """
    while _Probe.restore:
        owner, attr, raw, ours = _Probe.restore.pop()
        current = owner.__dict__.get(attr) if isinstance(owner, type) else getattr(owner, attr, None)
        if current is ours:
            setattr(owner, attr, raw)
    hooks.remove_hook(_PROBE_HOOK, hooks.Type.POST, _PROBE_ID)


def probe_start(targets: list[str] = ()) -> str:
    """Starts timing `targets` ("module:attr" / "module:Class.attr") and frame gaps."""
    probe_stop()
    _Probe.stats.clear()
    _Probe.frames.clear()
    _Probe.last_call = _Probe.frame_start = 0.0
    _Probe.started = time.perf_counter()
    failed = []
    for target in targets:
        try:
            _wrap(target)
        except Exception as ex:  # noqa: BLE001 - report and carry on with the rest
            failed.append(f"{target}: {ex!r}")
    hooks.add_hook(_PROBE_HOOK, hooks.Type.POST, _PROBE_ID, _on_camera)
    return "probing" + ("".join(f"\n  could not wrap {f}" for f in failed))


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.2f}"


def probe_report() -> str:
    span = time.perf_counter() - _Probe.started
    lines = [f"over {span:.1f} s"]
    frames = sorted(_Probe.frames)
    if frames:
        median = frames[len(frames) // 2]

        def pct(p: float) -> float:
            return frames[min(len(frames) - 1, int(len(frames) * p))]

        hitches = sum(f > 2 * median for f in frames)
        lines.append(
            f"frames {len(frames)} ({len(frames) / span:.0f}/s): median {_ms(median)} ms, p99 {_ms(pct(0.99))},"
            f" worst {_ms(frames[-1])}, over 2x median {hitches}",
        )
    for name, (calls, total, worst, over1, over5) in _Probe.stats.items():
        if not calls:
            lines.append(f"{name}: not called")
            continue
        lines.append(
            f"{name}: {calls} calls ({calls / span:.1f}/s), avg {_ms(total / calls)} ms, worst {_ms(worst)},"
            f" >1 ms {int(over1)}, >5 ms {int(over5)}, {_ms(total / span)} ms per second",
        )
    return "\n".join(lines)
