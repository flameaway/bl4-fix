# Loads or hot-reloads the Build Presets mod through the bridge (dev only), no game restart:
#   python tools/bridge.py <id> -f dev/reload_builds.py
# First time in a session (the game started before dev/bl4_builds existed) it just imports and
# enables it. After that it unhooks the old copy, reloads every module, and enables the new one.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_builds.native", "bl4_builds.game", "bl4_builds.presets", "bl4_builds.builds", "bl4_builds.keyboard",
              "bl4_builds.page", "bl4_builds.notice", "bl4_builds.hotkeys")

old = sys.modules.get("bl4_builds")
if old is None:
    new = importlib.import_module("bl4_builds")
else:
    # A name being typed has a keyboard hook running; never leave it behind
    if (old_page := sys.modules.get("bl4_builds.page")) is not None and hasattr(old_page, "stop_naming"):
        old_page.stop_naming(keep=False)
    if (old_hotkeys := sys.modules.get("bl4_builds.hotkeys")) is not None and hasattr(old_hotkeys, "stop"):
        old_hotkeys.stop()
    for old_hook in old.mod.hooks:
        old_hook.disable()
    for old_cmd in old.mod.commands:
        old_cmd.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
new.mod.enable()
print("builds loaded, enabled:", new.mod.is_enabled)
