# Loads or hot-reloads the BL4 Mod Menu through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_mod_menu.py
# The first run imports it into a game that started without it; later runs unhook the old copy,
# take it out of the mod list, and reload every module.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = (
    "bl4_mod_menu.pak",
    "bl4_mod_menu.pause_page",
    "bl4_mod_menu.main_page",
    "bl4_mod_menu.updates",
    "bl4_mod_menu.hotload",
    "bl4_mod_menu.repos",
    "bl4_mod_menu.keys",
    "bl4_mod_menu.widgets",
    "bl4_mod_menu.model",
    "bl4_mod_menu.extensions",
    "bl4_mod_menu.web",
    "bl4_mod_menu.menu",
)

old = sys.modules.get("bl4_mod_menu")
if old is None:
    new = importlib.import_module("bl4_mod_menu")
    print("loaded, enabled:", new.mod.is_enabled)
else:
    reopen = old.menu.S.is_open
    old.mod.enabling_locked = False  # a Library locks itself; unlock so disable() runs on_disable
    old.mod.disable(dont_update_setting=True)
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)  # new since the game loaded the mod
    new = importlib.reload(old)
    if reopen:
        new.menu.S.pending = new.menu.open_preferred
    print("reloaded, enabled:", new.mod.is_enabled)
