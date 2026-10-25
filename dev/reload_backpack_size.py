# Loads or hot-reloads the Backpack Size mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_backpack_size.py
# Puts the game's backpack limit back and unhooks the old copy, then imports the new one and
# enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_backpack_size")
if old is not None:
    old.mod.disable()  # restores the limit
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    importlib.reload(sys.modules["bl4_backpack_size.limits"])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_backpack_size")
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
