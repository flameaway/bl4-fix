# Loads or hot-reloads the Instant Fast Travel mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_instant_travel.py
# First time in a session (the game started before dev/bl4_instant_travel existed) it just imports
# and enables it. After that it unhooks the old copy, reloads every module, and enables the new one.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_instant_travel.pak", "bl4_instant_travel.stations", "bl4_instant_travel.land")

old = sys.modules.get("bl4_instant_travel")
if old is None:
    new = importlib.import_module("bl4_instant_travel")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
else:
    for old_hook in old.mod.hooks:
        old_hook.disable()
    old.land.cancel()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
new.mod.enable()
print("instant travel loaded, enabled:", new.mod.is_enabled)
