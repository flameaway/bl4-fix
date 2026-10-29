# Dumps every live mission waypoint (GbxDiscoveryProvider_OakMissionWaypoints) with its data's
# vtable RVA, the ints around +0x28..+0x34, its target actor and position (dev only):
#   python tools/bridge.py <id> -f dev/minimap_waypoints.py
import collections
import math
import struct
import sys

import unrealsdk
from mods_base import get_pc

ob = sys.modules["bl4_minimap.objectives"]
O = ob.Objectives
base = ob._k32.GetModuleHandleW(None)
prov = O.provider()
print("provider", prov is not None, "broken", repr(O.broken), "checked", O.checked)
me = get_pc().Pawn.K2_GetActorLocation()
head = ob._read(prov._get_address() + 0x38, 16)
arr, count = struct.unpack_from("<Qi", head, 0)
entries = ob._read(arr, count * 24)
by_vt = collections.Counter()
rows = []
for i in range(count):
    wp, eid = struct.unpack_from("<QQ", entries, i * 24)
    h = ob._read(wp, 0xD0)
    shared = struct.unpack_from("<Q", h, 0xC8)[0]
    data = ob._read(shared, 0x100)
    if data is None:
        continue
    vt = struct.unpack_from("<Q", data, 0)[0] - base
    ints = struct.unpack_from("<6I", data, 0x20)
    idx, ser = struct.unpack_from("<ii", data, 0x38)
    t = O._object_at(idx) if idx or ser else None
    actor = None
    where = None
    if t and t[1] == ser and t[0]:
        where = O._location(t[0])
        for o in (unrealsdk.find_object("Object", "/Engine/Transient"),):
            pass
        actor = t[0]
    where = where or ob._data_location(data)
    dist = math.hypot(where[0] - me.X, where[1] - me.Y) / 100 if where else -1
    by_vt[(hex(vt), ints[4])] += 1
    rows.append((dist, hex(vt), ints, hex(actor) if actor else "-", hex(wp)))
print("by (vtable, +0x30):", dict(by_vt))
for r in sorted(rows)[:40]:
    if r[1] != hex(ob.GIVER_DATA_VTABLE):
        print("%7.0f m" % r[0], *r[1:])
print("non-giver total", sum(1 for r in rows if r[1] != hex(ob.GIVER_DATA_VTABLE)))
