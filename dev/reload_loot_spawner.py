# Hot-reloads the dev Loot Spawner through the bridge (no game restart):
#   python tools/bridge.py <id> -f dev/reload_loot_spawner.py
import importlib
import sys

import mods_base.mod_list

old = sys.modules["bl4_loot_spawner"]
was_enabled = old.mod.is_enabled
for old_cmd in old.mod.commands:
    old_cmd.disable()
if old.mod in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.remove(old.mod)
new = importlib.reload(old)
if was_enabled:
    new.mod.enable()
print("loot spawner reloaded, enabled:", new.mod.is_enabled)
