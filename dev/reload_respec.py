# Hot-reloads the Easy Respec mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_respec.py
# Skips mod.disable(): that would sync the pak off. Unhooks the old copy, reloads, enables the new.
import importlib
import sys

import mods_base.mod_list

old = sys.modules["bl4_respec"]
for old_hook in old.mod.hooks:
    old_hook.disable()
if old.mod in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.remove(old.mod)
for name in ("bl4_respec.pak", "bl4_respec.skills_page"):
    importlib.reload(sys.modules[name])
new = importlib.reload(old)
new.mod.enable()
print("reloaded, enabled:", new.mod.is_enabled)
