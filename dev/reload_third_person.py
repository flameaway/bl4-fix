# Loads or hot-reloads the Third Person mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_third_person.py
# Puts the camera definitions back to vanilla and unhooks the old copy, then imports the new one
# and enables it. The found definitions carry over, so there's no rescan.
import importlib
import sys

import mods_base.mod_list

found = None
on = False
old = sys.modules.get("bl4_third_person")
if old is not None:
    found = old.camera_defs.Finder.defs
    on = old.State.on
    old.mod.disable()  # restores the definitions, leaves third person
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in ("bl4_third_person.camera_defs", "bl4_third_person.crosshair"):
        importlib.reload(sys.modules[name])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_third_person")
new.camera_defs.Finder.defs = found
new.mod.enable()
new.State.on = on or new.State.on
print("loaded, enabled:", new.mod.is_enabled, "on:", new.State.on, "defs:", found)
