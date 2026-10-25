"""
Encore Tweaks for Borderlands 4: using a Big Encore machine refills everyone's health, shields,
ammo and action skill cooldown, and can first kill the enemies around it.

A port of Yeti's Encore Tweaks (GPL-3.0, see ported/README.md).

We hook the machine's use event (Script_BossReplay). Ammo comes from the game's own resupply:
ServerOnRefillAmmo with a resupply vending machine we spawn for the call and destroy right after
(the original left one behind on every use). Killing uses the game's Kill dev perk, through the
controller of the player who used the machine (the one nearest it). Host only: the machine runs on
the host, so in co-op the host needs the mod, and then it works for every player whoever uses the
machine (clients don't need the mod).
"""

from __future__ import annotations

from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, build_mod, get_pc, hook
from unrealsdk import logging, make_struct
from unrealsdk.hooks import Type
from unrealsdk.unreal import IGNORE_STRUCT, BoundFunction, FGbxDefPtr, UObject, WrappedStruct

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

USED_FUNC = (
    "/Game/InteractiveObjects/GameSystemMachines/BossReplay/Script_BossReplay.Script_BossReplay_C:"
    "GbxActorScriptEvt__UsableActorState_K2_OnUsed"
)
RESUPPLY_SHOP = "InventoryShop_VendingMachine_Resupply"
DEV_PERK_KILL = 3  # EDevPerk.Kill
# HealthUIType values below this are the bars a refill should fill (health, shield, armor)
HEALTH_UI_MAX = 3
SPAWN_ALWAYS = 1  # ESpawnActorCollisionHandlingMethod.AlwaysSpawn

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_system = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
_damage = unrealsdk.find_class("DamageStatics").ClassDefaultObject
_action_skill = unrealsdk.find_class("GbxSkillComponentFunctions_ActionSkill").ClassDefaultObject
_vendor_class = unrealsdk.find_class("OakVendingMachine")


def log(msg: str) -> None:
    logging.info(f"[Encore Tweaks] {msg}")


health_option = BoolOption("Refill health and shields", True)
ammo_option = BoolOption("Refill ammo", True)
skill_option = BoolOption("Refill action skill", True, description="Ends the action skill cooldown.")
kill_option = BoolOption(
    "Kill enemies first",
    False,
    description="Using the machine first kills the enemies around it.",
)


def _players() -> list[UObject]:
    pawns = []
    for state in ENGINE.GameViewport.World.GameState.PlayerArray:
        pawn = state.PawnPrivate if state is not None else None
        if pawn is not None and pawn.Controller is not None:
            pawns.append(pawn)
    return pawns


def _refill_ammo(pawns: list[UObject]) -> None:
    world = ENGINE.GameViewport.World
    transform = make_struct("Transform", Scale3D=make_struct("Vector", X=1, Y=1, Z=1))
    vendor, _ = _statics.BeginDeferredActorSpawnFromClass(world, _vendor_class, transform, SPAWN_ALWAYS, None, 1)
    if vendor is None:
        log("couldn't spawn the resupply helper")
        return
    try:
        vendor.ShopDef = FGbxDefPtr(RESUPPLY_SHOP, "OakInventoryShopDef")
        vendor, _ = _statics.FinishSpawningActor(vendor, transform, 1)
        for pawn in pawns:
            pawn.Controller.ServerOnRefillAmmo(vendor, IGNORE_STRUCT, False)
    finally:
        if vendor is not None:
            vendor.K2_DestroyActor()


def _refill_health(pawn: UObject) -> None:
    for state in pawn.HealthState.HealthTypeStates:
        handle = state.HealthType
        if handle is not None and int(handle.DisplayColor) < HEALTH_UI_MAX:
            _damage.RefillHealthPercent(pawn, handle, 100, 100)


def _user(script: UObject, pawns: list[UObject]) -> UObject | None:
    """The controller of the player nearest the machine: whoever just used it (host or client)."""
    machine = script.GetOwner() if hasattr(script, "GetOwner") else None
    if machine is None or not pawns:
        return get_pc()
    at = machine.K2_GetActorLocation()

    def dist(pawn: UObject) -> float:
        p = pawn.K2_GetActorLocation()
        return (p.X - at.X) ** 2 + (p.Y - at.Y) ** 2 + (p.Z - at.Z) ** 2

    return min(pawns, key=dist).Controller


@hook(USED_FUNC, Type.PRE)
def machine_used(obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        world = ENGINE.GameViewport.World
        if world is None or not _system.IsServer(world):
            return
        pawns = _players()
        # The perk kills around the player whose controller asks, so ask through the user's
        if kill_option.value and (pc := _user(obj, pawns)) is not None:
            pc.ServerActivateDevPerk(DEV_PERK_KILL)
        if ammo_option.value and pawns:
            try:
                _refill_ammo(pawns)
            except Exception as ex:  # noqa: BLE001 - the other refills still happen
                logging.error(f"[Encore Tweaks] ammo: {ex!r}")
        for pawn in pawns:
            if health_option.value:
                _refill_health(pawn)
            if skill_option.value:
                _action_skill.RefillCooldown(pawn, 1.0, 0)
        log(f"refilled {len(pawns)} player(s)")
    except Exception as ex:  # noqa: BLE001 - never let our hook break the game's
        logging.error(f"[Encore Tweaks] {ex!r}")


mod = build_mod(
    options=[health_option, ammo_option, skill_option, kill_option],
    hooks=[machine_used],
)
