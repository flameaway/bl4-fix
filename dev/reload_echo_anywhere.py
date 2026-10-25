# Loads or hot-reloads the ECHO Anywhere mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_echo_anywhere.py
# Unhooks the old copy, then imports the new one and enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_echo_anywhere")
if old is not None:
    old.mod.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    importlib.reload(sys.modules["bl4_echo_anywhere.native"])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_echo_anywhere")
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
