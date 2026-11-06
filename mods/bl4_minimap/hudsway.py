"""
"Still HUD": stops the whole HUD zooming, shaking and skewing when you sprint, boost a vehicle,
slide, glide, grapple or ground-slam.

The HUD page is drawn through a material, MI_UI_HUD, with Zoom / ZoomShake / SkewX / SkewY
parameters. The game plays curves on them from its NCS table `materialevents`
(GbxUIMaterialAnimationDataDef records: SprintIntro, SprintOutro, VehicleBoost, Slide, Glide,
Grapple, ButtSlam; each names the material it animates in MaterialPath). Native code plays them:
no UFunction is called, and the records aren't reachable through reflection.

The fix is to the data, once: every curve key of the records that name MI_UI_HUD gets value 0 and
flat tangents, so the animations still play on the right material but hold everything at rest.
Turning the option off writes the keys back. Nothing runs per frame once it's done.
(Emptying the records' MaterialPath instead also stopped the motion, but the HUD then flashed a
slightly different colour as each animation started and ended.)

Where the records are (read from memory, game build 26007962, 2026-11-05):
- NexusConfigStoreUIMaterialAnimationDataDef (one, in /Engine/Transient) +0x1C8: a TArray of
  0x20-byte entries, the record's pointer at +0x10 (22 entries).
- A record's MaterialPath (reflected offset, 0x28): 8 bytes, then the package and asset FNames
  (8 bytes each). Seven records matched MI_UI_HUD's, the seven events above.
- Its ParameterCurves (reflected offset, 0xA0) is a TMap<FName, FRichCurve>: elements data and num
  first, each element 0xC0 bytes (FName, then the curve: vtable at +0x8, its Keys TArray at +0x80).
  A key is 0x1C bytes: modes (3 bytes, pad), Time, Value, ArriveTangent, ArriveTangentWeight,
  LeaveTangent, LeaveTangentWeight. The keys read back match the NCS file's (ButtSlam Zoom:
  0/0, 0.063/0.16, 0.173/-0.08, 0.24/0).
Only records whose 16 path bytes equal MI_UI_HUD's own names are touched, and only elements whose
vtable matches the first's, so a patch that moves things finds no match and changes nothing
(`broken`, logged once).
"""

from __future__ import annotations

import ctypes
import struct
import time

import unrealsdk
from unrealsdk import logging
from unrealsdk.unreal import UObject

from .objectives import _read, _u64

STORE_CLASS = "NexusConfigStoreUIMaterialAnimationDataDef"
RECORD_STRUCT = "/Script/GbxGame.GbxUIMaterialAnimationDataDef"
HUD_MATERIAL = "/Game/UI/Materials/MaterialInstances/MI_UI_HUD.MI_UI_HUD"
RECORDS = 0x1C8  # the store's TArray of entries (data, num)
ENTRY, ENTRY_RECORD = 0x20, 0x10
PATH_NAMES = 8  # into MaterialPath: the package and asset FNames
OBJECT_NAME = 0x18  # UObject::NamePrivate
CURVE, CURVE_VTABLE, CURVE_KEYS = 0xC0, 0x8, 0x80  # a ParameterCurves element
KEY, VALUE, LEAVE = 0x1C, 0x8, 0x14  # a key, its Value (ArriveTangent follows) and LeaveTangent
MAX_RECORDS = 4096  # sanity bounds (22 records, at most 4 curves of 4 keys in this build)
MAX_CURVES, MAX_KEYS = 64, 256
PARAMS = ("Zoom", "ZoomShake", "SkewX", "SkewY")
RETRY_S = 2.0  # until the store and the HUD material have loaded (start-up)

def _write(address: int, data: bytes) -> None:
    # A plain copy: the keys are the game's heap (read-write pages, checked with VirtualQuery) and
    # were just read through _read, so they're there. WriteProcessMemory took 0.67 ms a call.
    ctypes.memmove(address, data, len(data))


def _store() -> UObject | None:
    # exact: half the time of a search with subclasses (5.7 vs 11.2 ms, one walk of every object)
    return next((o for o in unrealsdk.find_all(STORE_CLASS, exact=True) if not o.Name.startswith("Default__")), None)


def _flat(keys: bytes) -> bytes:
    """The same keys (times and modes) with value and tangents 0."""
    out = bytearray(keys)
    for at in range(0, len(keys), KEY):
        out[at + VALUE : at + VALUE + 8] = bytes(8)  # Value, ArriveTangent
        out[at + LEAVE : at + LEAVE + 4] = bytes(4)  # LeaveTangent
    return bytes(out)


class HudSway:
    wanted = False  # the option
    applied = False  # the HUD curves are flat
    broken = False
    next_try = 0.0
    patched: list[tuple[int, bytes, bytes]] = []  # noqa: RUF012  # (keys address, original, flat)

    @classmethod
    def due(cls) -> bool:
        """Cheap: whether sync() has anything to do (checked once a frame)."""
        return cls.wanted != cls.applied and not cls.broken

    @classmethod
    def sync(cls, now: float) -> None:
        if now < cls.next_try:
            return
        cls.next_try = now + RETRY_S
        if cls.wanted:
            cls._apply()
        else:
            cls.restore()

    @classmethod
    def _apply(cls) -> None:
        material = unrealsdk.find_object("MaterialInstanceConstant", HUD_MATERIAL)
        store = _store()
        if material is None or store is None:
            return  # not loaded yet: tried again in RETRY_S
        package = _read(material.Outer._get_address() + OBJECT_NAME, 8)
        asset = _read(material._get_address() + OBJECT_NAME, 8)
        record_struct = unrealsdk.find_object("ScriptStruct", RECORD_STRUCT)
        path_off = record_struct._find("MaterialPath").Offset_Internal
        curves_off = record_struct._find("ParameterCurves").Offset_Internal
        head = _read(store._get_address() + RECORDS, 12)
        if package is None or asset is None or head is None:
            cls._give_up("couldn't read the HUD material or the animation store")
            return
        data, count = struct.unpack("<Qi", head)
        if not 0 < count <= MAX_RECORDS:
            cls._give_up(f"the animation store lists {count} records")
            return
        names = package + asset
        found, vtable = [], None
        for i in range(count):
            record = _u64(data + i * ENTRY + ENTRY_RECORD)
            if not record or _read(record + path_off + PATH_NAMES, 16) != names:
                continue
            elements, curves = struct.unpack("<Qi", _read(record + curves_off, 12))
            if not 0 < curves <= MAX_CURVES:
                cls._give_up(f"a HUD animation lists {curves} curves")
                return
            for c in range(curves):
                element = elements + c * CURVE
                vtable = vtable or _read(element + CURVE_VTABLE, 8)
                if _read(element + CURVE_VTABLE, 8) != vtable:
                    cls._give_up("a HUD animation curve isn't where it was (a game patch?)")
                    return
                keys, n = struct.unpack("<Qi", _read(element + CURVE_KEYS, 12))
                if not 0 <= n <= MAX_KEYS:
                    cls._give_up(f"a HUD animation curve lists {n} keys")
                    return
                if n:
                    original = _read(keys, n * KEY)
                    found.append((keys, original, _flat(original)))
        if not found:
            cls._give_up("no HUD animations found (a game patch?)")
            return
        for keys, _, flat in found:
            _write(keys, flat)
        cls.patched, cls.applied = found, True
        # An animation playing right now would leave its last value: back to rest
        for mid in unrealsdk.find_all("MaterialInstanceDynamic", exact=True):
            if mid.Parent == material:
                for name in PARAMS:
                    mid.SetScalarParameterValue(name, 0.0)
        logging.info(f"[Minimap] still HUD: {len(cls.patched)} HUD animation curves flat")

    @classmethod
    def restore(cls) -> None:
        """Gives the curves their keys back (option off, mod disabled)."""
        for keys, original, flat in cls.patched:
            if _read(keys, len(flat)) == flat:
                _write(keys, original)
        cls.patched, cls.applied = [], False

    @classmethod
    def _give_up(cls, why: str) -> None:
        cls.broken = True
        logging.warning(f"[Minimap] still HUD is off: {why}")


def set_wanted(on: bool) -> None:
    HudSway.wanted = on
    HudSway.next_try = 0.0
    if not on:
        HudSway.restore()
    elif HudSway.due():
        HudSway.sync(time.monotonic())
