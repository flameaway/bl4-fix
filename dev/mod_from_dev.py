# Replaces a loaded mod (the installed .sdkmod, e.g. in a ship-build test) with the repo's source
# folder, without a restart, the way the Mod Menu's hot reload swaps a staged update in:
#   python tools/bridge.py <id> -f dev/mod_from_dev.py          (edit MOD_ID first, or set it in
#   the bridge namespace: python tools/bridge.py <id> "MOD_FROM_DEV='bl4_x'" then this file)
# Turns its mods off (deregister), forgets its modules, imports mods/<id> and turns back on what
# was on. Refused for mods other loaded modules hold something of (BL4 Direct, the Mod Menu: use
# dev/mod_menu_from_dev.py). On a failed import the old copy is put back. The installed copy comes
# back at the next game start.
import importlib.util
import sys
from pathlib import Path

from mods_base.mod_list import deregister_mod, register_mod

MOD_ID = globals().get("MOD_FROM_DEV") or "bl4_item_cards"
# The repo: REPO set in the bridge namespace first (python tools/bridge.py <id> "REPO=r'<path>'"),
# else where this checkout usually lives
REPO = Path(globals().get("REPO") or Path.home() / "Documents" / "projects" / "bl4-direct")
DEV = REPO / "mods" / MOD_ID

hotload = sys.modules["bl4_mod_menu"].hotload
if not (DEV / "__init__.py").is_file():
    raise SystemExit(f"no {DEV}")
if MOD_ID in sys.modules and (why := hotload.can_reload(MOD_ID)):
    raise SystemExit(why)
old_file = sys.modules[MOD_ID].__file__ if MOD_ID in sys.modules else None
print("was:", old_file)

old_mods = hotload.mods_of(MOD_ID)
was_on = {m.name for m in old_mods if m.is_enabled}
for m in old_mods:
    deregister_mod(m)  # disables it, keeping "enabled" in its settings
old_modules = hotload._purge(MOD_ID)
before = list(hotload._registry())
spec = importlib.util.spec_from_file_location(MOD_ID, DEV / "__init__.py", submodule_search_locations=[str(DEV)])
module = importlib.util.module_from_spec(spec)
sys.modules[MOD_ID] = module
try:
    spec.loader.exec_module(module)
except Exception as ex:
    hotload._drop_new(before)
    hotload._purge(MOD_ID)
    sys.modules.update(old_modules)
    for m in old_mods:
        if m not in hotload._registry():
            register_mod(m)
        if m.name in was_on and not m.is_enabled:
            m.enable()
    raise SystemExit(f"the dev copy failed to import ({ex!r}); the old one is back")
for m in hotload.mods_of(MOD_ID):
    if m.name in was_on and not m.is_enabled and not getattr(m, "enabling_locked", False):
        m.enable()
print("now:", sys.modules[MOD_ID].__file__, [(m.name, m.version, m.is_enabled) for m in hotload.mods_of(MOD_ID)])
