# Loads or hot-reloads the Reroll mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_reroll.py
# First time in a session (the game started before dev/bl4_reroll existed) it just imports and
# enables it. After that it unhooks the old copy, reloads every module, and enables the new one.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_reroll.serial", "bl4_reroll.serialgen", "bl4_reroll.legendary", "bl4_reroll.pak", "bl4_reroll.reroll", "bl4_reroll.native", "bl4_reroll.game", "bl4_reroll.card")

old = sys.modules.get("bl4_reroll")
if old is None:
    new = importlib.import_module("bl4_reroll")
else:
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
print("reroll loaded, enabled:", new.mod.is_enabled)
