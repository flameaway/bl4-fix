# Hot-reloads the Detailed Item Cards mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_item_cards.py
# Skips mod.disable(): that would remove the loose files. Takes the old copy's page script off the
# BL4 UI Link first (the page tears it down; the new copy registers it again and the link runs it
# afresh), unhooks the old copy, reloads, enables the new one (option values come back from the
# settings file). The uilink/ and health/ copies aren't reloaded: they're shared with other mods.
import importlib
import sys

import mods_base.mod_list

SUBMODULES = ("bl4_item_cards.pak", "bl4_item_cards.card_page", "bl4_item_cards.skill_stats", "bl4_item_cards.encore_waypoint")

old = sys.modules.get("bl4_item_cards")
if old is not None:
    link = getattr(old, "uilink", None)
    if link is not None:
        try:
            link.unregister(getattr(old, "LINK_KEY", "itemcards"))
        except Exception as ex:  # noqa: BLE001
            print("old unregister failed:", repr(ex))
    for old_hook in old.mod.hooks:
        old_hook.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    new = importlib.reload(old)
else:
    new = importlib.import_module("bl4_item_cards")
new.mod.enable()
print("reloaded, enabled:", new.mod.is_enabled, "status:", new._status)
