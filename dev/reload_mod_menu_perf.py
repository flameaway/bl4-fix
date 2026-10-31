# Hot-reloads the Mod Menu's Performance page (bl4_mod_menu/perf/) through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_mod_menu_perf.py
# Ends a running benchmark (hooks put back), reloads the perf modules, and starts the new copy.
# The page's script and styles (ui/perf.js|css) reload themselves in the page. A change to the Mod
# Menu's own __init__ needs dev/reload_mod_menu.py instead.
import importlib
import sys

pkg = sys.modules.get("bl4_mod_menu.perf")
if pkg is None:
    raise SystemExit("bl4_mod_menu.perf isn't loaded: reload the Mod Menu (dev/reload_mod_menu.py)")
pkg.disable()
for name in ("bl4_mod_menu.perf.notice", "bl4_mod_menu.perf.profiler", "bl4_mod_menu.perf.bench", "bl4_mod_menu.perf.page"):
    if name in sys.modules:
        importlib.reload(sys.modules[name])
pkg = importlib.reload(pkg)
pkg.enable()
print("performance page reloaded")
