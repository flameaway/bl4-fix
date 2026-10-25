"""
Compass-only icons: vendors, the respec station, the bank, Lost Loot and firmware transfer
machines, ...

The compass gets these from a native "live replicated actor" discovery provider that Python
can't read, and the ECHO map doesn't show them all. They're ordinary actors in the level, though,
so every few seconds we look them up (one native class query, then the class and object name say
what each is) and keep their positions. Vending machines say their kind in their shop def
(`InventoryShop_VendingMachine_Health`, ...).
"""

from __future__ import annotations

import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject

SCAN_INTERVAL = 5.0  # s between the cheap lookups (by class); these only stream in and out
NAME_SCAN_PAUSED_S = 10.0  # between the slow lookups (by name) while paused
_ICONS = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/"


def _icon(name: str) -> str:
    return f"{_ICONS}{name}.{name}"


# Their own classes: found by a native class lookup (cheap)
BY_CLASS: tuple[tuple[str, str], ...] = (
    ("FirmwareTransferMachine", _icon("ico_menu_firmware_transfer")),
    ("OakLostLootMachine", _icon("ico_menu_lost_loot")),
)
# Plain OakInteractiveObjects told apart by object name: checking every interactive object's name
# in Python takes a few ms, so it's done once per level (while it loads in) and while paused
BY_NAME: tuple[tuple[str, str], ...] = (
    ("IO_RespecMachine", _icon("ico_menu_respec_station")),
    ("IO_PlayerBank", _icon("ico_menu_bank")),
)
# Vending machines by their shop def's name
VENDORS: tuple[tuple[str, str], ...] = (
    ("BlackMarket", _icon("ico_menu_vending_black_market")),
    ("Health", _icon("ico_Misc_Vending_Machine_Health")),
    ("Munitions", _icon("ico_Misc_Vending_Machine_Munitions")),
    ("Resupply", _icon("ico_Misc_Vending_Machine_Resupply")),
    ("Ammo", _icon("ico_Misc_Vending_Machine_Resupply")),
)
DEFAULT_VENDOR = _icon("ico_Misc_Vending_Machine_Munitions")


class Services:
    next_scan = 0.0
    next_name_scan = 0.0
    world = ""  # the level the by-name items are for
    by_class: list[dict[str, Any]] = []  # noqa: RUF012
    by_name: list[dict[str, Any]] = []  # noqa: RUF012
    items: list[dict[str, Any]] = []  # noqa: RUF012  # {"x", "y", "z", "texture"} like map icons

    @classmethod
    def _vendor_icon(cls, machine: UObject) -> str:
        shop = str(getattr(machine.ShopDef, "Name", "") or "")
        if not shop:
            text = str(machine.ShopDef)
            shop = text.split("'", 2)[1] if "'" in text else text
        for key, icon in VENDORS:
            if key.lower() in shop.lower():
                return icon
        return DEFAULT_VENDOR

    @staticmethod
    def _actors(pc: UObject, class_name: str) -> list[UObject]:
        gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
        found = gs.GetAllActorsOfClass(pc, unrealsdk.find_class(class_name), [])
        return list(found[1] if isinstance(found, tuple) else found)

    @staticmethod
    def _item(actor: UObject, icon: str) -> dict[str, Any]:
        loc = actor.K2_GetActorLocation()
        return {"x": loc.X, "y": loc.Y, "z": loc.Z, "texture": icon}

    @classmethod
    def scan(cls, pc: UObject, world_name: str, paused: bool) -> None:
        now = time.monotonic()
        if world_name != cls.world:
            cls.world = world_name
            cls.by_name = []
            cls.next_name_scan = 0.0  # once now, for the new level
        elif not paused:
            cls.next_name_scan = max(cls.next_name_scan, now + NAME_SCAN_PAUSED_S)
        if now >= cls.next_name_scan:
            cls.next_name_scan = now + NAME_SCAN_PAUSED_S
            by_name = []
            for actor in cls._actors(pc, "OakInteractiveObject"):
                name = str(actor.Name)
                for prefix, icon in BY_NAME:
                    if name.startswith(prefix):
                        by_name.append(cls._item(actor, icon))
            cls.by_name = by_name
        if now >= cls.next_scan:
            cls.next_scan = now + SCAN_INTERVAL
            by_class = [cls._item(a, cls._vendor_icon(a)) for a in cls._actors(pc, "OakVendingMachine") if not a.bHidden]
            for class_name, icon in BY_CLASS:
                by_class += [cls._item(a, icon) for a in cls._actors(pc, class_name) if not a.bHidden]
            cls.by_class = by_class
        cls.items = cls.by_class + cls.by_name

    @classmethod
    def near(cls, x: float, y: float, reach: float, skip: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """In range, leaving out any the map's own icons already show (same icon within 10 m)."""
        r2 = reach * reach
        out = []
        for item in cls.items:
            if (item["x"] - x) ** 2 + (item["y"] - y) ** 2 > r2:
                continue
            if any(
                icon["texture"] == item["texture"] and (icon["x"] - item["x"]) ** 2 + (icon["y"] - item["y"]) ** 2 < 1e6
                for icon in skip
            ):  # fmt: skip
                continue
            out.append(item)
        return out
