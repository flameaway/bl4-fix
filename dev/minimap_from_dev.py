# Switches a game that loaded the installed Minimap (.sdkmod, a ship-build test) to the repo's source
# folder, without a restart:
#   python tools/bridge.py <id> -f dev/minimap_from_dev.py
# dev/reload_minimap.py doesn't do this: reloading the package finds the installed copy again. Run
# this file again after each change instead. The installed copy comes back at the next game start.
import importlib
import importlib.util
import sys
from pathlib import Path

import mods_base.mod_list
DEV = Path(globals().get("REPO") or Path.home() / "Documents" / "projects" / "bl4-direct") / "mods" / "bl4_minimap"
SUB = ("health", "health.notice", "scene", "tiles", "fog", "mapdata", "services", "loot", "objectives", "breadcrumbs", "markers", "widget")
old = sys.modules["bl4_minimap"]
print("was:", old.__file__, "enabled:", old.mod.is_enabled)
was_on = old.mod.is_enabled
old.mod.disable(dont_update_setting=True)
if old.mod in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.remove(old.mod)
old.__path__ = [str(DEV)]
for short in SUB:
    name = "bl4_minimap." + short
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)
spec = importlib.util.spec_from_file_location("bl4_minimap", DEV / "__init__.py", submodule_search_locations=[str(DEV)])
old.__spec__, old.__file__, old.__loader__ = spec, spec.origin, spec.loader
spec.loader.exec_module(old)
new = sys.modules["bl4_minimap"]
if new.mod not in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.append(new.mod)
if was_on and not new.mod.is_enabled:
    new.mod.enable()
print("now:", new.__file__, "enabled:", new.mod.is_enabled, "mapdata:", sys.modules["bl4_minimap.mapdata"].__file__)
