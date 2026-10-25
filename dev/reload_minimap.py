# Loads or hot-reloads the Minimap mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_minimap.py
# Removes the old copy's widget and map model and unhooks it, then imports the new one and enables
# it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_minimap")
if old is not None:
    old.mod.disable()  # removes the widget and the map model
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in ("bl4_minimap.scene", "bl4_minimap.tiles", "bl4_minimap.fog", "bl4_minimap.mapdata", "bl4_minimap.services", "bl4_minimap.loot", "bl4_minimap.objectives", "bl4_minimap.breadcrumbs", "bl4_minimap.markers", "bl4_minimap.widget"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_minimap")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
