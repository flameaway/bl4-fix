# Hot-reloads the Detailed Item Cards mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_item_cards.py
# Skips mod.disable(): that would remove the pak and the loose files. Unhooks the old copy,
# reloads, enables the new one (option values come back from the settings file).
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_item_cards")
if old is not None:
    for old_hook in old.mod.hooks:
        old_hook.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in ("bl4_item_cards.pak", "bl4_item_cards.card_page", "bl4_item_cards.skill_stats", "bl4_item_cards.encore_waypoint"):
        importlib.reload(sys.modules[name])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_item_cards")
new.mod.enable()
print("reloaded, enabled:", new.mod.is_enabled, "options:", [o.identifier for o in new.mod.options])
