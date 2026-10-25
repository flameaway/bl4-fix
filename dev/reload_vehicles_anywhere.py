# Loads or hot-reloads the Vehicles Anywhere mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_vehicles_anywhere.py
# Puts the game's vehicle rules back and unhooks the old copy, then imports the new one and
# enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_vehicles_anywhere")
if old is not None:
    old.mod.disable()  # restores the rules
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    importlib.reload(sys.modules["bl4_vehicles_anywhere.rules"])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_vehicles_anywhere")
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
