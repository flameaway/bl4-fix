"""
Shoot to Open for Borderlands 4: shoot a container or chest and it opens, as if you'd walked up to
it and used it.

How it fits together:

- Bullet hits on world objects happen in native code, with no call we could hook, so we watch the
  equipped gun instead. Every frame we read its loaded ammo; when it drops, a shot was fired.
- For each shot we trace from the camera along the crosshair. If the first thing it meets is a
  lootable (crates, lockers, ammo boxes, red chests, ...), we ask the server to use it for us
  (OakPlayerController.ServerUseObject, the same request a use-key press sends). The server applies
  its usual rules, so locked containers stay locked.
- A few lootables aren't plain containers (pick-one reward drops, the Lost Capsule decrypt station),
  so we leave those for the use key.

Shots that don't spend ammo (free-shot perks, ammo regen at full) don't count, and spread means a
bullet can land a little off the crosshair, so the trace is a thin sphere rather than a line.

Co-op: the host's copy also opens what the other players shoot, tracing along their aim (their
replicated control rotation), so friends without the mod get it too. A friend's ammo is counted on
their own machine (the host's copy of it doesn't drop per shot), so on the host their fire button
is the signal: the Weapon.ServerStartUsing / ServerStopUsing requests their game sends us. A friend running the mod leaves it to a host that has it (BL4 Direct's
co-op channel says so) and asks the server itself otherwise; one who turned the mod off is left
alone.
"""

from __future__ import annotations

import math
import time
from typing import Any

import unrealsdk
from mods_base import SliderOption, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: local player only
    coop = None

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# A player pressing / releasing fire: RPCs from their game, which reach the host's hooks
FIRE_START_FUNC = "/Script/GbxWeapon.Weapon:ServerStartUsing"
FIRE_STOP_FUNC = "/Script/GbxWeapon.Weapon:ServerStopUsing"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0
AMMO_BEHAVIOR = "WeaponBehavior_AmmoPool"
TRACE_VISIBILITY = 0  # ETraceTypeQuery 1, which containers block (verified for every kind in a level)
TRACE_RADIUS = 12.0  # cm
USE_PRIMARY = 0  # EUsabilityType.Primary
# Lootables that are choices or quest props rather than containers
SKIP_NAMES = ("PowercoreDrop", "LostCapsule")
# Don't re-send a use for the same container while automatic fire keeps hitting it
RESEND_DELAY = 1.0
FEATURE = "shoot_open"
REMOTE_EVERY_S = 0.008  # the camera hook fires several times a frame; other players once a frame
EYE_HEIGHT = 60.0  # cm above a remote pawn's origin, when its camera isn't known to us
CAMERA_NEAR = 400.0  # cm: a remote player's camera manager further than this from them is stale
HELD_TRACE_S = 0.05  # while another player holds fire, trace along their aim this often
HELD_MAX_S = 15.0  # a release we never heard about doesn't keep opening what they look at


def log(msg: str) -> None:
    logging.info(f"[Shoot to Open] {msg}")


range_option = SliderOption(
    "Range (m)",
    60,
    5,
    200,
    5,
    description="How far away a container can be and still open when you shoot it.",
)

_kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
_lootable_class = unrealsdk.find_class("LootableObject")
# Vehicles are pawns too, without ActiveWeapons
_character_class = unrealsdk.find_class("OakCharacter")


class Gun:
    """One player's equipped gun and its ammo the last time we looked."""

    def __init__(self) -> None:
        self.weapon = WeakPointer()
        self.ammo_behavior = WeakPointer()
        self.loaded = -1
        # Host, for another player: their fire button, from the RPCs their game sends
        self.held_since = 0.0  # 0 = not held
        self.pressed = False  # a press not traced yet (a tap can start and stop in one frame)
        self.traced_at = 0.0

    def press(self) -> None:
        self.held_since = time.monotonic()
        self.pressed = True

    def release(self) -> None:
        self.held_since = 0.0

    def trigger_due(self, now: float) -> bool:
        """Host, for another player: whether to trace along their aim now (fire just pressed, or held)."""
        if self.held_since and now - self.held_since > HELD_MAX_S:
            self.held_since = 0.0
        if self.pressed or (self.held_since and now - self.traced_at >= HELD_TRACE_S):
            self.pressed = False
            self.traced_at = now
            return True
        return False

    def shots_fired(self, pawn: UObject) -> bool:
        """True if the loaded ammo went down since last frame."""
        slots = pawn.ActiveWeapons.Slots
        weapon = slots[0].Weapon if len(slots) else None
        if weapon is None:
            self.weapon = WeakPointer()
            return False
        if weapon is not self.weapon():
            # Swapped guns (or picked one up): start counting from this one
            self.weapon = WeakPointer(weapon)
            behavior = next((b for b in weapon.behaviors if b and b.Class.Name == AMMO_BEHAVIOR), None)
            self.ammo_behavior = WeakPointer(behavior)
            self.loaded = behavior.LoadedAmmo if behavior else -1
            return False
        behavior = self.ammo_behavior()
        if behavior is None:
            return False
        previous, self.loaded = self.loaded, behavior.LoadedAmmo
        return self.loaded < previous


_local_gun = Gun()
# Host: the other players' guns, by controller address
_remote_guns: dict[int, tuple[WeakPointer, Gun]] = {}
_remote_checked_at = 0.0


def _view(pc: UObject, pawn: UObject, local: bool) -> tuple[Any, Any]:
    """Where a player looks from, and which way."""
    if local:
        camera = pc.PlayerCameraManager
        return camera.GetCameraLocation(), camera.GetCameraRotation()
    # Another player, on the host: their aim is the replicated control rotation; their camera
    # manager's location is used only if it's near them (it may not be kept up to date here)
    rotation = pc.GetControlRotation()
    at = pawn.K2_GetActorLocation()
    camera = pc.PlayerCameraManager
    if camera is not None:
        start = camera.GetCameraLocation()
        if math.dist((start.X, start.Y, start.Z), (at.X, at.Y, at.Z)) < CAMERA_NEAR:
            return start, rotation
    return unrealsdk.make_struct("Vector", X=at.X, Y=at.Y, Z=at.Z + EYE_HEIGHT), rotation


def _crosshair_target(pc: UObject, pawn: UObject, local: bool = True) -> UObject | None:
    """The first actor along the crosshair, within range."""
    start, rotation = _view(pc, pawn, local)
    pitch, yaw = math.radians(rotation.pitch), math.radians(rotation.yaw)
    reach = range_option.value * 100
    end = unrealsdk.make_struct(
        "Vector",
        X=start.X + math.cos(pitch) * math.cos(yaw) * reach,
        Y=start.Y + math.cos(pitch) * math.sin(yaw) * reach,
        Z=start.Z + math.sin(pitch) * reach,
    )
    color = unrealsdk.make_struct("LinearColor")
    hit, _ignored, result = _kismet.SphereTraceSingle(
        pc, start, end, TRACE_RADIUS, TRACE_VISIBILITY, False, [pawn], 0,
        unrealsdk.make_struct("HitResult"), True, color, color, 0,
    )  # fmt: skip
    component = result.Component if hit else None
    return component.GetOwner() if component else None


# Containers we've asked to open, by name, and when
_requested: dict[str, float] = {}


def _try_open(pc: UObject, target: UObject) -> None:
    if not target.Class._inherits(_lootable_class) or target.PawnThatOpenedMe is not None:
        return
    name = str(target.Name)
    if any(skip in name for skip in SKIP_NAMES):
        return
    now = time.monotonic()
    if now - _requested.get(name, -RESEND_DELAY) < RESEND_DELAY:
        return
    _requested[name] = now
    if len(_requested) > 64:
        _requested.clear()
    # On the host for another player this runs the server's use for them (their loot, their open)
    pc.ServerUseObject(target, USE_PRIMARY, False)
    logging.dev_warning(f"[Shoot to Open] opened {name}")


def _remote_gun(pc: UObject) -> Gun:
    key = pc._get_address()
    entry = _remote_guns.get(key)
    if entry is None or entry[0]() != pc:
        entry = _remote_guns[key] = (WeakPointer(pc), Gun())
    return entry[1]


def _watch_remote() -> None:
    """Host: the other players' shots (players who run our mods but turned this one off are skipped)."""
    global _remote_checked_at
    now = time.monotonic()
    if now - _remote_checked_at < REMOTE_EVERY_S:
        return
    _remote_checked_at = now
    seen: set[int] = set()
    for pc in coop.remote_controllers():
        features = coop.player_features(pc)
        if features is not None and FEATURE not in features:
            continue
        pawn = pc.Pawn
        if pawn is None or not pawn.Class._inherits(_character_class):
            continue
        seen.add(pc._get_address())
        gun = _remote_gun(pc)
        # Their fire button is what we can rely on; the ammo check stays in case the host's copy
        # of their ammo does drop (both evaluated: each keeps its own state current)
        fired = gun.shots_fired(pawn)
        if gun.trigger_due(now) or fired:
            target = _crosshair_target(pc, pawn, local=False)
            if target is not None:
                _try_open(pc, target)
    for key in [k for k in _remote_guns if k not in seen]:
        del _remote_guns[key]


def _remote_fire_gun(weapon: UObject) -> Gun | None:
    """Host: the Gun of the other player holding `weapon` (None for our own or on a client)."""
    if coop is None or coop.is_client():
        return None
    pawn = weapon.Owner
    if pawn is None or not pawn.Class._inherits(_character_class):
        return None
    pc = pawn.Controller
    if pc is None or pc == get_pc():
        return None
    return _remote_gun(pc)


@hook(FIRE_START_FUNC, Type.PRE)
def remote_fire_start(obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        gun = _remote_fire_gun(obj)
        if gun is not None:
            gun.press()  # traced from the next tick, not inside the RPC
    except Exception as ex:  # noqa: BLE001
        _log_error(ex)


@hook(FIRE_STOP_FUNC, Type.PRE)
def remote_fire_stop(obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        gun = _remote_fire_gun(obj)
        if gun is not None:
            gun.release()
    except Exception as ex:  # noqa: BLE001
        _log_error(ex)


@hook(TICK_FUNC, Type.POST)
def watch_shots(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return
    try:
        if coop is not None:
            if coop.is_client():
                if coop.host_has(FEATURE):
                    return  # the host's copy opens what we shoot
            elif _remote_guns or coop.role() == "host":
                _watch_remote()
        pawn = pc.Pawn
        if pawn is None or not pawn.Class._inherits(_character_class):
            return
        if _local_gun.shots_fired(pawn):
            target = _crosshair_target(pc, pawn)
            if target is not None:
                _try_open(pc, target)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        _log_error(ex)


_logged_errors: set[str] = set()


def _log_error(ex: Exception) -> None:
    """Each distinct error once: a game patch renaming something this reads would fail every frame."""
    text = repr(ex)[:300]
    if text not in _logged_errors and len(_logged_errors) < 20:  # noqa: PLR2004
        _logged_errors.add(text)
        logging.error(f"[Shoot to Open] {text} (logged once)")


def _enable() -> None:
    global _local_gun
    _local_gun = Gun()
    _remote_guns.clear()
    _requested.clear()


mod = build_mod(
    options=[range_option],
    hooks=[watch_shots, remote_fire_start, remote_fire_stop],
    on_enable=_enable,
)

if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
