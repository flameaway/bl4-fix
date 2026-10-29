# Loads or hot-reloads the UVH Boss Teleport mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_uvh_teleport.py
# First time in a session (the game started before dev/bl4_uvh_teleport existed) it just imports
# and enables it. After that it unhooks the old copy, reloads every module, and enables the new one.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_uvh_teleport.travel", "bl4_uvh_teleport.page")

old = sys.modules.get("bl4_uvh_teleport")
if old is None:
    new = importlib.import_module("bl4_uvh_teleport")
else:
    # Take the old copy's page script back from whichever link holds it (the new one registers again)
    try:
        old.page.unregister()
    except Exception as ex:  # noqa: BLE001
        print("old unregister failed:", repr(ex))
    for old_hook in old.mod.hooks:
        old_hook.disable()
    for old_cmd in old.mod.commands:
        old_cmd.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
new.mod.enable()
print("uvh teleport loaded, enabled:", new.mod.is_enabled)
