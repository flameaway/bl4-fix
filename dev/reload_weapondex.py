# Loads or hot-reloads the Weapondex mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_weapondex.py
# The first run imports it into a game that started without it; later runs disable and unhook the
# old copy, take it out of the mod list, and reload every module. Progress is in a file, so it's kept.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = (
    "bl4_weapondex.pak",
    "bl4_weapondex.data",
    "bl4_weapondex.progress",
    "bl4_weapondex.itemkey",
    "bl4_weapondex.owned",
    "bl4_weapondex.savegame",
    "bl4_weapondex.tracker",
    "bl4_weapondex.stats",
    "bl4_weapondex.card",
    "bl4_weapondex.serialgen",
    "bl4_weapondex.identity",
    "bl4_weapondex.roll",
    "bl4_weapondex.picture",
    "bl4_weapondex.thumbs",
    "bl4_weapondex.travel",
    "bl4_weapondex.screen",
    "bl4_weapondex.bake",
)

old = sys.modules.get("bl4_weapondex")
if old is None:
    new = importlib.import_module("bl4_weapondex")
else:
    old.mod.disable(dont_update_setting=True)
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    new = importlib.reload(old)
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
