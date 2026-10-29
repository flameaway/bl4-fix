# Loads or hot-reloads the Sprint Toggle mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_sprint_toggle.py
# First time in a session (the game started before dev/bl4_sprint_toggle existed) it just imports
# and enables it. After that it unhooks the old copy, reloads every module, and enables the new one
# (the toggle starts off).
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_sprint_toggle.binds", "bl4_sprint_toggle.sprint")

old = sys.modules.get("bl4_sprint_toggle")
if old is None:
    new = importlib.import_module("bl4_sprint_toggle")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
else:
    old.mod.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
new.mod.enable()
print("sprint toggle loaded, enabled:", new.mod.is_enabled)
