# Loads or hot-reloads the Instant Item Pictures mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_item_pictures.py
# Unhooks the old copy, then imports the new one and enables it. The thumbnail cvars stay as they
# are across the reload (putting MaxThumbnails back to 100 made the game wipe every picture), and
# the new copy takes over the game's original values, so a later disable still restores them.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_item_pictures")
if old is not None:
    pictures = sys.modules["bl4_item_pictures.pictures"]
    kept = (dict(pictures.Cvars.original), dict(pictures.Cvars.ours))
    restore = pictures.restore_cvars
    pictures.restore_cvars = lambda: None
    try:
        old.mod.disable()
    finally:
        pictures.restore_cvars = restore
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in ("bl4_item_pictures.page",):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    pictures = importlib.reload(pictures)
    pictures.Cvars.original.update(kept[0])
    pictures.Cvars.ours.update(kept[1])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_item_pictures")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
