"""
Legendary and pearlescent items lying on the ground.

Dropped items are `InventoryPickup` actors. Their rarity isn't reflected, but each one's loot beam
(a Niagara component, NS_FX_Item_Loot_Beams) gets user parameters from the game: `User.IsLegendary`
and `User.Color` (the beam's colour) among them. Those sit in the component's reflected
`OverrideParameters` store (`SortedParameterOffsets` names + byte offsets into `ParameterData`).
A pickup's rarity doesn't change, so each pickup is read once and remembered.
"""

from __future__ import annotations

import struct
import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject, WeakPointer

SCAN_INTERVAL = 1.0  # s between looks for new pickups
# Reading a pickup's beam costs ~0.13 ms: after a big fight, read the new ones a few at a time
MAX_NEW_PER_SCAN = 10
BACKLOG_INTERVAL = 0.1
LEGENDARY, PEARL = "legendary", "pearl"


class Loot:
    next_scan = 0.0
    seen: dict[tuple[int, str], str | None] = {}  # noqa: RUF012  # (address, name) -> kind or None
    found: list[tuple[WeakPointer, str]] = []  # noqa: RUF012  # the legendary/pearl pickups
    pickup_class: Any = None
    niagara_class: Any = None

    @classmethod
    def clear(cls) -> None:
        cls.next_scan = 0.0
        cls.seen = {}
        cls.found = []

    @classmethod
    def _kind(cls, pickup: UObject) -> str | None:
        """legendary / pearl / None (anything else, or no beam to tell by)."""
        for beam in pickup.K2_GetComponentsByClass(cls.niagara_class):
            store = beam.OverrideParameters
            data = bytes(store.ParameterData)
            values = {str(e.Name): e.Offset for e in store.SortedParameterOffsets}
            legendary = values.get("User.IsLegendary")
            color = values.get("User.Color")
            if legendary is None or legendary + 4 > len(data):
                continue
            if not any(data[legendary : legendary + 4]):
                return None
            # Pearls share the legendary beam; their colour is blue-green instead of orange
            if color is not None and color + 12 <= len(data):
                r, _g, b = struct.unpack_from("<3f", data, color)
                if b > r:
                    return PEARL
            return LEGENDARY
        return None

    @classmethod
    def scan(cls, pc: UObject) -> None:
        now = time.monotonic()
        if now < cls.next_scan:
            return
        cls.next_scan = now + SCAN_INTERVAL
        if cls.pickup_class is None:
            cls.pickup_class = unrealsdk.find_class("InventoryPickup")
            cls.niagara_class = unrealsdk.find_class("NiagaraComponent")
        gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
        found = gs.GetAllActorsOfClass(pc, cls.pickup_class, [])
        pickups = found[1] if isinstance(found, tuple) else found
        seen: dict[tuple[int, str], str | None] = {}
        keep = []
        budget = MAX_NEW_PER_SCAN
        for pickup in pickups:
            if pickup is None:
                continue
            key = (pickup._get_address(), str(pickup.Name))  # noqa: SLF001
            if key in cls.seen:
                kind = cls.seen[key]
            elif budget > 0:
                budget -= 1
                kind = cls._kind(pickup)
            else:
                cls.next_scan = now + BACKLOG_INTERVAL  # the rest soon
                continue
            seen[key] = kind
            if kind is not None and not pickup.bHidden:
                keep.append((WeakPointer(pickup), kind))
        cls.seen = seen  # forgets the ones picked up
        cls.found = keep

    @classmethod
    def items(cls) -> list[tuple[str, float, float, float]]:
        """(kind, x, y, z) of the legendary / pearl items on the ground."""
        out = []
        for ptr, kind in cls.found:
            pickup = ptr()
            if pickup is None:
                continue
            loc = pickup.K2_GetActorLocation()
            out.append((kind, loc.X, loc.Y, loc.Z))
        return out
