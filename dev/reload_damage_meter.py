# Loads or hot-reloads the Carnage Meter mod through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_damage_meter.py
# Disables the old copy (unbinds its delegate entries, unregisters its page), reloads every
# submodule, then imports the package again and enables it. The page's JS/CSS reload themselves.
# The tallies (players, fights, bosses being fought, the last boss report) carry over, so a reload
# in the middle of a fight doesn't start it over; the characters are bound again on the next upkeep.
import importlib
import sys

import mods_base.mod_list

PKG = "bl4_damage_meter"
SUBMODULES = ("itemkey", "pak", "native", "naming", "tracker", "share", "page", "popup_page")
TRACKER_KEEP = ("players", "order", "bosses", "report", "report_ids", "fight_start", "fight_last", "fight_no", "fight_closed",
                "session_start", "session_last", "revision", "events", "counted", "unknown", "errors",
                "busy", "worst", "drains", "drain_busy", "drain_worst", "busy_seen", "frame_worst")  # fmt: skip
PAGE_KEEP = ("view", "report_seen", "toast")

old = sys.modules.get(PKG)
if old is not None:
    kept_tracker = {k: getattr(old.tracker.S, k) for k in TRACKER_KEEP if hasattr(old.tracker.S, k)}
    kept_tracker["bosses"] = dict(old.tracker.S.bosses)  # disabling clears the watched bosses
    kept_page = {k: getattr(old.page.S, k) for k in PAGE_KEEP if hasattr(old.page.S, k)}
    # The new copy syncs the pop-up page when it's enabled: the old one mustn't remove it meanwhile
    old._sync_popup = lambda _enabled: None  # noqa: SLF001
    try:
        old._disable()  # noqa: SLF001
    except Exception as ex:  # noqa: BLE001
        print("old disable failed:", repr(ex))
    for old_hook in old.mod.hooks:
        old_hook.disable()
    if old.mod in mods_base.mod_list.mod_list:
        mods_base.mod_list.mod_list.remove(old.mod)
    for name in SUBMODULES:
        if f"{PKG}.{name}" in sys.modules:
            importlib.reload(sys.modules[f"{PKG}.{name}"])
    new = importlib.reload(old)
    for k, v in kept_tracker.items():
        setattr(new.tracker.S, k, v)
    for k, v in kept_page.items():
        setattr(new.page.S, k, v)
    print(f"kept {len(new.tracker.S.players)} player(s), {len(new.tracker.S.bosses)} boss(es),"
          f" report: {bool(new.tracker.S.report)}")
else:
    new = importlib.import_module(PKG)
    mods_base.mod_list.mod_list.append(new.mod) if new.mod not in mods_base.mod_list.mod_list else None
new.mod.enable()
print("loaded, enabled:", new.mod.is_enabled)
