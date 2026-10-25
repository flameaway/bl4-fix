"""
Grapple Anywhere for Borderlands 4: grapple to whatever you aim at, not just the game's grapple
points. One key pulls you up onto the ledge you aim at, the other is a free grapple that doesn't
look for a landing spot. Optionally turns the game's own grapple points off.

A port of Yeti's Grapple Anywhere (GPL-3.0, see ported/README.md), without its pak: the grapple
keeps the game's range.

A key press traces from the camera, spawns one of the game's GrapplePoint actors where it hits
(shrunk out of sight), waits for its script to start, then asks the character to grapple
(ServerStartGrapple). The point is destroyed when you let go of it, after a timeout, or on the
next press. The point hooks run only while a point is in flight; the tick does nothing between
presses except a once-a-second pass while "turn off the game's points" is on.

Co-op: grapple points are replicated actors only the host can spawn and switch on, and the host
starts every grapple (the grapple state replicates to the player). So a client's press asks the
host's copy of this mod (BL4 Direct's co-op channel) to spawn the point; once it has arrived on
the client, the client's own targeting picks it (we face it at our camera) and tells the server,
and ServerStartGrapple runs as in solo. The host removes the point after the grapple or a timeout.
The host needs the mod for a friend to use it; the friend needs it for the keys.
"""

from __future__ import annotations

import ctypes
import math
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, SliderOption, build_mod, get_pc, hook, keybind
from unrealsdk import logging, make_struct
from unrealsdk.hooks import Type
from unrealsdk.unreal import IGNORE_STRUCT, BoundFunction, UObject, WeakPointer, WrappedStruct

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host only
    coop = None

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

FEATURE = "grapple_anywhere"
SPAWN_SERVICE = "grapple_anywhere.point"
DROP_SERVICE = "grapple_anywhere.drop"
ARRIVE_TIMEOUT = 3.0  # s a client waits for the host's point to replicate to it
ARRIVE_NEAR = 150.0  # cm between where we asked for the point and where it showed up
HOST_POINT_LIFE = 12.0  # s the host keeps a friend's point at most
FRIEND_SHRINK_S = 0.5  # s after spawning, the host hides a friend's point from its own view

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
SCRIPT_STARTED_FUNC = "/Script/GbxGame.GbxActorScript:OnBeginPlay"
DETACHED_FUNC = (
    "/Game/Grapple/Script_Sequence_Grapple_Point.Script_Sequence_Grapple_Point_C:"
    "GbxActorScriptEvt__OnGrapplerDetached"
)
TRACE_CHANNEL = 4
MIN_DISTANCE = 500.0  # cm: closer than this the grapple has nowhere to go
LIFT = 35.0  # cm above the hit, so the point isn't inside the surface
GRAPPLE_DELAY = 0.1  # s after the point's script starts, so the game sees it as a target
SCRIPT_TIMEOUT = 2.0  # s to wait for the point's script
POINT_TIMEOUT = 8.0  # s a point may live after the grapple started
HIDDEN_SCALE = 0.01
VANILLA_CHECK_S = 1.0
SPAWN_ALWAYS = 1  # ESpawnActorCollisionHandlingMethod.AlwaysSpawn

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_system = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
_math = unrealsdk.find_class("KismetMathLibrary").ClassDefaultObject
_point_class = unrealsdk.find_class("GrapplePoint")


def log(msg: str) -> None:
    logging.info(f"[Grapple Anywhere] {msg}")


def _vanilla_changed(_option: BoolOption, _value: bool) -> None:
    State.next_vanilla_check = 0.0


distance_option = SliderOption(
    "Max distance (m)",
    24,
    5,
    50,
    1,
    description=(
        "How far the grapple keys reach. The game's own grapple range is 24 m; farther targets may"
        " not grapple."
    ),
)
vanilla_option = BoolOption(
    "Turn off the game's grapple points",
    False,
    description="The game's own grapple points stop working while this is on.",
    on_change=_vanilla_changed,
)


class State:
    point: WeakPointer | None = None  # our helper point
    spawned_at = 0.0
    script_started_at = 0.0  # 0 until its script starts
    grappled_at = 0.0  # 0 until we asked the character to grapple
    camera: WeakPointer | None = None
    pawn: WeakPointer | None = None
    # The game's points we turned off, by address
    disabled: dict[int, WeakPointer] = {}
    next_vanilla_check = 0.0
    watching = False
    # Client: a point we asked the host for: (asked at, where, mantle), until it arrives
    asked: tuple[float, Any, bool] | None = None
    remote_point = False  # our point is the host's (never destroy it here)
    # Host: friends' points, by controller address: (point, spawned at)
    friend_points: dict[int, tuple[WeakPointer, float]] = {}
    friend_shrunk: set[int] = set()


# region Helpers

_byte_bools: dict[tuple[str, str], int | None] = {}


def _set_bool(obj: UObject, prop: str, value: bool) -> None:
    """obj.prop = value, writing whole-byte bools as 1 (the SDK writes 0xFF)."""
    setattr(obj, prop, value)
    key = (obj.Class.Name, prop)
    if key not in _byte_bools:
        field = next((p for p in obj.Class._properties() if p.Name == prop), None)
        whole = field is not None and field.Class.Name == "BoolProperty" and field.FieldMask == 0xFF  # noqa: PLR2004
        _byte_bools[key] = field.Offset_Internal if whole else None  # pyright: ignore[reportOptionalMemberAccess]
    if (offset := _byte_bools[key]) is not None:
        ctypes.c_uint8.from_address(obj._get_address() + offset).value = int(value)


def _our_point() -> UObject | None:
    return State.point() if State.point is not None else None


def _is_ours(actor: UObject | None) -> bool:
    point = _our_point()
    return actor is not None and point is not None and actor._get_address() == point._get_address()


def _drop_point() -> None:
    point = _our_point()
    remote, State.remote_point = State.remote_point, False
    State.point = None
    State.asked = None
    State.camera = State.pawn = None
    State.spawned_at = State.script_started_at = State.grappled_at = 0.0
    if remote:
        if coop is not None and coop.is_client() and coop.host_has(FEATURE):
            coop.request(DROP_SERVICE, {}, lambda _reply: None)
        point = None
    if point is not None:
        try:
            point.GbxDestroyActor(True)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Grapple Anywhere] {ex!r}")
    _update_watch()


def _update_watch() -> None:
    """The point hooks run only while a point is in flight."""
    in_flight = State.point is not None or bool(State.friend_points)
    if in_flight != State.watching:
        State.watching = in_flight
        for h in (script_started, grapple_detached):
            if in_flight:
                h.enable()
            else:
                h.disable()


# endregion
# region Grappling


def _grapple(mantle: bool) -> None:
    pc = get_pc()
    pawn = pc.Pawn if pc is not None else None
    if pawn is None or pawn.Class.Name == "OakVehicle":
        return
    _drop_point()

    camera = pc.PlayerCameraManager
    start = camera.GetCameraLocation()
    forward = _math.GetForwardVector(camera.GetCameraRotation())
    reach = distance_option.value * 100.0
    end = make_struct("Vector", X=start.X + forward.X * reach, Y=start.Y + forward.Y * reach, Z=start.Z + forward.Z * reach)
    hit_something, _, hit = _system.LineTraceSingle(
        pawn, start, end, TRACE_CHANNEL, False, [pawn], 0, IGNORE_STRUCT, True, IGNORE_STRUCT, IGNORE_STRUCT, 0.0,
    )  # fmt: skip
    if not hit_something:
        return
    target = make_struct("Vector", X=hit.Location.X, Y=hit.Location.Y, Z=hit.Location.Z + LIFT)
    here = pawn.K2_GetActorLocation()
    distance = math.dist((here.X, here.Y, here.Z), (target.X, target.Y, target.Z))
    if not MIN_DISTANCE <= distance <= reach + LIFT:
        return
    if coop is not None and coop.is_client():
        _ask_host(pawn, camera, target, mantle)
        return

    point = _spawn_point(pawn, target, mantle)
    if point is None:
        return
    State.point = WeakPointer(point)
    State.camera = WeakPointer(camera)
    State.pawn = WeakPointer(pawn)
    State.spawned_at = time.monotonic()
    _update_watch()


def _spawn_point(pawn: UObject, target: Any, mantle: bool) -> UObject | None:
    """Host: a grapple point at `target` for `pawn` (anyone's), facing them."""
    here = pawn.K2_GetActorLocation()
    world = ENGINE.GameViewport.World
    transform = make_struct("Transform", Translation=target, Scale3D=make_struct("Vector", X=1, Y=1, Z=1))
    point, _ = _statics.BeginDeferredActorSpawnFromClass(world, _point_class, transform, SPAWN_ALWAYS, pawn, 1)
    if point is None:
        return None
    point, _ = _statics.FinishSpawningActor(point, transform, 1)
    if point is None:
        return None
    point.K2_SetActorRotation(_math.FindLookAtRotation(target, here)[0], False)
    point.SetGrappleableEnabled(True)
    _set_bool(point, "bSearchForMantleUpDestination" if mantle else "bSearchForDropDownDestination", True)
    return point


def _ask_host(pawn: UObject, camera: UObject, target: Any, mantle: bool) -> None:
    """Client: the host spawns the point (only it can); we take over once it has arrived here."""
    if coop is None or not coop.host_has(FEATURE):
        log("in co-op the host needs Grapple Anywhere for you to use it")
        return
    State.camera = WeakPointer(camera)
    State.pawn = WeakPointer(pawn)
    where = (target.X, target.Y, target.Z)
    State.asked = (time.monotonic(), where, mantle)

    def done(reply: dict[str, Any]) -> None:
        if not reply.get("ok") and State.asked is not None and State.asked[1] == where:
            log(f"the host couldn't make a grapple point: {reply.get('error')}")
            State.asked = None

    coop.request(SPAWN_SERVICE, {"x": where[0], "y": where[1], "z": where[2], "mantle": mantle}, done)


def _look_for_host_point(now: float) -> None:
    """Client: has the host's point replicated to us yet?"""
    assert State.asked is not None
    asked_at, where, mantle = State.asked
    pawn = State.pawn() if State.pawn is not None else None
    if pawn is None or now - asked_at > ARRIVE_TIMEOUT:
        if pawn is not None:
            log("the host's grapple point didn't arrive")
        State.asked = None
        return
    _, points = _statics.GetAllActorsOfClass(ENGINE.GameViewport.World, _point_class, [])
    for point in points:
        if point is None or point.Owner != pawn:
            continue
        at = point.K2_GetActorLocation()
        if math.dist((at.X, at.Y, at.Z), where) > ARRIVE_NEAR:
            continue
        State.asked = None
        State.point = WeakPointer(point)
        State.remote_point = True
        State.spawned_at = now
        # It began play when it arrived, maybe before this check saw it
        State.script_started_at = now
        # Not replicated: our side's grapple reads its own copy
        _set_bool(point, "bSearchForMantleUpDestination" if mantle else "bSearchForDropDownDestination", True)
        _update_watch()
        return


def _serve_spawn(req: Any) -> dict[str, Any]:
    """Host: a friend's point (one per friend; the last one goes)."""
    if not mod.is_enabled:
        return {"ok": False, "error": "Grapple Anywhere is off on the host"}
    pawn = req.pc.Pawn
    if pawn is None:
        return {"ok": False, "error": "no character"}
    _drop_friend_point(req.pc._get_address())
    d = req.data
    target = make_struct("Vector", X=float(d["x"]), Y=float(d["y"]), Z=float(d["z"]))
    point = _spawn_point(pawn, target, bool(d.get("mantle")))
    if point is None:
        return {"ok": False, "error": "the point didn't spawn"}
    State.friend_points[req.pc._get_address()] = (WeakPointer(point), time.monotonic())
    _update_watch()
    return {"ok": True}


def _serve_drop(req: Any) -> dict[str, Any]:
    _drop_friend_point(req.pc._get_address())
    return {"ok": True}


def _drop_friend_point(key: int) -> None:
    State.friend_shrunk.discard(key)
    entry = State.friend_points.pop(key, None)
    point = entry[0]() if entry is not None else None
    if point is not None:
        try:
            point.GbxDestroyActor(True)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Grapple Anywhere] {ex!r}")
    _update_watch()


def _expire_friend_points(now: float) -> None:
    for key, (weak, at) in list(State.friend_points.items()):
        point = weak()
        if point is None:
            del State.friend_points[key]
        elif now - at > HOST_POINT_LIFE:
            _drop_friend_point(key)
        elif now - at > FRIEND_SHRINK_S and key not in State.friend_shrunk:
            # Out of the host's sight too, once its script is surely running (like ours, below)
            State.friend_shrunk.add(key)
            point.SetActorScale3D(make_struct("Vector", X=HIDDEN_SCALE, Y=HIDDEN_SCALE, Z=HIDDEN_SCALE))


def _start_grapple(point: UObject) -> None:
    camera = State.camera() if State.camera is not None else None
    pawn = State.pawn() if State.pawn is not None else None
    if camera is None or pawn is None:
        _drop_point()
        return
    point.SetActorScale3D(make_struct("Vector", X=HIDDEN_SCALE, Y=HIDDEN_SCALE, Z=HIDDEN_SCALE))
    # The point's grab component faces the camera, so the game scores it as the target
    facing = _math.FindLookAtRotation(camera.GetCameraLocation(), point.K2_GetActorLocation())[0]
    components = point.BlueprintCreatedComponents
    if len(components) > 1 and components[1] is not None:
        components[1].K2_SetWorldRotation(facing, True, IGNORE_STRUCT, True)
    pawn.ServerStartGrapple()
    State.grappled_at = time.monotonic()


@keybind("Grapple to ledge", description="Aim at the top edge of something you could climb: you land on top of it.")
def ledge_key() -> None:
    try:
        _grapple(mantle=True)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Grapple Anywhere] {ex!r}")
        _drop_point()


@keybind("Free grapple", description="Grapples toward what you aim at without looking for a landing spot.")
def free_key() -> None:
    try:
        _grapple(mantle=False)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Grapple Anywhere] {ex!r}")
        _drop_point()


# endregion
# region The game's points


def _check_vanilla() -> None:
    if vanilla_option.value:
        _, points = _statics.GetAllActorsOfClass(ENGINE.GameViewport.World, _point_class, [])
        for point in points:
            if point is None or _is_ours(point) or not point.bGrappleEnabled:
                continue
            State.disabled[point._get_address()] = WeakPointer(point)
            _set_bool(point, "bGrappleEnabled", False)
    else:
        _restore_vanilla()


def _restore_vanilla() -> None:
    for weak in State.disabled.values():
        if (point := weak()) is not None:
            _set_bool(point, "bGrappleEnabled", True)
    State.disabled.clear()


# endregion
# region Hooks


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    now = time.monotonic()
    if State.point is None and State.asked is None and not State.friend_points and now < State.next_vanilla_check:
        return
    try:
        if State.asked is not None:
            _look_for_host_point(now)
        if State.friend_points:
            _expire_friend_points(now)
        if State.point is not None:
            point = _our_point()
            if point is None:
                _drop_point()
            elif State.grappled_at:
                if now - State.grappled_at > POINT_TIMEOUT:
                    _drop_point()
            elif State.script_started_at:
                if now - State.script_started_at >= GRAPPLE_DELAY:
                    _start_grapple(point)
            elif now - State.spawned_at > SCRIPT_TIMEOUT:
                log("the grapple point never started; giving up")
                _drop_point()
        if now >= State.next_vanilla_check:
            State.next_vanilla_check = now + VANILLA_CHECK_S
            if vanilla_option.value or State.disabled:
                _check_vanilla()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Grapple Anywhere] {ex!r}")
        _drop_point()


@hook(SCRIPT_STARTED_FUNC, Type.POST)
def script_started(obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        if not State.script_started_at and _is_ours(obj.GetOwner()):
            State.script_started_at = time.monotonic()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Grapple Anywhere] {ex!r}")


@hook(DETACHED_FUNC, Type.POST)
def grapple_detached(obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        owner = obj.GetOwner()
        if _is_ours(owner):
            _drop_point()
            return
        if owner is not None and State.friend_points:
            address = owner._get_address()
            for key, (weak, _) in list(State.friend_points.items()):
                if (point := weak()) is not None and point._get_address() == address:
                    _drop_friend_point(key)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Grapple Anywhere] {ex!r}")


# endregion


# The point hooks are turned on per grapple (_update_watch), so they aren't in the hook list
script_started.disable()
grapple_detached.disable()


def _enable() -> None:
    State.next_vanilla_check = 0.0


def _disable() -> None:
    _drop_point()
    for key in list(State.friend_points):
        _drop_friend_point(key)
    _restore_vanilla()
    State.watching = False
    script_started.disable()
    grapple_detached.disable()


mod = build_mod(
    options=[distance_option, vanilla_option],
    keybinds=[ledge_key, free_key],
    hooks=[tick],
    on_enable=_enable,
    on_disable=_disable,
)

if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
    coop.handle(SPAWN_SERVICE, _serve_spawn)
    coop.handle(DROP_SERVICE, _serve_drop)
