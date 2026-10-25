# Run through the bridge (game thread):  python tools/bridge.py <id> -f dev/perf/dump_uobjects.py
# Writes .perf/uobjects.json: every UFunction's native entry (UFunction::Func, +0xD8) and every
# UClass's CDO address + superclass, which symbols.py turns into names for the sampler's addresses.
# One-off: walks every Function and Class object (~1-3 s on the game thread, a visible hitch).
# ruff: noqa: F821
import ctypes
import json
import os
import time

_t = time.perf_counter()
_out = {"functions": [], "classes": []}
for _f in unrealsdk.find_all("Function", exact=False):
    try:
        _a = _f._get_address()
        _func = ctypes.c_uint64.from_address(_a + 0xD8).value
        if _func:
            _out["functions"].append((_func, f"{_f.Outer.Name}::{_f.Name}", int(_f.FunctionFlags)))
    except Exception:  # noqa: BLE001
        pass
for _c in unrealsdk.find_all("Class", exact=False):
    try:
        _cdo = _c.ClassDefaultObject
        _sup = _c.SuperField
        _out["classes"].append((_c.Name, _sup.Name if _sup else None, _cdo._get_address() if _cdo else 0))
    except Exception:  # noqa: BLE001
        pass
_path = os.path.join(os.environ.get("BL4_REPO", r"C:\Users\Josh\Documents\projects\bl4-direct"), ".perf",
                     "uobjects.json")
with open(_path + ".tmp", "w") as _fh:
    json.dump(_out, _fh)
os.replace(_path + ".tmp", _path)
print(len(_out["functions"]), "functions", len(_out["classes"]), "classes", f"{time.perf_counter() - _t:.2f}s", _path)
