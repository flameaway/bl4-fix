"""Real legendary rolls from the saves (read-only), with part tokens resolved to NCS part names."""
import ctypes, json, re, struct, sys, zlib
from collections import defaultdict
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "mods" / "bl4_reroll"))
import serial as S  # noqa: E402

GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
HERE = Path(__file__).parent

# --- save decrypt (same as bl4_weapondex/savegame.py, read only)
_b = ctypes.WinDLL("bcrypt")
_alg = ctypes.c_void_p()
_b.BCryptOpenAlgorithmProvider(ctypes.byref(_alg), ctypes.c_wchar_p("AES"), None, 0)
_m = ctypes.create_unicode_buffer("ChainingModeECB")
_b.BCryptSetProperty(_alg, ctypes.c_wchar_p("ChainingMode"), _m, ctypes.sizeof(_m), 0)


def decrypt(key, blob):
    blob = blob[: len(blob) // 16 * 16]
    h = ctypes.c_void_p()
    _b.BCryptGenerateSymmetricKey(_alg, ctypes.byref(h), None, 0, key, 32, 0)
    out = ctypes.create_string_buffer(len(blob)); got = wintypes.ULONG()
    _b.BCryptDecrypt(h, blob, len(blob), None, None, 0, out, len(blob), ctypes.byref(got), 0)
    _b.BCryptDestroyKey(h)
    return zlib.decompressobj().decompress(out.raw).decode("utf-8", "replace")


def save_serials():
    off = json.loads((GAME / "sdk_mods/settings/weapondex_savekey.json").read_text())["offset"]
    base = (GAME / "OakGame/Binaries/Win64/Borderlands4.exe").open("rb")
    base.seek(off); raw = base.read(32)
    root = Path.home() / "Documents/My Games/Borderlands 4/Saved/SaveGames"
    found = {}
    for acct in root.iterdir():
        if not acct.name.isdigit():
            continue
        sid = struct.pack("<Q", int(acct.name))
        key = bytes(a ^ b for a, b in zip(raw[:8], sid)) + raw[8:]
        for sav in (acct / "Profiles/client").glob("*.sav"):
            text = decrypt(key, sav.read_bytes())
            for m in re.finditer(r"'(@Ug[^']+)'", text):
                found.setdefault(m.group(1), sav.name)
    return found


# --- inv data: per type root serial index -> {part index: (table, name)}
def inv_tables():
    entries = json.loads((HERE / "inv_entries.json").read_text(encoding="utf-8"))
    roots, parts = {}, defaultdict(dict)
    by_key = defaultdict(list)
    for e in entries:
        by_key[e["key"].lower()].append(e)
    for key, es in by_key.items():
        root = next((e["value"] for e in es if e.get("value")), None)
        if not root or (root.get("serialindex") or {}).get("_scope") != "Root":
            continue
        ti = int(root["serialindex"]["index"])
        roots[ti] = key
        for e in es:
            for d in e.get("deps") or []:
                si = (d["value"] or {}).get("serialindex") or {}
                if si.get("index") is not None:
                    parts[ti][int(si["index"])] = (d["table"], d["key"].lower())
    return roots, parts, by_key


def resolve(serial, roots, parts):
    s = S.decode(serial)
    ti = s.item_type
    out = []
    for p in s.parts():
        if p.values is not None:  # {table:[...]}
            for v in p.values:
                out.append((p.index, v.value, *parts[p.index].get(v.value, ("?", "?"))))
        elif p.value is not None and p.index in roots and p.index != ti:  # {table:part}
            out.append((p.index, p.value, *parts[p.index].get(p.value, ("?", "?"))))
        else:
            out.append((ti, p.index, *parts[ti].get(p.index, ("?", "?"))) + ((p.value,) if p.value is not None else ()))
    return s, ti, out


if __name__ == "__main__":
    roots, parts, _ = inv_tables()
    found = save_serials()
    rows = []
    for serial, src in found.items():
        try:
            s, ti, res = resolve(serial, roots, parts)
        except Exception as ex:
            continue
        comps = [r for r in res if r[2] == "inv_comp"]
        if not any("legendary" in r[3] for r in comps):
            continue
        rows.append({"serial": serial, "src": src, "type": roots.get(ti, ti), "text": str(s), "parts": res})
    (HERE / "real_rolls.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(len(found), "serials,", len(rows), "legendary")
    for r in rows[:4]:
        print(r["type"], r["text"])
        for p in r["parts"]:
            print("   ", p)
