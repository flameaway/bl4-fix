# Loads or hot-reloads the BL4 Encore Tweaks port through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_encore_tweaks.py
# Disables the old copy (its on_disable puts back what it changed), then imports the new one and
# enables it. Needs the junction dev/bl4_encore_tweaks -> ported/bl4_encore_tweaks.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_encore_tweaks")
if old is not None:
    old.mod.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_encore_tweaks")
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
