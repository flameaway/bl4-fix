# Status of the Minimap's tile cache, through the bridge (dev only):
#   python tools/bridge.py <id> -f dev/minimap_tiles_status.py
# ruff: noqa: F821
import sys

_S = sys.modules["bl4_minimap.scene"].Scene
_T = sys.modules["bl4_minimap.tiles"].Tiles
print("session", _T.session)
print("folder", _T.folder, "on disk", len(_T.index.get("tiles", {})) if _T.index else 0)
print("tiles in memory", {k: ("settling" if t.settle else "done", "saved" if t.saved else "unsaved", t.slot.kind)
                          for k, t in sorted(_T.tiles.items())})
print("slots", len(_T.slots), "version", _T.version, "mask", None if _T.mask is None else _T.mask[:2])
print("pictures", [(e.kind, e.key is not None, e.stale, e.version) for e in _S.entries])
print("shown", _S.current.kind if _S.current else None, _S.current.key if _S.current else None)
