"""
The map's fog and distance haze, and thinning them out.

Each map's `WorldTimeOfDayActor` drives an `ExponentialHeightFogComponent` and a
`SkyAtmosphereComponent` from its time-of-day cycle (the `TimeOfDayCycle` NCS:
`WorldTimeOfDayExponentialHeightFog_FogDensity`, `_StartDistance`, ... keyed by time and weather).
It writes its numbers in stretches: none for seconds, then every frame (World_P 2026-10-29: our fog
density stood 5 s, then was put back on 150 frames in a row; the sky's aerial perspective was put
back on every frame for 6 s), always before the frame's first camera update (never between the
two). So `apply` runs once a frame (the shared frame tick) and keeps the game's own number for each
value: when a value isn't the one we last wrote, the game has written a new one, and we write that
times our scale. The fog never draws at the game's density. Most frames it only reads one or two
numbers per component and finds them as we left them.

What we change:
- height fog: density (both layers; volumetric fog follows it) and start distance, the two the
  cycle data sets;
- the sky's distance haze: `AerialPespectiveViewDistanceScale` (Unreal's spelling), the blue haze
  over far ground that's still there with the fog at 0 (the user's "No fog" ask, 2026-10-29);
- local fog volumes (13 in World_P): radial and height extinction, scaled like the density. The
  time of day doesn't write these (they stayed at 0 for 6 s), so they're only checked when we look
  for components (once a second), not every frame.

The Set* functions are used rather than the properties so the renderer picks the change up.
`restore` puts the game's numbers back.

All the way off (density 0 / haze 0, 2026-10-29): the renderer's own switches instead of numbers,
`r.Fog 0` (height fog, volumetric fog with it) and the aerial perspective's depth range cut to
almost nothing (no distance haze). The game can't write those back, so nothing runs per frame:
fighting the time of day's per-frame writes cost ~0.1 ms a frame (perf_probe, World_P). They're
checked once a second (a scalability change could touch them) and put back on restore.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import UClass, UObject, WeakPointer

FIND_INTERVAL = 1.0  # s between looks for components (new map, streamed-in level)
# Actors that carry the components we change. Looking these up costs well under a millisecond;
# walking every object (find_all) costs about 20 ms, so that only runs once per map, for height fogs
OWNERS = ("WorldTimeOfDayActor", "ExponentialHeightFog", "SkyAtmosphere", "LocalFogVolume")

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject

FOG_CVAR = "r.Fog"  # 0: no height fog and no volumetric fog
HAZE_CVAR = "r.SkyAtmosphere.AerialPerspectiveLUT.Depth"  # km the distance haze is worked out for
HAZE_OFF = 0.1  # km: haze past it stays what it is at 100 m, i.e. none to see


def _class(name: str) -> UClass | None:
    try:
        return unrealsdk.find_class(name)
    except Exception:  # noqa: BLE001 - a game build may not have all of them
        return None


@dataclass(frozen=True)
class _Value:
    name: str
    get: Callable[[UObject], float]
    set: Callable[[UObject, float], None]
    scale: str  # "density", "start" or "haze"


@dataclass(frozen=True)
class _Kind:
    name: str
    cls: UClass | None
    values: tuple[_Value, ...]
    # How many of `values` (the first ones) are read every frame to see whether the game wrote
    # anything (it writes every value at once); 0: checked only when we look for components (the
    # game doesn't write these)
    watch: int


HEIGHT_FOG = _Kind(
    "fog",
    _class("ExponentialHeightFogComponent"),
    (
        _Value("FogDensity", lambda c: c.FogDensity, lambda c, v: c.SetFogDensity(v), "density"),
        _Value("StartDistance", lambda c: c.StartDistance, lambda c, v: c.SetStartDistance(v), "start"),
        _Value(
            "SecondFogDensity",
            lambda c: c.SecondFogData.FogDensity,  # a struct read: only when the game wrote
            lambda c, v: c.SetSecondFogDensity(v),
            "density",
        ),
    ),
    2,
)
SKY = _Kind(
    "sky",
    _class("SkyAtmosphereComponent"),
    (
        _Value(
            "AerialPespectiveViewDistanceScale",
            lambda c: c.AerialPespectiveViewDistanceScale,
            lambda c, v: c.SetAerialPespectiveViewDistanceScale(v),
            "haze",
        ),
    ),
    1,
)
LOCAL_FOG = _Kind(
    "local fog",
    _class("LocalFogVolumeComponent"),
    (
        _Value("RadialFogExtinction", lambda c: c.RadialFogExtinction, lambda c, v: c.SetRadialFogExtinction(v), "density"),
        _Value("HeightFogExtinction", lambda c: c.HeightFogExtinction, lambda c, v: c.SetHeightFogExtinction(v), "density"),
    ),
    0,
)
KINDS = tuple(k for k in (HEIGHT_FOG, SKY, LOCAL_FOG) if k.cls is not None)

_owner_classes = [c for c in map(_class, OWNERS) if c is not None]
# actor class name -> [(property name, kind)] for its properties that hold a component we change
_props: dict[str, list[tuple[str, _Kind]]] = {}


@dataclass
class _Changed:
    game: float  # the game's own number
    ours: float  # what we wrote, as stored (float32)
    wrote: float | None = None  # the number we asked for (game x scale); None until we write


@dataclass
class _Target:
    component: WeakPointer
    kind: _Kind
    changed: dict[str, _Changed] = field(default_factory=dict)
    # What a frame where the game wrote nothing reads back (the watched values), and the scales then
    settled: tuple[tuple[float, ...], tuple[float, ...]] | None = None


_targets: list[_Target] = []
_watched: list[_Target] = []  # the targets with watched values: looked at every frame
_last_key: tuple[float, float, float] | None = None
_next_find = 0.0
_world = WeakPointer()  # the world we last walked every object of


def log(msg: str) -> None:
    logging.info(f"[Less Fog] {msg}")


def _owned(world: UObject) -> list[tuple[UObject, _Kind]]:
    """The components we change on the known actors in the world (streamed-in levels too)."""
    out = []
    for cls in _owner_classes:
        _, actors = _statics.GetAllActorsOfClass(world, cls, [])
        for actor in actors:
            if actor is None:
                continue
            name = actor.Class.Name
            if name not in _props:
                _props[name] = [
                    (p.Name, kind)
                    for p in actor.Class._properties()
                    if p.Class.Name == "ObjectProperty"
                    for kind in KINDS
                    if p.PropertyClass._inherits(kind.cls)
                ]
            out.extend((c, kind) for p, kind in _props[name] if (c := getattr(actor, p)) is not None)
    return out


def _find() -> list[_Target]:
    """Picks up components we don't know yet, drops ones that are gone. Returns the new ones."""
    global _world
    _targets[:] = [t for t in _targets if t.component() is not None]
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return []
    found = _owned(world)
    if _world() != world:
        _world = WeakPointer(world)
        found += [(c, HEIGHT_FOG) for c in unrealsdk.find_all("ExponentialHeightFogComponent", exact=False)]
    known = {t.component() for t in _targets}
    new = []
    for comp, kind in found:
        if comp in known or "Default__" in comp._path_name():
            continue
        if comp.GetOwner() is None:  # a blueprint's template, not a placed component
            continue
        known.add(comp)
        target = _Target(WeakPointer(comp), kind)
        _targets.append(target)
        new.append(target)
        if kind is HEIGHT_FOG:
            log(f"fog: {comp.Outer.Name} (density {comp.FogDensity:g}, start {comp.StartDistance:g})")
    _watched[:] = [t for t in _targets if t.kind.watch]
    if new and any(t.kind is not HEIGHT_FOG for t in new):
        counts = {k.name: sum(1 for t in new if t.kind is k) for k in (SKY, LOCAL_FOG)}
        log(", ".join(f"{n} {k}" for k, n in counts.items() if n))
    return new


def _apply_one(target: _Target, comp: UObject, scales: dict[str, float], seen: tuple[float, ...] = ()) -> None:
    """Writes each value as the game's number x its scale. `seen`: the watched values, just read."""
    for i, value in enumerate(target.kind.values):
        current = seen[i] if i < len(seen) else value.get(comp)
        entry = target.changed.get(value.name)
        if entry is None or current != entry.ours:
            # First look, or the game wrote a new number since our last write
            entry = target.changed[value.name] = _Changed(current, current)
        want = entry.game * scales[value.scale]
        if want == entry.wrote or (want == entry.game and current == entry.game):
            continue  # already showing what we want
        value.set(comp, want)
        entry.ours = value.get(comp)  # as stored (float32, or clamped)
        entry.wrote = want


_saved_cvars: dict[str, float] = {}  # cvar -> its value before our first change


def _hold_cvars(fog_off: bool, haze_off: bool) -> None:
    """Sets the renderer's switches for what's all the way off, and puts back the others."""
    for name, off, value in ((FOG_CVAR, fog_off, 0.0), (HAZE_CVAR, haze_off, HAZE_OFF)):
        if not off and name not in _saved_cvars:
            continue
        current = float(_kismet.GetConsoleVariableFloatValue(name))
        if off:
            _saved_cvars.setdefault(name, current)
            want = value
        else:
            want = _saved_cvars.pop(name)
        if abs(current - want) > 1e-6:  # noqa: PLR2004
            pc = get_pc(possibly_loading=True)
            if pc is None:
                if not off:
                    _saved_cvars[name] = want  # try again next time
                continue
            _kismet.ExecuteConsoleCommand(pc, f"{name} {want:g}", pc)


def _skipped(target: _Target, fog_off: bool, haze_off: bool) -> bool:
    """Components the renderer's switches take care of: left at the game's numbers."""
    return (fog_off and target.kind is HEIGHT_FOG) or (haze_off and target.kind is SKY)


def _restore_one(target: _Target, comp: UObject) -> None:
    for value in target.kind.values:
        entry = target.changed.get(value.name)
        if entry is not None and value.get(comp) == entry.ours != entry.game:
            value.set(comp, entry.game)
    target.changed.clear()
    target.settled = None


def _settled(target: _Target) -> tuple[float, ...]:
    """What the watched values read while the game leaves them alone: what we left there."""
    return tuple(target.changed[v.name].ours for v in target.kind.values[: target.kind.watch])


def apply(density: float, start: float, haze: float = 1.0) -> None:
    """
    Scales every fog by `density` (height fog and local fog volumes), its start distance by
    `start`, and the sky's distance haze by `haze` (1.0 = the game's).
    """
    global _next_find, _last_key
    now = time.monotonic()
    key = (density, start, haze)
    # Every target when we look for components (once a second) or the settings changed; otherwise
    # only the ones the game writes (watched values), and most frames those are as we left them
    everything = now >= _next_find or key != _last_key
    if now >= _next_find:
        _next_find = now + FIND_INTERVAL
        _find()
    _last_key = key
    fog_off, haze_off = density == 0, haze == 0
    if everything:
        _hold_cvars(fog_off, haze_off)
        _watched[:] = [t for t in _targets if t.kind.watch and not _skipped(t, fog_off, haze_off)]
    scales = {"density": density, "start": start, "haze": haze}
    for target in _targets if everything else _watched:
        comp = target.component()
        if comp is None:
            continue
        if _skipped(target, fog_off, haze_off):
            if target.changed:
                _restore_one(target, comp)  # the switch does it: leave the game's numbers alone
            continue
        watch = target.kind.watch
        seen = tuple(v.get(comp) for v in target.kind.values[:watch]) if watch else ()
        settled = target.settled
        if settled is not None and settled[1] == key and (seen == settled[0] if watch else not everything):
            continue  # most frames: as we left it
        _apply_one(target, comp, scales, seen)
        target.settled = (_settled(target), key)


def restore() -> None:
    for target in _targets:
        comp = target.component()
        if comp is not None:
            _restore_one(target, comp)
    if _saved_cvars:
        _hold_cvars(fog_off=False, haze_off=False)
    _targets.clear()
    _watched.clear()
    global _next_find, _world, _last_key
    _next_find = 0.0
    _last_key = None
    _world = WeakPointer()
