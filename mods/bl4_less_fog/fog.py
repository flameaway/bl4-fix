"""
The map's height fog, and thinning it out.

Each map's `WorldTimeOfDayActor` drives an `ExponentialHeightFogComponent` from its time-of-day
cycle (the `TimeOfDayCycle` NCS: `WorldTimeOfDayExponentialHeightFog_FogDensity`, `_StartDistance`,
... keyed by time and weather). It writes new values whenever its clock moves on (every few
frames), not every frame, and our camera hook runs after it in the same frame. So `apply` keeps
the game's own number for each value: when a value isn't the one we last wrote, the game has
written a new one, and we write that times our scale. The fog never draws at the game's density.

We change density (both fog layers; volumetric fog follows it) and start distance, the same two
the cycle data sets. The Set* functions are used rather than the properties so the renderer picks
the change up. `restore` puts the game's numbers back.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import unrealsdk
from mods_base import ENGINE
from unrealsdk import logging
from unrealsdk.unreal import UClass, UObject, WeakPointer

FIND_INTERVAL = 1.0  # s between looks for fog components (new map, streamed-in level)
# Actors that carry the fogs we change. Looking these up costs well under a millisecond; walking
# every object (find_all) costs about 20 ms, so that only runs once per map, for anything else
FOG_OWNERS = ("WorldTimeOfDayActor", "ExponentialHeightFog")

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_fog_class = unrealsdk.find_class("ExponentialHeightFogComponent")


def _class(name: str) -> UClass | None:
    try:
        return unrealsdk.find_class(name)
    except Exception:  # noqa: BLE001 - a game build may not have both
        return None


_owner_classes = [c for c in map(_class, FOG_OWNERS) if c is not None]
# actor class name -> its properties that hold a fog component
_fog_props: dict[str, list[str]] = {}


@dataclass(frozen=True)
class _Value:
    name: str
    get: Callable[[UObject], float]
    set: Callable[[UObject, float], None]
    scale: str  # "density" or "start"


VALUES = (
    _Value("FogDensity", lambda c: c.FogDensity, lambda c, v: c.SetFogDensity(v), "density"),
    _Value(
        "SecondFogDensity",
        lambda c: c.SecondFogData.FogDensity,
        lambda c, v: c.SetSecondFogDensity(v),
        "density",
    ),
    _Value("StartDistance", lambda c: c.StartDistance, lambda c, v: c.SetStartDistance(v), "start"),
)


@dataclass
class _Changed:
    game: float  # the game's own number
    ours: float  # what we wrote, as stored (float32)
    wrote: float | None = None  # the number we asked for (game x scale); None until we write


@dataclass
class _Fog:
    component: WeakPointer
    changed: dict[str, _Changed] = field(default_factory=dict)


_fogs: list[_Fog] = []
_next_find = 0.0
_world = WeakPointer()  # the world we last walked every object of


def log(msg: str) -> None:
    logging.info(f"[Less Fog] {msg}")


def _owned_fogs(world: UObject) -> list[UObject]:
    """The fog components of the known fog actors in the world (streamed-in levels too)."""
    comps = []
    for cls in _owner_classes:
        _, actors = _statics.GetAllActorsOfClass(world, cls, [])
        for actor in actors:
            if actor is None:
                continue
            name = actor.Class.Name
            if name not in _fog_props:
                _fog_props[name] = [
                    p.Name
                    for p in actor.Class._properties()
                    if p.Class.Name == "ObjectProperty" and p.PropertyClass._inherits(_fog_class)
                ]
            comps.extend(c for p in _fog_props[name] if (c := getattr(actor, p)) is not None)
    return comps


def _find() -> None:
    """Pick up fog components we don't know yet, drop ones that are gone."""
    global _world
    _fogs[:] = [f for f in _fogs if f.component() is not None]
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return
    found = _owned_fogs(world)
    if _world() != world:
        _world = WeakPointer(world)
        found += unrealsdk.find_all("ExponentialHeightFogComponent", exact=False)
    known = {f.component() for f in _fogs}
    for comp in found:
        if comp in known or "Default__" in comp._path_name():
            continue
        if comp.GetOwner() is None:  # a blueprint's template, not a placed fog
            continue
        known.add(comp)
        _fogs.append(_Fog(WeakPointer(comp)))
        log(f"fog: {comp.Outer.Name} (density {comp.FogDensity:g}, start {comp.StartDistance:g})")


def apply(density: float, start: float) -> None:
    """Scale every fog by `density` and its start distance by `start` (1.0 = the game's)."""
    global _next_find
    now = time.monotonic()
    if now >= _next_find:
        _next_find = now + FIND_INTERVAL
        _find()
    scales = {"density": density, "start": start}
    for fog in _fogs:
        comp = fog.component()
        if comp is None:
            continue
        for value in VALUES:
            current = value.get(comp)
            entry = fog.changed.get(value.name)
            if entry is None or current != entry.ours:
                # First look, or the game wrote a new number since our last write
                entry = fog.changed[value.name] = _Changed(current, current)
            want = entry.game * scales[value.scale]
            if want == entry.wrote or (want == entry.game and current == entry.game):
                continue  # already showing what we want
            value.set(comp, want)
            entry.ours = value.get(comp)
            entry.wrote = want


def restore() -> None:
    for fog in _fogs:
        comp = fog.component()
        if comp is None:
            continue
        for value in VALUES:
            entry = fog.changed.get(value.name)
            if entry is not None and value.get(comp) == entry.ours != entry.game:
                value.set(comp, entry.game)
    _fogs.clear()
    global _next_find, _world
    _next_find = 0.0
    _world = WeakPointer()
