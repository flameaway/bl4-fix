# Loads or hot-reloads the Save Backups mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_save_backup.py
# First time in a session (the game started before dev/bl4_save_backup existed) it imports and
# enables it (which makes the launch backup). After that it unhooks the old copy, reloads every
# module and enables the new one; the launch backup isn't repeated (once per game process).
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_save_backup.savefile", "bl4_save_backup.store", "bl4_save_backup.restore",
              "bl4_save_backup.backups", "bl4_save_backup.page")

old = sys.modules.get("bl4_save_backup")
if old is None:
    new = importlib.import_module("bl4_save_backup")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
else:
    for old_hook in old.mod.hooks:
        old_hook.disable()
    old.page.unregister()  # not _disable: that cancels a staged restore
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
new.mod.enable()
print("save backups loaded, enabled:", new.mod.is_enabled)
