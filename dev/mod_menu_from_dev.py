# Switches a game that loaded the installed BL4 Mod Menu (.sdkmod, a ship-build test) to the repo's
# source folder, without a restart:
#   python tools/bridge.py <id> -f dev/mod_menu_from_dev.py
# The modules are reloaded in place (same module objects), so what other mods registered with it
# (extension pages, key asks) stays. After this, dev/reload_mod_menu.py works in that game too.
# The installed copy comes back at the next game start.
import importlib
import importlib.util
import sys
from pathlib import Path

import mods_base.mod_list

# The repo: REPO set in the bridge namespace first (python tools/bridge.py <id> "REPO=r'<path>'"),
# else where this checkout usually lives
REPO = Path(globals().get("REPO") or Path.home() / "Documents" / "projects" / "bl4-direct")
DEV = REPO / "mods" / "bl4_mod_menu"
# Parents before children, libraries before the modules that use them (dev/reload_mod_menu.py's order)
SUBMODULES = (
    "pak", "uilink", "uilink.pak", "health", "health.notice", "pause_page", "main_page", "updates", "hotload",
    "repos", "gamekeys", "keys", "widgets", "model", "extensions", "web", "menu",
)  # fmt: skip

old = sys.modules["bl4_mod_menu"]
missing = [n for n in SUBMODULES if not any((DEV / Path(*n.split("."))).with_suffix(s).exists() for s in (".py",))
           and not (DEV / Path(*n.split(".")) / "__init__.py").exists()]  # fmt: skip
if missing:
    raise SystemExit(f"not in {DEV}: {missing}")
print("was:", old.__file__)
reopen = old.menu.S.is_open
old.mod.enabling_locked = False  # a Library locks itself; unlock so disable() runs on_disable
old.mod.disable(dont_update_setting=True)
if old.mod in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.remove(old.mod)
# Submodules are looked up in the package's __path__: point it at the source folder
old.__path__ = [str(DEV)]
for short in SUBMODULES:
    name = "bl4_mod_menu." + short
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)
    print(f"  {short}: {Path(sys.modules[name].__file__).relative_to(DEV.parent)}")
spec = importlib.util.spec_from_file_location("bl4_mod_menu", DEV / "__init__.py", submodule_search_locations=[str(DEV)])
old.__spec__, old.__file__, old.__loader__ = spec, spec.origin, spec.loader
spec.loader.exec_module(old)
new = sys.modules["bl4_mod_menu"]
if reopen:
    new.menu.S.pending = new.menu.open_preferred
print("now:", new.__file__, "enabled:", new.mod.is_enabled, "listed:", new.mod in mods_base.mod_list.mod_list)
