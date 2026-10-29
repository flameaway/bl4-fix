# Run while the ECHO map is open: every map icon component, kept or skipped by the snapshot rules.
#   python tools/bridge.py <id> -f dev/minimap_map_icons.py
import collections
import sys

import unrealsdk

md = sys.modules["bl4_minimap.mapdata"]
sc = sys.modules["bl4_minimap.scene"]
viewer = sc.Viewer.get()
print("viewer", viewer is not None)
level = viewer.Outer if viewer else None
poi_comp = unrealsdk.find_class("OakMapViewerPOIIconComponent")
kept, skipped = collections.Counter(), collections.Counter()
for a in unrealsdk.find_all("OakMapViewerPOI", exact=False):
    if a.Outer is not level:
        continue
    for comp in a.K2_GetComponentsByClass(poi_comp):
        tex, sca = md._params(comp.MaterialInstance)  # noqa: SLF001
        t = tex.get(str(comp.MaterialTextureParamName))
        name = t.Name if t is not None else "<none>"
        why = []
        if a.bHidden:
            why.append("actorHidden")
        if not comp.bVisible:
            why.append("compInvisible")
        if comp.K2_GetComponentScale().X <= 0:
            why.append("scale0")
        if sca.get(str(comp.MaterialIsHiddenParamName), 0.0) > 0.5:
            why.append("IsHidden")
        if str(a.Class.Name) in md.SKIP_POI_CLASSES:
            why.append("player")
        key = (str(a.Class.Name), str(name))
        if why:
            skipped[(*key, ",".join(why))] += 1
        else:
            kept[key] += 1
print("KEPT")
for k, v in sorted(kept.items()):
    print(" ", v, k)
print("SKIPPED (live ones only: scale0/compInvisible are old copies)")
for k, v in sorted(skipped.items()):
    if "scale0" not in k[2] and "compInvisible" not in k[2]:
        print(" ", v, k)
