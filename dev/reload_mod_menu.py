# Loads or hot-reloads the BL4 Mod Menu through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_mod_menu.py
# The first run imports it into a game that started without it; later runs unhook the old copy,
# take it out of the mod list, and reload every module.
import importlib
import importlib.util
import sys
from pathlib import Path

import mods_base.mod_list

SUBMODULES = (
    "bl4_mod_menu.pak",
    "bl4_mod_menu.pause_page",
    "bl4_mod_menu.main_page",
    "bl4_mod_menu.updates",
    "bl4_mod_menu.hotload",
    "bl4_mod_menu.repos",
    "bl4_mod_menu.keys",
    "bl4_mod_menu.keyboard",
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
    # Re-run the copy that's loaded: importlib.reload would look the package up again and could
    # pick another copy (the installed .sdkmod over the source folder dev/mod_menu_from_dev.py
    # switched to), whose files then replace the page's scripts
    if Path(old.__file__).is_file():
        spec = importlib.util.spec_from_file_location("bl4_mod_menu", old.__file__, submodule_search_locations=list(old.__path__))
        old.__spec__, old.__loader__ = spec, spec.loader
        spec.loader.exec_module(old)
        new = old
    else:
        new = importlib.reload(old)
    if reopen:
        new.menu.S.pending = new.menu.open_preferred
    print("reloaded, enabled:", new.mod.is_enabled)
