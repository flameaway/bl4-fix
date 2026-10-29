# Loads or hot-reloads the Instant Menu Tabs mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_instant_tabs.py
# Puts the game's code back and drops the old copy, then imports the new one and enables it.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_instant_tabs")
if old is not None:
    old.mod.disable()  # restores the original bytes (and takes the fade skip off the link)
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in ("pak", "pages", "patch"):
        sub = sys.modules.get(f"bl4_instant_tabs.{name}")
        if sub is not None:
            importlib.reload(sub)
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_instant_tabs")
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
for site in new.patch.SITES:
    found = new.patch.get(site)
    print(f"  {site.name}:", "not found" if found is None else f"applied={found.applied} at {found.addr:#x}")
