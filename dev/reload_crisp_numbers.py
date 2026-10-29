# Loads or hot-reloads the Crisp Damage Numbers mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_crisp_numbers.py
# Disables the old copy (it puts the game's cvars back), then imports the new one and enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_crisp_numbers")
if old is not None:
    keep = dict(old._S.original)  # the game's values, so the new copy restores those, not ours
    old.mod.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    new = importlib.reload(old)
    new._S.original.update(keep)
else:
    new = importlib.import_module("bl4_crisp_numbers")
if new.mod not in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.append(new.mod)
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
