# Loads or hot-reloads the Shoot to Open mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_shoot_open.py
# Unhooks the old copy, then imports the new one and enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_shoot_open")
if old is not None:
    for old_hook in old.mod.hooks:
        old_hook.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_shoot_open")
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
