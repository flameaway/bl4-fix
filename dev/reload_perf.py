# Loads or hot-reloads the Performance Fixes mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_perf.py
# Disables the old copy (restores cvars, releases the pinned Python state on the next tick), drops
# it from the mod list, reloads the submodules and enables the new one.
import importlib
import sys

import mods_base.mod_list

old = sys.modules.get("bl4_perf")
if old is not None:
    # release the pin here (we're on the game thread) so the new python_state module can pin again
    old.render.restore()
    old.python_state.unpin_here()
    old.mod.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    if hasattr(old, "gc_tune"):
        old.gc_tune.restore()
    for sub in ("python_state", "render", "memory", "gc_tune"):
        if f"bl4_perf.{sub}" in sys.modules:
            importlib.reload(sys.modules[f"bl4_perf.{sub}"])
        else:
            importlib.import_module(f"bl4_perf.{sub}")
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_perf")
    if new.mod not in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.append(new.mod)
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
