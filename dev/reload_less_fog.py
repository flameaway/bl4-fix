# Loads or hot-reloads the Less Fog mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_less_fog.py
# Puts the game's fog back and unhooks the old copy, then imports the new one and enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_less_fog")
if old is not None:
    old.mod.disable()  # restores the fog
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    importlib.reload(sys.modules["bl4_less_fog.fog"])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_less_fog")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
