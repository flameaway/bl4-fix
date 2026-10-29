# Loads or hot-reloads the Mod Profiler mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_profiler.py
# First time in a session (the game started before dev/bl4_profiler existed) it just imports and
# enables it. After that it ends a running benchmark (hooks put back), unhooks the old copy,
# reloads every module, and enables the new one.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_profiler.profiler", "bl4_profiler.bench", "bl4_profiler.page")

old = sys.modules.get("bl4_profiler")
if old is None:
    new = importlib.import_module("bl4_profiler")
else:
    old.bench.shutdown()
    for old_hook in old.mod.hooks:
        old_hook.disable()
    old.page.unregister()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
new.mod.enable()
print("mod profiler loaded, enabled:", new.mod.is_enabled)
