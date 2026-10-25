# Hot-reloads BL4 Direct's co-op channel (bl4_direct/coop.py) through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/reload_coop.py
# The module re-adds its hooks under the same identifiers and keeps the features and handlers
# other mods registered. Requests still waiting for an answer are dropped.
import importlib
import sys

name = "bl4_direct.coop"
module = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)
print("co-op channel loaded; features:", module.my_features(), "role:", module.role())
