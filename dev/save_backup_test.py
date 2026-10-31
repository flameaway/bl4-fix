"""
Save Backups offline test: backups, pruning, the page's view, restore staging and the real
restore_helper.ps1, end to end. Works on a temp copy of your saves (read only: the real save
folder is never written); the helper runs with a fake process name, so it doesn't wait for a game.

    python dev/save_backup_test.py

Written against the dev profile (4 characters: Rafa level 52 in 1.sav, Amon in 2.sav, Vex in
4.sav; 172 bank items): another profile needs its names and numbers changed below.
"""

import importlib
import importlib.machinery
import json
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods/bl4_save_backup"
pkg = types.ModuleType("sbk")
pkg.__path__ = [str(MOD)]
pkg.__spec__ = importlib.machinery.ModuleSpec("sbk", None, is_package=True)
pkg.__spec__.submodule_search_locations = [str(MOD)]
sys.modules["sbk"] = pkg
sf = importlib.import_module("sbk.savefile")
store = importlib.import_module("sbk.store")
restore = importlib.import_module("sbk.restore")
backups = importlib.import_module("sbk.backups")

ACCOUNTS = sf.accounts()
if not ACCOUNTS:
    raise SystemExit("no Borderlands 4 saves on this PC to copy")
SID_INT, REAL = ACCOUNTS[0]
SP = Path(tempfile.mkdtemp(prefix="bl4_save_backup_test_"))
SRC = SP / "saves"
SRC.mkdir()
for p in sf.save_files(REAL):
    shutil.copy2(p, SRC / p.name)
T = SP / "store_test"
SID = str(SID_INT)
live = T / "SaveGames" / SID / "Profiles/client"
live.mkdir(parents=True)
for p in SRC.glob("*.sav"):
    shutil.copy2(p, live / p.name)
base = T / "Backups"
exe = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4\OakGame\Binaries\Win64\Borderlands4.exe")
keys = sf.Keys(exe, SP / "keycache.json")
key = keys.key(int(SID), (SRC / "profile.sav").read_bytes())
assert key is not None, "no save key"

backups.W.store = store.Store(keys, base=base, saves_root=T / "SaveGames")
backups.W.ui_dir = T / "ui"
logs = []
backups.W.log = lambda text, error=False: logs.append(("ERR " if error else "") + text)
st = backups.W.store
checks = 0


def check(cond, what):
    global checks
    assert cond, what
    checks += 1


def run(fn):
    fn()
    backups._publish()


def view():
    return json.loads((T / "ui/view.json").read_text(encoding="utf-8"))


# 1. launch backup
t = time.perf_counter()
run(lambda: backups.backup("launch"))
print(f"launch backup {1000 * (time.perf_counter() - t):.0f} ms", logs[-1])
acc = st.active()
check(len(acc.sessions) == 1, "one backup")
first = st.ordered(acc)[0]
for p in SRC.glob("*.sav"):
    check((acc.dir / first / p.name).read_bytes() == p.read_bytes(), "copy is exact")
info = json.loads((acc.dir / first / "info.json").read_text(encoding="utf-8"))
check(all(f["ok"] for f in info["files"].values()), "all read whole")

# 2. unchanged: no new backup
run(lambda: backups.backup("launch"))
check(len(acc.sessions) == 1 and "Nothing changed" in logs[-1], "unchanged skipped")

# 3. play: character 1 gains a level and loses 2 items; the bank loses its slots past 2
text1 = sf.decrypt(key, (live / "1.sav").read_bytes())
lines = text1.split("\n")
# (the character's real level, whatever it is now: Rafa was 52 when this was written)
i = next(k for k, line in enumerate(lines) if line.startswith("    level: "))
LEVEL = int(lines[i].split(": ")[1])
lines[i] = f"    level: {LEVEL + 1}"
# drop backpack slot_0's three lines
j = lines.index("        slot_0: ")
del lines[j : j + 3]
(live / "1.sav").write_bytes(sf.encrypt(key, "\n".join(lines)))
ptext = sf.decrypt(key, (live / "profile.sav").read_bytes())
bl = sf.bank_block(ptext).split("\n")
cut = next(k for k, line in enumerate(bl) if line.startswith("            slot_2:"))
(live / "profile.sav").write_bytes(sf.encrypt(key, sf.with_bank(ptext, "\n".join(bl[:cut]))))
time.sleep(1.1)  # backup folder names are per second
run(lambda: backups.backup("manual"))
check(len(acc.sessions) == 2, "second backup")

v = view()
rafa = next(c for c in v["characters"] if c["name"] == "Rafa")
check(len(rafa["versions"]) == 2, "Rafa has 2 versions")
newest, older = rafa["versions"]
check(newest["current"] and not older["current"], "current flag")
check(newest["info"]["level"] == LEVEL + 1 and older["info"]["level"] == LEVEL, "levels")
check(newest["vs_older"]["level"] == 1 and newest["vs_older"]["items_removed"] == 1, f"delta {newest['vs_older']}")
check(older["gone_now"] == 1 and older["new_now"] == 0, "older has an item that's gone now")
amon = next(c for c in v["characters"] if c["name"] == "Amon")
check(len(amon["versions"]) == 1 and amon["versions"][0]["oldest_backup"] != amon["versions"][0]["backup"], "unchanged character = 1 version over 2 backups")
check(len(v["bank"]["versions"]) == 2 and v["bank"]["now"] == 2, f"bank {v['bank']['now']}")
oldbank = v["bank"]["versions"][1]
check(oldbank["items"] == 172 and oldbank["gone_now"] == 170, "old bank vs now")
check(v["count"] == 2 and v["fit_1gb"] > 1000, f"fit {v['fit_1gb']} per {v['per_backup']}")
print("1 GB holds", v["fit_1gb"], "backups of", v["per_backup"])

# 4. prune: 5 more distinct backups, keep 3, one marked Keep
for n in range(5):
    t1 = sf.decrypt(key, (live / "4.sav").read_bytes()).replace("  cash: ", "  cash: 1", 1)
    (live / "4.sav").write_bytes(sf.encrypt(key, t1))
    time.sleep(1.05)
    run(lambda: backups.backup("manual"))
check(len(acc.sessions) == 7, "7 backups")
oldest = st.ordered(acc)[-1]
run(lambda: backups.set_keep(oldest, True))
backups.W.keep = "3"
run(backups.prune)
names = st.ordered(acc)
check(len(names) == 4 and oldest in names, f"3 newest + the kept one: {names}")
check(len([p for p in acc.dir.iterdir() if p.is_dir()]) == 4, "folders removed")
backups.W.keep = backups.FIT
run(backups.prune)
check(len(st.ordered(acc)) == 4, "FIT removes nothing small")

# 5. stage restores: Rafa's older version, the old bank (from the kept backup), then cancel one
rafa = next(c for c in view()["characters"] if c["name"] == "Rafa")
older = next(x for x in rafa["versions"] if not x["current"])
# start_helper would start a real helper for Borderlands4: run it ourselves with a fake name below
restore.start_helper = lambda base: True
run(lambda: backups.stage("character", older["backup"], older["file"]))
oldbank = next(x for x in view()["bank"]["versions"] if not x["current"])
run(lambda: backups.stage("bank", oldbank["backup"]))
check(len(backups.W.pending) == 2, f"pending {backups.W.pending}")
plan = restore.pending(acc)
check(plan["key"] == key.hex() and {a["target"] for a in plan["actions"]} == {"1.sav", "profile.sav"}, "plan")
run(lambda: backups.stage("profile", oldbank["backup"]))  # replaces the bank action (same target)
check(len(backups.W.pending) == 2 and restore.pending(acc)["actions"][-1]["kind"] == "file", "profile replaced bank")
run(lambda: backups.stage("bank", oldbank["backup"]))
run(lambda: backups.cancel("1.sav"))
check([p["target"] for p in backups.W.pending] == ["profile.sav"], "cancel one")
run(lambda: backups.stage("character", older["backup"], older["file"]))
# a deleted slot: Vex (4.sav) gone, restore puts it back in 4.sav
vex_backup = st.ordered(acc)[-1]
(live / "4.sav").unlink()
run(lambda: backups.stage("character", vex_backup, "4.sav"))
check({p["target"] for p in backups.W.pending} == {"1.sav", "profile.sav", "4.sav"}, "deleted character restorable")
# another character in that slot now: refused
shutil.copy2(SRC / "2.sav", live / "4.sav")
st.refresh_live(acc)
try:
    restore.stage_character(st, acc, vex_backup, "4.sav")
    check(False, "should refuse")
except restore.RestoreError as ex:
    check("another character" in str(ex), str(ex))
(live / "4.sav").unlink()
st.refresh_live(acc)

# 6. the helper applies it (fake process name: no waiting)
r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(MOD / "restore_helper.ps1"),
                    "-Root", str(base), "-Process", "NoSuchProcessHere", "-SettleSeconds", "0"], capture_output=True, text=True, timeout=300)
check(r.returncode == 0, r.stderr)
run(backups.refresh)
res = backups.W.result
check(res and all(x["ok"] for x in res["results"]) and len(res["results"]) == 3, json.dumps(res))
now1 = sf.summarize(sf.decrypt(key, (live / "1.sav").read_bytes()))
check(now1["level"] == LEVEL, f"Rafa back to {LEVEL}")
nowp = sf.summarize(sf.decrypt(key, (live / "profile.sav").read_bytes()))
check(len(nowp["bank"]) == 172, "bank back")
check((live / "4.sav").read_bytes() == (acc.dir / vex_backup / "4.sav").read_bytes(), "Vex back")
st.scan()
before = [n for n in st.ordered(acc) if n.endswith("before-restore")]
check(len(before) == 1 and acc.sessions[before[0]]["label"] == "before_restore", "before-restore backup indexed")
check(acc.sessions[before[0]]["files"]["1.sav"]["level"] == LEVEL + 1, "before-restore holds the state before")
v = view()
check(any(s["label"] == "Before a restore" for s in v["sessions"]), "labelled on the page")
run(backups.dismiss_result)
check(backups.W.result is None and (acc.dir / "last_restore.seen.json").exists(), "dismissed")
check(not backups.W.pending, "nothing pending after")

# 7. a torn live file: retried, then kept as is, marked damaged
store.READ_TRIES, store.READ_RETRY_S = 2, 0.05
raw = (live / "2.sav").read_bytes()
(live / "2.sav").write_bytes(raw[: len(raw) // 2 // 16 * 16])
time.sleep(1.05)
run(lambda: backups.backup("manual"))
newest = st.ordered(acc)[0]
check(acc.sessions[newest]["files"]["2.sav"]["ok"] is False, "torn marked")
amon = next(c for c in view()["characters"] if c["name"] == "Amon")
try:
    restore.stage_character(st, acc, newest, "2.sav")
    check(False, "damaged copy should refuse")
except restore.RestoreError as ex:
    check("didn't read whole" in str(ex) or "no character" in str(ex), str(ex))
(live / "2.sav").write_bytes(raw)

# 9. backup_now (the Mod Menu asks before another modder's mod loads): the real worker thread, a bounded wait
store.READ_TRIES, store.READ_RETRY_S = 6, 0.5
time.sleep(1.05)
t = time.perf_counter()
check(backups.backup_now("before Test Mod loads", 10.0), "backup_now: True once the saves are in a backup")
print(f"backup_now {1000 * (time.perf_counter() - t):.0f} ms (worker + wait)")
newest = st.ordered(acc)[0]
check(acc.sessions[newest]["label"] == "before_mod", "backup_now: a new backup, labelled before_mod")
for p in live.glob("*.sav"):
    check((acc.dir / newest / p.name).read_bytes() == p.read_bytes(), "backup_now: exact copies of the live saves")
check(backups.backup_now("again", 10.0) and st.ordered(acc)[0] == newest, "backup_now: nothing changed = True, no second backup")
check(any("before Test Mod loads" in x for x in logs), "backup_now: logged with its reason")
backups._publish()
check(any(s["label"] == "Before loading a mod" for s in view()["sessions"]), "backup_now: labelled on the page")
saved_store, backups.W.store = backups.W.store, None
check(backups.backup_now("not set up", 1.0) is False, "backup_now: False when the mod isn't set up")
backups.W.store = saved_store

# 8. the page's view size
size = (T / "ui/view.json").stat().st_size
print(f"view.json {size / 1024:.1f} KB with {len(st.ordered(acc))} backups")
print("errors logged:", [x for x in logs if x.startswith("ERR")])
shutil.rmtree(SP, ignore_errors=True)
print(f"{checks}/{checks} passed")
