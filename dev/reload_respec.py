# Hot-reloads the Easy Respec mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_respec.py
# Skips mod.disable(): that would turn the page files off. Unhooks the old copy, reloads, enables the new.
import importlib
import sys

import mods_base.mod_list

old = sys.modules["bl4_respec"]
# Take the old copy's page script off the link (the new one registers again)
if hasattr(old, "uilink"):
    old.uilink.unregister(getattr(old, "LINK_KEY", "respec"))
for old_hook in old.mod.hooks:
    old_hook.disable()
if old.mod in mods_base.mod_list.mod_list:
    mods_base.mod_list.mod_list.remove(old.mod)
# uilink / health are shared packages (shared/): a reload of a copy takes over from the owner, so
# leave them be once loaded; the reloaded __init__ imports them the first time
for name in ("bl4_respec.pak", "bl4_respec.skills_page"):
    importlib.reload(sys.modules[name])
new = importlib.reload(old)
new.mod.enable()
print("reloaded, enabled:", new.mod.is_enabled)
