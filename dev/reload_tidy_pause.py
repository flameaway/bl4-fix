# Loads or hot-reloads the Tidy Pause Menu mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_tidy_pause.py
# First time in a session (the game started before dev/bl4_tidy_pause existed) it just imports
# and enables it. After that it unhooks the old copy, reloads every module, and enables the new one.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_tidy_pause.page",)

old = sys.modules.get("bl4_tidy_pause")
if old is None:
    new = importlib.import_module("bl4_tidy_pause")
else:
    for old_hook in old.mod.hooks:
        old_hook.disable()
    old.page.unregister()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
new.mod.enable()
print("tidy pause menu loaded, enabled:", new.mod.is_enabled)
