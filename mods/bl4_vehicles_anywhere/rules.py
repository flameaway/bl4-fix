"""
The game's "no vehicles here" rules, and switching them off.

Before it lets you summon (and while you drive), the game checks:

- the map's world settings (OakWorldSettings.bAllowPersonalVehicle; interiors and some hubs turn
  it off),
- no-vehicle zones (OakVehicleBlockingVolume). A zone either refuses summons inside it
  (BlockVehicles) or throws the driver out on entry (EjectDriver). Missions switch zones on and
  off through facts as the story moves, so a zone can come back on at any time.
  The summon check finds zones by collision overlap and ignores the zone's bEnabled (verified in
  the city's zone: summons said Blocked with bEnabled off, and worked once its collision was off),
  so we switch off both.

Zones stream in and out with the world, and missions re-enable them, so `apply` runs every second
and only touches what changed. Everything we change is remembered, and `restore` puts it back.

Left alone on purpose: the "vehicles allowed" fact (`transient.worldchange_has_vehicle`) is the
story unlock from the mission that gives you the vehicle, not a per-place rule, and summon locks
from cutscenes and skills.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import ctypes

import unrealsdk
from mods_base import ENGINE
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_zone_class = unrealsdk.find_class("OakVehicleBlockingVolume")

# (object address, property) -> (object, the game's value) for everything we've changed
_changed: dict[tuple[int, str], tuple[WeakPointer, object]] = {}


def log(msg: str) -> None:
    logging.info(f"[Vehicles Anywhere] {msg}")


# (class name, property) -> byte offset of a whole-byte bool, or None for anything else
_byte_bools: dict[tuple[str, str], int | None] = {}


def _byte_bool(obj: UObject, prop: str) -> int | None:
    """
    The address of obj.prop if it's a whole-byte bool.

    The SDK stores those as 0xFF, but native checks such as the summon's world settings test
    compare the byte with 1 (verified in Vault_Grasslands_P: 0xFF still said WorldSettings), so we
    read and write their byte ourselves.
    """
    key = (obj.Class.Name, prop)
    if key not in _byte_bools:
        field = next(p for p in obj.Class._properties() if p.Name == prop)
        whole = field.Class.Name == "BoolProperty" and field.FieldMask == 0xFF  # noqa: PLR2004
        _byte_bools[key] = field.Offset_Internal if whole else None
    offset = _byte_bools[key]
    return None if offset is None else obj._get_address() + offset


def _write(obj: UObject, prop: str, value: object) -> None:
    if prop == "bActorEnableCollision":
        obj.SetActorEnableCollision(value)  # the flag alone doesn't update the physics state
        return
    setattr(obj, prop, value)
    if (addr := _byte_bool(obj, prop)) is not None:
        ctypes.c_uint8.from_address(addr).value = int(bool(value))


def _set(obj: UObject, prop: str, value: object) -> bool:
    """Sets obj.prop, remembering the game's value. True if it changed."""
    old = getattr(obj, prop)
    addr = _byte_bool(obj, prop) if prop != "bActorEnableCollision" else None
    exact = addr is None or ctypes.c_uint8.from_address(addr).value == int(bool(value))
    if old == value and exact:
        return False
    key = (obj._get_address(), prop)
    entry = _changed.get(key)
    if entry is None or entry[0]() != obj or old != value:
        # First change, or the game set it again since our last write (a mission switching a zone
        # back on): that's the game's value now, and what restore puts back. A change to the
        # value we already set can't be seen, so restore may undo a mission switching a zone off
        _changed[key] = (WeakPointer(obj), old)
    _write(obj, prop, value)
    return True


def _prune() -> None:
    """Forget objects that are gone (levels streamed out, old maps)."""
    for key in [key for key, (ref, _) in _changed.items() if ref() is None]:
        del _changed[key]


def apply(zones: bool = True) -> None:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    if world is None:
        return
    _prune()
    settings = world.PersistentLevel.WorldSettings if world.PersistentLevel else None
    if settings is not None and _set(settings, "bAllowPersonalVehicle", True):
        log(f"allowed vehicles on {world.Name}")
    if zones:
        _, found = _statics.GetAllActorsOfClass(world, _zone_class, [])
        for zone in found:
            if zone is None:
                continue
            # Both, always: `or` would skip the second while the first changed
            changed = _set(zone, "bEnabled", False)
            changed = _set(zone, "bActorEnableCollision", False) or changed
            if changed:
                log(f"opened no-vehicle zone {zone.Name}")


def restore() -> None:
    for (_, prop), (ref, old) in _changed.items():
        obj = ref()
        if obj is not None:
            _write(obj, prop, old)
    _changed.clear()


def restore_zones() -> None:
    """Puts the no-vehicle zones back (when the zones option is turned off)."""
    for key, (ref, old) in list(_changed.items()):
        obj = ref()
        if obj is None:
            del _changed[key]
        elif obj.Class._inherits(_zone_class):
            _write(obj, key[1], old)
            del _changed[key]
