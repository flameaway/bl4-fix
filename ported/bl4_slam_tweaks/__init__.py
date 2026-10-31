"""
Slam Tweaks for Borderlands 4: a fast, Pre-Sequel style ground slam. No hop before it, no long
dive and landing animations, your gun stays up and you can act right after it lands. Optional
slam damage change.

A port of Yeti's TPS Style Slams (GPL-3.0, see ported/README.md), rewritten so turning it off (or
an option) puts every value back. Modified by flameaway from 2026-10-24 (changes since in git
history).

The slam is data: the move (Move_GroundSlam), its tricks (looping dive, landing) and their trick
options. We change those objects in place, remembering each original value; they load with your
character, so we check for them when the character changes (about once a second). Without the
landing animation the landing script never ends the camera effect itself, so a hook ends it.

Co-op: the host simulates every player's slam and each player's own game predicts theirs, so the
move and trick changes are made only while everyone in the game runs this mod (BL4 Direct's co-op
channel tells who does); with a friend who doesn't, everyone slams the vanilla way rather than
being pulled back by the host. Slam damage is dealt by the host, so the host's damage option
applies to every player, with the mod or without.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, SliderOption, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

from . import frametick

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo behavior
    coop = None

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

FEATURE = "slam_tweaks"

LANDING_SCRIPT_FUNC = (
    "/Game/PlayerCharacters/_Shared/Tricks/ControlledMoves/Trick_GroundSlamExit."
    "Trick_GroundSlamExit_Script_Instanced_C:OnBegin_Mut"
)
CHECK_S = 1.0
# Objects we wanted that weren't loaded at the last apply, and objects we changed that may have been
# unloaded, are looked at a few per LATE_STEP_S (one find_object, ~0.09 ms, per step) instead of all
# at once each second (~1-2 ms in one frame, 2026-10-29). Each missing one about once a second for
# LATE_FAST_S after an apply (the slam assets can load after the character), then about every
# LATE_SLOW_S (a DLC that isn't installed never loads)
LATE_STEP_S = 0.1
LATE_FAST_S = 30.0
LATE_SLOW_S = 5.0
LATE_WEAK_BATCH = 8  # (unused since 2026-10-29: see LATE_WEAK_S)
# The changed objects' WeakPointers are all checked this often, in one step (13 reads, ~20-40 us).
# It was 8 per step: ~80 reads a second, most of the late tick's ~0.3 ms/s (2026-10-29)
LATE_WEAK_S = 1.0
SLAM_GRAVITY = 40.0  # the move's gravity scale (the game's is 12)

_character_class = unrealsdk.find_class("OakCharacter")


def log(msg: str) -> None:
    logging.info(f"[Slam Tweaks] {msg}")


BASE = "/Game/PlayerCharacters/_Shared/Tricks/ControlledMoves/"
DLC = "/Game/DLC/Cowbell/PlayerCharacters/_Shared/Tricks/"  # only with that DLC installed

Field = tuple[str | int, ...]
# (class, object path, field path, value)
Change = tuple[str, str, Field, Any]

_NO_ANIM = ("Anim", "Asset")
_DIVE: list[Change] = []
for _path in (
    BASE + "Trick_Looping_GroundSlam.Trick_Looping_GroundSlam",
    DLC + "TrickLoop_GroundSlam_Robodealer.TrickLoop_GroundSlam_Robodealer",
):
    _DIVE += [
        ("GbxTrick_Loop", _path, ("Start", "Meshes", 1, *_NO_ANIM), None),
        ("GbxTrick_Loop", _path, ("Loops", 0, "Anim", "Meshes", 1, *_NO_ANIM), None),
    ]

_MOVE = BASE + "Move_GroundSlam.Move_GroundSlam"
_DIVE += [
    ("OakControlledMove", _MOVE, ("GravityModifier", "constant"), SLAM_GRAVITY),
    ("OakControlledMove", _MOVE, ("bApplyLaunchVelocityOnStart",), False),
    ("OakControlledMove", _MOVE, ("LaunchDirection", "RelativeDirection"), 0),
]

_OPTIONS = [
    "/Script/OakGame.Default__OakControlledMove_GroundSlam:options",
    _MOVE + ":options",
    BASE + "Trick_Looping_GroundSlam.Trick_Looping_GroundSlam:options",
    BASE + "Trick_GroundSlamExit_Water.Trick_GroundSlamExit_Water:options",
    DLC + "TrickLoop_GroundSlam_Robodealer.TrickLoop_GroundSlam_Robodealer:options",
    DLC + "Trick_GroundSlamExit_Water_Robodealer.Trick_GroundSlamExit_Water_Robodealer:options",
]
_LANDING_OPTIONS = [
    BASE + "Trick_GroundSlamExit.Trick_GroundSlamExit:options",
    DLC + "Trick_GroundSlamExit_Robodealer.Trick_GroundSlamExit_Robodealer:options",
]
for _path in _OPTIONS + _LANDING_OPTIONS:
    _DIVE += [
        ("OakTrickOptions", _path, ("bBlockWeaponActions",), False),
        ("OakTrickOptions", _path, ("bLockActionSkills",), False),
        ("OakTrickOptions", _path, ("bHideWeapons",), False),
        ("OakTrickOptions", _path, ("bBringUpWeaponAtEnd",), False),
        ("OakTrickOptions", _path, ("bHideFirstPersonLegs",), True),
    ]
for _path in _LANDING_OPTIONS:
    _DIVE += [
        ("OakTrickOptions", _path, ("bBlockJumping",), False),
        ("OakTrickOptions", _path, ("MovementModesToBlock",), 0),
    ]

_LANDING: list[Change] = [
    ("GbxTrick_Anim", BASE + "Trick_GroundSlamExit.Trick_GroundSlamExit", ("AnimData", "Meshes", 0, *_NO_ANIM), None),
    ("GbxTrick_Anim", DLC + "Trick_GroundSlamExit_Robodealer.Trick_GroundSlamExit_Robodealer", ("AnimData", "Meshes", 0, *_NO_ANIM), None),
]  # fmt: skip


def _apply_soon(*_: Any) -> None:
    State.dirty = True
    frametick.soon("bl4_slam_tweaks")  # applied at the next frame


landing_option = BoolOption(
    "Landing animation",
    False,
    description="Keep the landing animation (it carries the slam's impact sound) but still skip the dive.",
    on_change=_apply_soon,
)
damage_option = SliderOption(
    "Slam damage (%)",
    100,
    0,
    500,
    5,
    description=(
        "Slam damage as a percentage of the game's own. You can slam about three times as often, so"
        " you may want it lower."
    ),
    on_change=_apply_soon,
)


class State:
    # (object path, field path) -> (object, the game's value; objects as WeakPointers)
    changed: dict[tuple[str, Field], tuple[WeakPointer, Any]] = {}
    pawn: WeakPointer | None = None
    # movement component address -> (it, the game's Value and BaseValue) for the damage option
    damage: dict[int, tuple[WeakPointer, float, float]] = {}
    sync: Any = None  # (pawn, data allowed, damaged pawns) at the last apply
    data_allowed: bool | None = None
    landing_hooked = False
    dirty = True  # an option changed since the last apply
    # (class, path) of the objects we want to change that didn't exist at the last apply: the
    # character can show up before its slam assets have loaded (DLC ones may never exist)
    missing: frozenset[tuple[str, str]] = frozenset()
    late = False  # late_check found something: the next tick applies again
    applied_at = 0.0  # when a new character / option / co-op change was last applied
    next_find = 0.0
    find_cursor = 0
    weak_cursor = 0
    next_weak = 0.0
    roots: tuple[frozenset[tuple[str, str]], list[tuple[str, str]]] | None = None  # see _late_roots


class _Gone:
    """Restore marker for an object value whose object no longer exists."""


# region Field access

_byte_bools: dict[tuple[str, str], int | None] = {}


def _byte_bool_offset(obj: UObject, prop: str) -> int | None:
    """Offset of obj.prop if it's a whole-byte bool (the SDK writes those as 0xFF, we write 1)."""
    key = (obj.Class.Name, prop)
    if key not in _byte_bools:
        field = next((p for p in obj.Class._properties() if p.Name == prop), None)
        whole = field is not None and field.Class.Name == "BoolProperty" and field.FieldMask == 0xFF  # noqa: PLR2004
        _byte_bools[key] = field.Offset_Internal if whole else None  # pyright: ignore[reportOptionalMemberAccess]
    return _byte_bools[key]


def _container(obj: Any, fields: Field) -> Any:
    for name in fields[:-1]:
        obj = obj[name] if isinstance(name, int) else getattr(obj, name)
    return obj


def _get(obj: UObject, fields: Field) -> Any:
    return getattr(_container(obj, fields), str(fields[-1]))


def _set(obj: UObject, fields: Field, value: Any) -> None:
    setattr(_container(obj, fields), str(fields[-1]), value)
    if len(fields) == 1 and isinstance(value, bool) and (offset := _byte_bool_offset(obj, str(fields[0]))) is not None:
        ctypes.c_uint8.from_address(obj._get_address() + offset).value = int(value)


# endregion
# region Applying


def _find(cls: str, path: str) -> UObject | None:
    try:
        return unrealsdk.find_object(cls, path)
    except ValueError:
        return None  # not loaded (no character yet) or not installed (DLC)


def _wanted() -> list[Change]:
    return _DIVE + ([] if landing_option.value else _LANDING)


def _restore(key: tuple[str, Field]) -> None:
    weak, original = State.changed.pop(key)
    if (obj := weak()) is None or isinstance(original, _Gone):
        return
    if isinstance(original, WeakPointer):
        if (original := original()) is None:
            return  # the animation was unloaded: it comes back with a restart
    _set(obj, key[1], original)


def _apply_data() -> None:
    missing: set[tuple[str, str]] = set()
    wanted = _wanted()
    keys = {(path, fields) for _, path, fields, _ in wanted}
    for key in [k for k in State.changed if k not in keys]:
        _restore(key)
    for cls, path, fields, value in wanted:
        key = (path, fields)
        obj = _find(cls, path)
        if obj is None:
            missing.add((cls, path))
            State.changed.pop(key, None)  # unloaded: nothing left to restore (_late_objects retries it)
            continue
        if (entry := State.changed.get(key)) is not None and (old := entry[0]()) is not None and (
            old._get_address() == obj._get_address()
        ):
            continue  # already ours
        original = _get(obj, fields)
        if isinstance(original, UObject):
            original = WeakPointer(original)
        elif original is None and value is None:
            original = _Gone()
        State.changed[key] = (WeakPointer(obj), original)
        _set(obj, fields, value)
    _set_landing_hook(not landing_option.value)
    State.missing = frozenset(missing)


def _late_objects() -> bool:
    """
    Whether the last apply needs redoing for objects alone: one we wanted loaded since (one
    find_object per missing path, ~0.05 ms), or one we changed was unloaded and may come back fresh.
    (All at once: late_check spreads the same checks out.)
    """
    if any(weak() is None for weak, _ in State.changed.values()):
        return True
    return any(_find(cls, path) is not None for cls, path in State.missing)


def _late_roots(missing: frozenset[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    The missing paths worth a find_object (~0.1-0.3 ms each in the frame tick, 2026-10-29): a
    subobject ("A.A:options") can't exist while its asset ("A.A") is missing too, so it's skipped
    until the asset loads (that find re-runs the whole apply). 10 missing paths -> 6 finds a round.
    """
    cached = State.roots
    if cached is not None and cached[0] is missing:
        return cached[1]
    paths = {path for _, path in missing}
    roots = sorted(m for m in missing if (outer := m[1].partition(":")[0]) == m[1] or outer not in paths)
    State.roots = (missing, roots)
    return roots


def _late_step(now: float) -> bool:
    """One step of _late_objects: every changed object once per LATE_WEAK_S, and at most one missing path."""
    if State.changed and now >= State.next_weak:
        State.next_weak = now + LATE_WEAK_S
        if any(weak() is None for weak, _ in State.changed.values()):
            return True
    missing = State.missing
    if not missing or now < State.next_find:
        return False
    roots = _late_roots(missing)
    every = 1.0 if now - State.applied_at < LATE_FAST_S else LATE_SLOW_S
    State.next_find = now + every / len(roots)
    cls, path = roots[State.find_cursor % len(roots)]
    State.find_cursor += 1
    return _find(cls, path) is not None


def _restore_damage(keep: set[int] | None = None) -> None:
    """Puts the game's slam damage back (on every character not in `keep`)."""
    for key in [k for k in State.damage if keep is None or k not in keep]:
        weak, value, base = State.damage.pop(key)
        if (movement := weak()) is not None:
            movement.GroundSlamDamage.Value = value
            movement.GroundSlamDamage.BaseValue = base


def _apply_damage(pawns: list[UObject]) -> None:
    if damage_option.value == 100:  # noqa: PLR2004
        _restore_damage()
        return
    scale = damage_option.value / 100.0
    keep: set[int] = set()
    for pawn in pawns:
        movement = pawn.OakCharacterMovement
        if movement is None:
            continue
        key = movement._get_address()
        keep.add(key)
        entry = State.damage.get(key)
        if entry is None or entry[0]() != movement:
            damage = movement.GroundSlamDamage
            entry = State.damage[key] = (WeakPointer(movement), damage.Value, damage.BaseValue)
        _, value, base = entry
        movement.GroundSlamDamage.Value = value * scale
        movement.GroundSlamDamage.BaseValue = base * scale
    _restore_damage(keep)  # characters gone, or no longer ours to change


def _restore_data() -> None:
    for key in list(State.changed):
        _restore(key)
    _set_landing_hook(False)


def _restore_all() -> None:
    _restore_data()
    _restore_damage()


def _damage_pawns(pc: UObject) -> list[UObject]:
    """The characters whose slam damage we set: everyone's on the host (it deals the damage)."""
    world = ENGINE.GameViewport.World
    if coop is not None and not coop.is_client() and world is not None and world.GameState is not None:
        pawns = []
        for ps in world.GameState.PlayerArray:
            pawn = ps.PawnPrivate if ps is not None else None
            if pawn is not None and pawn.Class._inherits(_character_class):
                pawns.append(pawn)
        return pawns
    pawn = pc.Pawn
    return [pawn] if pawn is not None and pawn.Class._inherits(_character_class) else []


def _data_allowed() -> bool:
    """
    The slam's data (move, tricks) is simulated by the host and by the slamming player's own game,
    so both must agree: change it only while everyone in the game runs this mod (solo: always).
    """
    return coop is None or coop.everyone_has(FEATURE)


# endregion
# region Hooks


@hook(LANDING_SCRIPT_FUNC, Type.POST)
def landing_started(obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        obj.OnEnd_Mut(args.Actor, 0)  # ends the landing's camera effect at once
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Slam Tweaks] {ex!r}")


def _set_landing_hook(on: bool) -> None:
    if on != State.landing_hooked:
        State.landing_hooked = on
        if on:
            landing_started.enable()
        else:
            landing_started.disable()


# Once a second through the shared frame tick (frametick/; frames in between cost nothing); the
# first one right after enabling
@frametick.tick("bl4_slam_tweaks", every=CHECK_S)
def tick() -> None:
    try:
        pc = get_pc(possibly_loading=True)
        pawn = pc.Pawn if pc is not None else None
        if pawn is None or pawn.Class.Name == "OakVehicle":
            return
        pawns = _damage_pawns(pc)
        allowed = _data_allowed()
        sync = (pawn._get_address(), allowed, frozenset(p._get_address() for p in pawns))
        unchanged = not State.dirty and State.sync == sync and State.pawn is not None and State.pawn() == pawn
        if unchanged and not (allowed and State.late):
            return
        State.late = False
        if not unchanged:
            State.applied_at = time.monotonic()
        missing_before = len(State.missing)
        State.pawn = WeakPointer(pawn)
        State.sync = sync
        State.dirty = False
        if allowed:
            _apply_data()
            if unchanged and len(State.missing) < missing_before:
                log(f"changed the slam objects that loaded after your character ({missing_before - len(State.missing)})")
        elif State.changed:
            _restore_data()
        if allowed != State.data_allowed:
            State.data_allowed = allowed
            if not allowed:
                log("a player in this game doesn't run Slam Tweaks: vanilla slam moves until they do"
                    " (slam damage still applies on the host)")
        _apply_damage(pawns)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Slam Tweaks] {ex!r}")


@frametick.tick("bl4_slam_tweaks.late", every=LATE_STEP_S)
def late_check() -> None:
    """Looks for slam objects that loaded (or unloaded) since the last apply, a few at a time."""
    try:
        if State.late or not State.data_allowed or State.pawn is None:
            return
        if _late_step(time.monotonic()):
            State.late = True
            frametick.soon("bl4_slam_tweaks")  # applied at the next frame
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Slam Tweaks] {ex!r}")


# endregion


def _enable() -> None:
    State.pawn = None
    State.dirty = True
    State.late = False


def _disable() -> None:
    _restore_all()
    State.missing = frozenset()
    State.pawn = None
    State.sync = None
    State.data_allowed = None


mod = build_mod(
    options=[landing_option, damage_option],
    hooks=[tick, late_check],
    on_enable=_enable,
    on_disable=_disable,
)

if coop is not None:
    coop.feature(FEATURE, lambda: mod.is_enabled)
