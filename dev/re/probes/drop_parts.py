# Dev probe (bridge): drop items the game's own way (the old roll.py path: a hidden DefaultPawn 50 m
# up as the owner of SpawnInventoryFromCharacter), hide each drop at once, and read its parts
# (serial index + owning table from the part defs in memory) and serial. Then destroy everything.
#   exec this file, then: wdx_drop_start(["<inv>.<comp>", ...]); later wdx_drop_collect(); wdx_drop_cleanup()
import ctypes
import struct

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk.unreal import WeakPointer

from bl4_weapondex import stats as wdx_drop_stats

PICKUP_IDENTITY = 0x3F0
GET_SERIAL_RVA, GET_SERIAL_HEAD = 0xCD1BA8, "41565657534883ec2848833900747e4489c34889d64889cf"


class WdxDrop:
    owner = None
    before = set()
    keys = []
    found = {}
    keep = []


def _wdx_drop_pickups():
    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    found = gs.GetAllActorsOfClass(ENGINE.GameViewport.World, unrealsdk.find_class("InventoryPickup"), [])
    return list(found[1] if isinstance(found, tuple) else found)


def wdx_drop_start(keys):
    pc = get_pc()
    wdx_drop_stats.resolve_defs(pc)
    loc = pc.Pawn.K2_GetActorLocation()
    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    xf = unrealsdk.make_struct("Transform", Translation=unrealsdk.make_struct("Vector", X=loc.X, Y=loc.Y, Z=loc.Z + 5000.0),
                               Rotation=unrealsdk.make_struct("Quat", X=0, Y=0, Z=0, W=1), Scale3D=unrealsdk.make_struct("Vector", X=1, Y=1, Z=1))
    owner = gs.BeginDeferredActorSpawnFromClass(pc, unrealsdk.find_class("DefaultPawn"), xf, 1, None, 0)
    owner = owner[0] if isinstance(owner, tuple) else owner
    owner = gs.FinishSpawningActor(owner, xf, 0)
    owner = owner[0] if isinstance(owner, tuple) else owner
    owner.SetActorHiddenInGame(True)
    owner.SetActorEnableCollision(False)
    WdxDrop.owner = WeakPointer(owner)
    WdxDrop.before = {p.Name for p in _wdx_drop_pickups()}
    WdxDrop.keys = list(keys)
    for key in keys:
        unrealsdk.find_class("OakInventoryStatics").ClassDefaultObject.SpawnInventoryFromCharacter(owner, key, "", 0, False, None)
    return len(keys)


def _wdx_drop_serial(identity):
    base = ctypes.windll.kernel32.GetModuleHandleW(None)
    if wdx_drop_stats._read(base + GET_SERIAL_RVA, len(GET_SERIAL_HEAD) // 2) != bytes.fromhex(GET_SERIAL_HEAD):
        return None
    out = ctypes.create_string_buffer(struct.pack("<QQQQ", 0, 0, 0, 15))
    if not ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(base + GET_SERIAL_RVA)(identity, ctypes.addressof(out), False):
        return None
    WdxDrop.keep.append(out)
    ptr, _, size, cap = struct.unpack("<4Q", wdx_drop_stats._read(identity + 0xA0, 32))
    text = wdx_drop_stats._read(identity + 0xA0 if cap < 16 else ptr, size)
    return text.decode("ascii", "replace") if text else None


def wdx_drop_collect():
    """Hides and reads every new pickup: {name: (comp index, [(index, table)], serial)}."""
    for p in _wdx_drop_pickups():
        if p.Name in WdxDrop.before or p.Name in WdxDrop.found:
            continue
        p.SetActorHiddenInGame(True)
        p.SetActorEnableCollision(False)
        identity = p._get_address() + PICKUP_IDENTITY
        WdxDrop.found[p.Name] = (wdx_drop_stats.comp_index(identity), wdx_drop_stats.part_owners(identity), _wdx_drop_serial(identity), WeakPointer(p))
    return {n: v[:3] for n, v in WdxDrop.found.items()}


def wdx_drop_cleanup():
    n = 0
    for v in WdxDrop.found.values():
        p = v[3]()
        if p is not None:
            p.K2_DestroyActor()
            n += 1
    owner = WdxDrop.owner() if WdxDrop.owner else None
    if owner is not None:
        owner.K2_DestroyActor()
    WdxDrop.owner = None
    return n
