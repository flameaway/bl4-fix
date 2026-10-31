"""
Exercises the installer logic against fake game folders (fresh machine, repair, conflicts,
uninstall) without touching the real install or the real firewall rule.

    python installer/build.py      # once, to produce installer/build/payload
    python dev/installer_test.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "installer"))

import build  # noqa: E402
import catalog  # noqa: E402
import core  # noqa: E402
import shared  # noqa: E402  (core puts the payload copy on sys.path)

firewall_calls: list[tuple] = []
core.game_running = lambda: False
firewall_runs: list = []  # how each firewall call ran: None = directly (admin), else the UAC runner
core.firewall.apply_rule = lambda port, ips, lan, run=None: (
    firewall_runs.append(run) or firewall_calls.append(("apply", port, list(ips), lan)) or "ok"
)
core.firewall.remove_rule = lambda run=None: firewall_runs.append(run) or firewall_calls.append(("remove",)) or "ok"
core.firewall.rule_summary = lambda: None  # no rule yet; tests switch it to "stubbed" once one exists
core.is_admin = lambda: True  # never a real UAC prompt from tests (elevation is stubbed below)
elevated_calls: list[list[str]] = []
core._run_self_elevated = lambda args, log=print: elevated_calls.append(list(args))  # noqa: SLF001
core._make_shortcut = lambda target, log=print: None  # noqa: SLF001  (no Start menu entries from tests)

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def fake_game(root: Path) -> Path:
    game = root / "Borderlands 4"
    exe = game / core.GAME_EXE_REL
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ fake")
    return game


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bl4d_test_"))
    logs: list[str] = []
    log = logs.append
    try:
        # --- fresh machine ---
        game = fake_game(tmp / "fresh")
        st = core.status(game)
        check(st.sdk is None and st.mod is None and not st.ready, "fresh: nothing installed, not ready")

        core.install(game, log)
        sdk_mods = game / "sdk_mods"
        win64 = game / core.WIN64_REL
        check((win64 / "dsound.dll").is_file(), "fresh: SDK loader dsound.dll installed")
        check(core.installed_sdk_version(game) == shared.SDK_VERSION, f"fresh: SDK version {shared.SDK_VERSION}")
        check((sdk_mods / "mods_base.sdkmod").is_file(), "fresh: mods_base installed")
        check(core.installed_mod_version(game) == shared.MOD_VERSION, f"fresh: mod version {shared.MOD_VERSION}")
        settings = json.loads((sdk_mods / "settings" / "bl4_direct.json").read_text())
        check(settings.get("enabled") is True, "fresh: mod enabled in settings")
        check((sdk_mods / "bl4_item_cards.sdkmod").is_file(), "fresh: item cards mod installed")
        cards_settings = json.loads((sdk_mods / "settings" / "bl4_item_cards.json").read_text())
        check(cards_settings.get("enabled") is True, "fresh: item cards mod enabled in settings")
        check(core.friends_file(game).is_file(), "fresh: friends file created")
        check(not firewall_calls, f"fresh: firewall rule off by default, firewall untouched {firewall_calls}")
        cfg = core.load_config(game)
        check(not cfg.firewall and cfg.firewall_up_to_date(), "fresh: firewall off recorded as up to date")
        check(core.status(game).ready and core.status(game).firewall_ok, "fresh: status ready, firewall OK")

        # --- turned on: the rule is made ---
        core.install(game, log, use_firewall=True)
        check(firewall_calls[-1] == ("apply", 7777, [], True), f"on: firewall applied with defaults {firewall_calls[-1]}")
        check(core.load_config(game).firewall_up_to_date(), "on: firewall recorded as up to date")
        core.firewall.rule_summary = lambda: "stubbed"
        cfg = core.load_config(game)

        # --- repair keeps user data ---
        (sdk_mods / "settings" / "other_mod.json").write_text('{"x": 1}')
        (sdk_mods / "other_mod.sdkmod").write_bytes(b"PK other")
        cfg.friends.append(shared.Friend("Bob", "203.0.113.5"))
        cfg.save(core.friends_file(game))
        loose = sdk_mods / "bl4_direct"
        loose.mkdir()
        (loose / "__init__.py").write_text("# stale manual copy")
        core.install(game, log)
        check(json.loads((sdk_mods / "settings" / "other_mod.json").read_text()) == {"x": 1},
              "repair: other mods' settings untouched")
        check(not loose.exists() and (sdk_mods / ".bl4_direct_old_folder").is_dir(),
              "repair: loose mod folder moved aside (it would shadow the .sdkmod)")
        check([f.name for f in core.load_config(game).friends] == ["Bob"], "repair: friends kept")
        check(firewall_calls[-1] == ("apply", 7777, ["203.0.113.5"], True), "repair: firewall gets friend IP")

        # --- leading-zero IPs are normalized, junk is skipped ---
        cfg = core.load_config(game)
        cfg.friends.append(shared.Friend("Zero", "21.142.145.01"))
        cfg.friends.append(shared.Friend("Junk", "not-an-ip"))
        cfg.save(core.friends_file(game))
        core.apply_firewall(game, log)
        check(firewall_calls[-1][2] == ["203.0.113.5", "21.142.145.1"],
              f"firewall: '01' normalized, junk skipped {firewall_calls[-1][2]}")

        # --- firewall rule turned off: removed once, then left alone; on again re-applies ---
        core.install(game, log, use_firewall=False)
        cfg = core.load_config(game)
        check(firewall_calls[-1] == ("remove",), f"firewall off: old rule removed {firewall_calls[-1]}")
        check(not cfg.firewall and cfg.firewall_up_to_date(), "firewall off: saved, nothing for the game to update")
        check([f.name for f in cfg.friends] == ["Bob", "Zero", "Junk"], "firewall off: friends kept")
        calls = len(firewall_calls)
        core.firewall.rule_summary = lambda: None
        core.install(game, log)
        check(len(firewall_calls) == calls, "firewall off: a repair doesn't touch the firewall")
        check(core.status(game).firewall_ok, "firewall off + no rule: status OK")
        core.firewall.rule_summary = lambda: "stubbed"
        core.install(game, log, use_firewall=True)
        check(firewall_calls[-1][0] == "apply" and core.load_config(game).firewall, "firewall on again: rule applied")

        # --- friends files from before the switch: on if the app had made the rule ---
        legacy = tmp / "legacy.json"
        legacy.write_text(json.dumps({"host_port": 7777, "friends": [], "firewall_applied": "7777|True|"}))
        check(shared.Config.load(legacy).firewall, "legacy: a file with an applied rule keeps it on")
        legacy.write_text(json.dumps({"host_port": 7777, "friends": []}))
        check(not shared.Config.load(legacy).firewall, "legacy: no rule made before -> off")
        core.friends_file(game).unlink()
        core.install(game, log)
        check(firewall_calls[-1][0] == "apply" and core.load_config(game).firewall,
              "friends file deleted, rule there: a repair keeps the rule")

        # --- not admin: only the firewall step asks for it (netsh elevated, not a copy of the app) ---
        core.is_admin = lambda: False
        calls, n = len(firewall_calls), len(elevated_calls)
        core.install(game, log)
        check(len(firewall_calls) == calls + 1 and firewall_runs[-1] is core.run_as_admin and len(elevated_calls) == n,
              f"not admin: firewall rule through netsh's own UAC prompt {firewall_runs[-1:]}")
        check(shared.UAC_HINT in logs, "not admin: the log names the prompt (Network Command Shell)")
        cfg = core.load_config(game)
        cfg.firewall = False
        cfg.save(core.friends_file(game))
        core.firewall.rule_summary = lambda: None
        n = len(elevated_calls)
        core.install(game, log)
        check(len(elevated_calls) == n, "not admin, rule off and none there: no admin prompt at all")
        writable = core._writable  # noqa: SLF001
        core._writable = lambda game_dir: False  # noqa: SLF001
        core.install(game, log, mods={"bl4_reroll"}, use_firewall=True)
        check(elevated_calls[-1] == ["--install", "--game", str(game), "--mods", "bl4_reroll", "--firewall", "on"],
              f"not admin, protected game folder: whole install elevated {elevated_calls[-1]}")
        core._writable = writable  # noqa: SLF001
        core.is_admin = lambda: True
        core.firewall.rule_summary = lambda: "stubbed"

        # --- optional mods: defaults, add, remove, keep the player's off switch ---
        check(core.installed_mods(game) == catalog.DEFAULTS, f"mods: first install = defaults {sorted(core.installed_mods(game))}")
        mm = json.loads((sdk_mods / "settings" / "bl4_mod_menu.json").read_text())
        check(mm.get("enabled") is True, "mods: Mod Menu (required) enabled")
        paks = game / core.MOD_PAKS_REL
        paks.mkdir(parents=True, exist_ok=True)
        ui = game / core.UI_REL
        for n in ("pakchunk0-Windows_4242_P.pak", "pakchunk0-Windows_4242_P.pak.next", "pakchunk0-Windows_4244_P.pak", "other_P.pak"):
            (paks / n).write_bytes(b"pak")
        for d in ("bl4direct", "bl4respec", "someone_else"):
            (ui / d).mkdir(parents=True, exist_ok=True)
            (ui / d / "x.js").write_text("x")
        core.install(game, log, mods={"bl4_dialog_skipper", "bl4_respec"})
        got = core.installed_mods(game)
        check(got == {"bl4_direct", "bl4_mod_menu", "bl4_dialog_skipper", "bl4_respec"}, f"mods: chosen set installed {sorted(got)}")
        check(not (sdk_mods / "bl4_item_cards.sdkmod").exists(), "mods: unticked item cards removed")
        check(not (paks / "pakchunk0-Windows_4242_P.pak").exists() and not (paks / "pakchunk0-Windows_4242_P.pak.next").exists(),
              "mods: item cards pak and its staging file removed")
        check(not (ui / "bl4direct").exists(), "mods: item cards loose UI folder removed")
        check((paks / "pakchunk0-Windows_4244_P.pak").exists() and (ui / "bl4respec").exists(), "mods: kept mod's pak and UI kept")
        check((paks / "other_P.pak").exists() and (ui / "someone_else").exists(), "mods: other paks / UI folders untouched")
        check(json.loads((sdk_mods / "settings" / "bl4_item_cards.json").read_text()).get("enabled") is True,
              "mods: removed mod's settings kept")
        ds = json.loads((sdk_mods / "settings" / "bl4_dialog_skipper.json").read_text())
        check(ds.get("enabled") is True, "mods: newly installed mod enabled")
        import zipfile  # noqa: PLC0415
        with zipfile.ZipFile(sdk_mods / "bl4_dialog_skipper.sdkmod") as zf:
            names = zf.namelist()
        check("bl4_dialog_skipper/LICENSE" in names and "bl4_dialog_skipper/__init__.py" in names, "mods: port ships its GPL LICENSE")
        local = {p.name for p in (REPO / "ported" / "bl4_dialog_skipper").rglob("*") if p.name in build.LOCAL_ONLY}
        check(not any(n.rsplit("/", 1)[-1] in local or "__pycache__" in n for n in names), "mods: no local-only files / pycache in .sdkmod")

        (sdk_mods / "settings" / "bl4_dialog_skipper.json").write_text(json.dumps({"enabled": False, "x": 2}))
        core.install(game, log)
        check(core.installed_mods(game) == got, "mods: repair keeps the chosen set")
        ds = json.loads((sdk_mods / "settings" / "bl4_dialog_skipper.json").read_text())
        check(ds == {"enabled": False, "x": 2}, f"mods: repair keeps a mod the player switched off {ds}")
        record = json.loads((sdk_mods / "settings" / core.INSTALL_RECORD).read_text())
        check(record.get("mods") == sorted(got), "mods: install record lists the mods")

        # A newer mod (put in by the in-game updater) isn't replaced by this app's older copy
        import io  # noqa: PLC0415
        rs = sdk_mods / "bl4_respec.sdkmod"
        buf = io.BytesIO()
        with zipfile.ZipFile(rs) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                body = src.read(item)
                if item.filename == "bl4_respec/pyproject.toml":
                    body = body.replace(b'version = "', b'version = "9', 1)
                dst.writestr(item, body)
        rs.write_bytes(buf.getvalue())
        newer = core.sdkmod_version(rs, "bl4_respec")
        core.install(game, log)
        check(core.sdkmod_version(rs, "bl4_respec") == newer, f"downgrade: newer installed mod kept ({newer})")
        shutil.copyfile(core.payload_mod("bl4_respec"), rs)

        try:
            core.install(game, log, mods={"bl4_nope"})
            check(False, "mods: unknown id should be refused")
        except RuntimeError as ex:
            check("bl4_nope" in str(ex), "mods: unknown id refused")

        # --- skip intro is a mod with a switch ---
        movies_dir = game / "OakGame" / "Content" / "Movies"
        movies_dir.mkdir(parents=True, exist_ok=True)
        for name in ("2KLOGO", "GBXLOGO"):
            (movies_dir / f"{name}.bk2").write_bytes(b"bk2")
        core.install(game, log, skip_intro=True)
        check("bl4_skip_intro" in core.installed_mods(game) and core.status(game).intro_skipped, "intro: on = installed + movies aside")
        check("bl4_dialog_skipper" in core.installed_mods(game), "intro: switch keeps the other mods")
        core.install(game, log, mods=set(core.installed_mods(game)) - {"bl4_skip_intro"})
        check(not (sdk_mods / "bl4_skip_intro.sdkmod").exists() and (movies_dir / "2KLOGO.bk2").exists(),
              "intro: unticked = removed + movies back")
        check(core.choose_mods(game, set()) == set(catalog.REQUIRED), "choose: none = required only")
        core.install(game, log, mods=set(catalog.BY_ID))
        check(core.installed_mods(game) == set(catalog.BY_ID), f"mods: all {len(catalog.MODS)} install")

        # --- Weapondex pictures come with the mod ---
        with zipfile.ZipFile(core.payload_assets("bl4_weapondex")) as zf:
            bundled = {f: json.loads(zf.read(f"{f}/index.json"))
                       for f in ("bl4weapondex/bosses", "bl4weapondex/items", "bl4weapondex/bosses/small", "bl4weapondex/items/small")}
        for folder, index in bundled.items():
            got_index = json.loads((ui / folder / "index.json").read_text())
            pngs = {p.stem for p in (ui / folder).glob("*.png")}
            check(got_index == index and pngs == set(index), f"pictures: {folder} has all {len(index)} pictures + index")
        boss = next(iter(bundled["bl4weapondex/bosses"]))
        small_png, full_png = ui / "bl4weapondex/bosses/small" / f"{boss}.png", ui / "bl4weapondex/bosses" / f"{boss}.png"
        check(small_png.stat().st_mtime >= full_png.stat().st_mtime, "pictures: small copy not older than its picture")
        # A picture the player took later (newer version) and one we don't ship survive a repair
        boss_index = ui / "bl4weapondex/bosses/index.json"
        mine = json.loads(boss_index.read_text())
        mine[boss] = 9_999_999_999
        mine["my-own-boss"] = 5
        boss_index.write_text(json.dumps(mine))
        full_png.write_bytes(b"my picture")
        core.install(game, log)
        after = json.loads(boss_index.read_text())
        check(full_png.read_bytes() == b"my picture" and after[boss] == 9_999_999_999 and after.get("my-own-boss") == 5,
              "pictures: repair keeps the player's newer and extra pictures")
        core.install(game, log, mods=set(catalog.BY_ID) - {"bl4_weapondex"})
        check(not (ui / "bl4weapondex").exists(), "pictures: unticking Weapondex removes them")
        core.install(game, log, mods=set(catalog.DEFAULTS))

        # --- uninstall, keep SDK ---
        core.uninstall(game, remove_sdk=False, log=log)
        check(core.installed_mod_version(game) is None, "uninstall: mod removed")
        check(not (sdk_mods / "bl4_item_cards.sdkmod").exists(), "uninstall: item cards mod removed")
        check(core.installed_sdk_version(game) == shared.SDK_VERSION, "uninstall: SDK kept when asked")
        check(core.friends_file(game).is_file(), "uninstall: friends list kept")
        check(firewall_calls[-1] == ("remove",), "uninstall: firewall rule removed")
        left = [m.id for m in catalog.MODS if (sdk_mods / f"{m.id}.sdkmod").exists()]
        check(not left, f"uninstall: every catalog mod removed {left}")
        check(not (paks / "pakchunk0-Windows_4244_P.pak").exists() and (paks / "other_P.pak").exists(),
              "uninstall: our paks removed, others kept")

        # --- uninstall, remove SDK ---
        core.install(game, log)
        core.uninstall(game, remove_sdk=True, log=log)
        check(not (win64 / "dsound.dll").exists() and core.installed_sdk_version(game) is None,
              "uninstall+sdk: SDK files removed")
        check((sdk_mods / "other_mod.sdkmod").is_file(), "uninstall+sdk: other mods left alone")

        # --- bug report: the installed Mod Menu's report.py, private details masked ---
        game3 = fake_game(tmp / "report")
        mods3 = game3 / "sdk_mods"
        (mods3 / "settings").mkdir(parents=True)
        with zipfile.ZipFile(mods3 / "bl4_mod_menu.sdkmod", "w") as zf:
            zf.write(REPO / "mods" / "bl4_mod_menu" / "report.py", "bl4_mod_menu/report.py")
            zf.writestr("bl4_mod_menu/pyproject.toml", '[project]\nname = "BL4 Mod Menu"\nversion = "9.9.9"\n')
        user = Path.home().name
        (mods3 / "settings" / "bl4_direct_friends.json").write_text(json.dumps({"friends": [{"name": "Moxxi", "address": "203.0.113.77", "port": 7777}]}))
        (mods3 / "settings" / "bl4_mod_menu.json").write_text(json.dumps({"enabled": True, "options": {"Last host": "Moxxi 203.0.113.77"}}))
        (mods3 / "settings" / "weapondex_savekey.json").write_text('{"key": "secret"}')
        (mods3 / "settings" / "bl4fix_errors.jsonl").write_text(
            json.dumps({"t": "2026-10-29T18:00:00", "session": "S", "mod": "bl4_minimap", "version": "0.1.7", "sig": "s1",
                        "msg": "boom", "trace": f"File \"C:\\Users\\{user}\\x.py\"", "map": None, "role": None}) + "\n"
            + json.dumps({"t": "2026-10-29T18:01:00", "session": "S", "sig": "s1", "count": 5}) + "\n")
        plugins = game3 / core.WIN64_REL / "Plugins"
        plugins.mkdir(parents=True)
        (plugins / "unrealsdk.log").write_text(
            "date time thread location v|\n"
            f"2026-10-29 18:02:45.639Z   562c   ~ C:\\Users\\{user}\\m.py@7     ERR| joining 203.0.113.77 failed\n")
        (game3 / core.MOD_PAKS_REL).mkdir(parents=True)
        (game3 / core.MOD_PAKS_REL / "someone_P.pak").write_bytes(b"pak")
        out = tmp / "reports"
        zip_path, url = core.bug_report(game3, out, log)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            text = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
            report_json = json.loads(zf.read("report.json"))
        check({"summary.txt", "report.json", "errors.jsonl", "logs/unrealsdk.log", "logs/unrealsdk.errors.txt",
               "settings/bl4_mod_menu.json"} <= set(names), f"report: zip has its files {names}")
        check(not any("friends" in n or "savekey" in n for n in names), "report: friends list and save key left out")
        check("203.0.113.77" not in text and "Moxxi" not in text and f"\\{user}\\" not in text, "report: IP, friend and user name masked")
        check([e.get("count") for e in report_json["errors"]] == [5], f"report: count lines folded {report_json['errors']}")
        check(any(p["name"] == "someone_P.pak" for p in report_json["paks"]), "report: ~mods paks listed")
        check(url.startswith("https://github.com/") and "template=bug.yml" in url and "mod_menu+9.9.9" in url and len(url) < 2000,
              f"report: issue link with versions ({len(url)} chars)")
        for _ in range(6):
            core.bug_report(game3, out, log)
        check(len(list(out.glob("bl4fix-report-*.zip"))) == 5, "report: newest 5 kept")
        (mods3 / "bl4_mod_menu.sdkmod").unlink()
        try:
            core.bug_report(game3, out, log)  # falls back to the payload copy
            check(True, "report: payload copy used without an installed Mod Menu")
        except RuntimeError as ex:
            check("No Mod Menu" in str(ex), "report: payload without report.py -> clear error (rebuild the payload)")

        # --- safe mode: other modders' mods set aside in sdk_mods/.bl4_safe_mode, and back ---
        game4 = fake_game(tmp / "safe")
        core.install(game4, log, mods={"bl4_reroll"})
        mods4 = game4 / "sdk_mods"
        sdk_own = sorted(p.name for p in mods4.iterdir())
        (mods4 / "PersistentLoot.sdkmod").write_bytes(b"PK other")
        (mods4 / "better_hud").mkdir()
        (mods4 / "better_hud" / "__init__.py").write_text("# someone's mod")
        (mods4 / "My Mod v1.2").mkdir()  # the SDK doesn't load dotted folders: left alone
        (mods4 / "_scratch").mkdir()
        (mods4 / "readme.txt").write_text("not a mod")
        (mods4 / "bl4_minimap").mkdir()  # a catalog mod as a loose folder: BL4 Fix's, stays
        (mods4 / "settings" / "better_hud.json").write_text('{"enabled": true}')
        st = core.safe_mode_status(game4)
        check(not st["on"] and st["others"] == ["better_hud", "PersistentLoot.sdkmod"],
              f"safe mode: status off, counts the 2 other modders' mods {st['others']}")
        result = core.set_safe_mode(game4, True, log)
        aside = game4 / core.SAFE_MODE_REL
        record = json.loads((aside / core.SAFE_MODE_RECORD).read_text())
        check(sorted(result["moved"]) == ["PersistentLoot.sdkmod", "better_hud"] and not result["skipped"],
              f"safe mode on: moved only the other modders' mods {result}")
        check((aside / "PersistentLoot.sdkmod").is_file() and (aside / "better_hud" / "__init__.py").is_file()
              and not (mods4 / "PersistentLoot.sdkmod").exists() and not (mods4 / "better_hud").exists(),
              "safe mode on: they're in .bl4_safe_mode, gone from sdk_mods")
        check(all((mods4 / n).exists() for n in sdk_own) and (mods4 / "bl4_reroll.sdkmod").is_file()
              and (mods4 / "bl4_minimap").is_dir(), f"safe mode on: SDK files and BL4 Fix's mods stay {sdk_own}")
        check((mods4 / "My Mod v1.2").is_dir() and (mods4 / "_scratch").is_dir() and (mods4 / "readme.txt").is_file()
              and (mods4 / "settings" / "better_hud.json").is_file(), "safe mode on: non-mods, _ folders and settings stay")
        check(record.get("format") == 1 and sorted(record.get("moved", [])) == ["PersistentLoot.sdkmod", "better_hud"]
              and isinstance(record.get("time"), int) and record["time"] > 0 and set(record) == {"format", "moved", "time"},
              f"safe mode on: record written {record}")
        check(not list(aside.glob("*.tmp")), "safe mode on: no temp file left")
        st = core.safe_mode_status(game4)
        check(st["on"] and len(st["aside"]) == 2 and not st["others"] and not st["left"], f"safe mode: status on, 2 aside {st}")
        # Added while on: a second On sets it aside too, the record keeps the first two
        (mods4 / "late.sdkmod").write_bytes(b"PK late")
        check(core.safe_mode_status(game4)["others"] == ["late.sdkmod"], "safe mode: a mod added while on is listed")
        core.set_safe_mode(game4, True, log)
        record = json.loads((aside / core.SAFE_MODE_RECORD).read_text())
        check(sorted(record["moved"]) == ["PersistentLoot.sdkmod", "better_hud", "late.sdkmod"], f"safe mode on again: record extended {record['moved']}")

        # Off, with one name back in sdk_mods: that one is skipped and reported, the rest return
        (mods4 / "better_hud").mkdir()
        (mods4 / "better_hud" / "__init__.py").write_text("# reinstalled")
        n_logs = len(logs)
        result = core.set_safe_mode(game4, False, log)
        check(sorted(result["moved"]) == ["PersistentLoot.sdkmod", "late.sdkmod"]
              and [s[0] for s in result["skipped"]] == ["better_hud"], f"safe mode off: restored 2, conflict skipped {result}")
        check(any("better_hud" in line for line in logs[n_logs:]), "safe mode off: the skipped one is logged")
        check((mods4 / "PersistentLoot.sdkmod").read_bytes() == b"PK other" and (mods4 / "late.sdkmod").is_file(),
              "safe mode off: files back unchanged")
        check((mods4 / "better_hud" / "__init__.py").read_text() == "# reinstalled" and (aside / "better_hud" / "__init__.py").is_file(),
              "safe mode off: neither copy of the conflict overwritten")
        check(not (aside / core.SAFE_MODE_RECORD).exists() and aside.is_dir(), "safe mode off: record gone, folder kept (not empty)")
        st = core.safe_mode_status(game4)
        check(not st["on"] and st["left"] == ["better_hud"] and len(st["others"]) == 3,
              f"safe mode: status off, 1 left aside, 3 others {st}")
        # The conflict cleared: the next Off puts the leftover back and removes the folder
        shutil.rmtree(mods4 / "better_hud")
        result = core.set_safe_mode(game4, False, log)
        check(result["moved"] == ["better_hud"] and (mods4 / "better_hud" / "__init__.py").read_text() == "# someone's mod"
              and not aside.exists(), f"safe mode off again: leftover back, empty folder removed {result}")
        # A record that names a path outside sdk_mods is never followed
        aside.mkdir()
        (aside / core.SAFE_MODE_RECORD).write_text(json.dumps({"format": 1, "moved": ["..\\..\\x", "../y"], "time": 1}))
        result = core.set_safe_mode(game4, False, log)
        check(not result["moved"] and not aside.exists(), "safe mode off: path names in the record ignored")

        core.game_running = lambda: True
        try:
            core.set_safe_mode(game4, True, log)
            check(False, "safe mode: should refuse while BL4 runs")
        except RuntimeError as ex:
            check("Close Borderlands 4" in str(ex) and not aside.exists() and (mods4 / "late.sdkmod").is_file(),
                  "safe mode: refused while BL4 runs, nothing moved")
        core.game_running = lambda: False

        # --- build.py: optional exe signing (signtool stubbed: never a real signing) ---
        sign_dir = tmp / "sign"
        sign_dir.mkdir()
        exe = sign_dir / "BL4FixSetup.exe"
        exe.write_bytes(b"MZ setup")
        pfx, dlib, meta, tool = (sign_dir / n for n in ("cert.pfx", "Azure.CodeSigning.Dlib.dll", "metadata.json", "signtool.exe"))
        for f in (pfx, dlib, meta, tool):
            f.write_bytes(b"x")
        runs: list[list[str]] = []

        def fake_run(cmd, check=False, code=0):  # noqa: ANN001, ANN202
            runs.append(cmd)
            return type("R", (), {"returncode": code})()

        check(build.sign_exe(exe, {}, fake_run) is False and not runs, "signing: no configuration -> unsigned, signtool not run")
        env = {"BL4FIX_SIGN_PFX": str(pfx), "BL4FIX_SIGN_PFX_PASSWORD": "hunter2", "BL4FIX_SIGNTOOL": str(tool)}
        check(build.sign_exe(exe, env, fake_run) is True and len(runs) == 2, "signing: certificate file -> sign + verify")
        check(runs[0][:9] == [str(tool), "sign", "/fd", "SHA256", "/tr", build.TIMESTAMP_PFX, "/td", "SHA256", "/f"]
              and runs[0][-3:] == ["/p", "hunter2", str(exe)] and runs[1] == [str(tool), "verify", "/pa", str(exe)],
              f"signing: pfx command line {runs[0][1:]}")
        check("hunter2" not in build._shown(runs[0]), "signing: password masked in the build log")  # noqa: SLF001
        runs.clear()
        env = {"BL4FIX_SIGN_DLIB": str(dlib), "BL4FIX_SIGN_METADATA": str(meta), "BL4FIX_SIGNTOOL": str(tool)}
        build.sign_exe(exe, env, fake_run)
        check(runs[0][-5:] == ["/dlib", str(dlib), "/dmdf", str(meta), str(exe)] and build.TIMESTAMP_AZURE in runs[0],
              f"signing: Azure Artifact Signing command line {runs[0][1:]}")
        for bad_env, what in (
            ({"BL4FIX_SIGN_DLIB": str(dlib), "BL4FIX_SIGNTOOL": str(tool)}, "dlib without metadata"),
            ({"BL4FIX_SIGN_PFX": str(sign_dir / "nope.pfx"), "BL4FIX_SIGNTOOL": str(tool)}, "missing pfx"),
            ({"BL4FIX_SIGN_PFX": str(pfx), "BL4FIX_SIGN_DLIB": str(dlib), "BL4FIX_SIGN_METADATA": str(meta)}, "both kinds"),
            ({"BL4FIX_SIGN_PFX": str(pfx), "BL4FIX_SIGNTOOL": str(sign_dir / "none.exe")}, "no signtool"),
        ):
            try:
                build.sign_exe(exe, bad_env, fake_run)
                check(False, f"signing: {what} should fail the build")
            except SystemExit:
                check(True, f"signing: {what} fails the build")
        try:
            build.sign_exe(exe, {"BL4FIX_SIGN_PFX": str(pfx), "BL4FIX_SIGNTOOL": str(tool)}, lambda cmd, check=False: fake_run(cmd, code=1))
            check(False, "signing: a signtool error should fail the build")
        except SystemExit:
            check(True, "signing: a signtool error fails the build")
        kits = sign_dir / "kits"
        for version in ("10.0.19041.0", "10.0.22621.0", "wdf"):
            (kits / version / "x64").mkdir(parents=True)
            (kits / version / "x64" / "signtool.exe").write_bytes(b"x")
        which = build.shutil.which
        build.shutil.which = lambda name: None
        try:
            found = build.find_signtool({}, kits)
        finally:
            build.shutil.which = which
        check(found == kits / "10.0.22621.0" / "x64" / "signtool.exe", f"signing: newest Windows SDK signtool found {found}")
        digest = build.write_sha256(exe)
        import hashlib  # noqa: PLC0415
        check(digest == hashlib.sha256(b"MZ setup").hexdigest()
              and (sign_dir / "BL4FixSetup.exe.sha256").read_text() == f"{digest} *BL4FixSetup.exe\n",
              "signing: SHA-256 file written next to the exe")

        # --- foreign dsound.dll (another mod loader) blocks install ---
        game2 = fake_game(tmp / "conflict")
        (game2 / core.WIN64_REL / "dsound.dll").write_bytes(b"someone else's loader")
        try:
            core.install(game2, log)
            check(False, "conflict: install should refuse a foreign dsound.dll")
        except RuntimeError as ex:
            check("dsound.dll" in str(ex), "conflict: install refuses a foreign dsound.dll")

        # --- not a game folder ---
        try:
            core.install(tmp / "nothing", log)
            check(False, "bad folder: install should refuse")
        except RuntimeError:
            check(True, "bad folder: install refuses a non-BL4 folder")

        # --- game running ---
        core.game_running = lambda: True
        try:
            core.install(game, log)
            check(False, "running: install should refuse while BL4 runs")
        except RuntimeError as ex:
            check("Close Borderlands 4" in str(ex), "running: install refuses while BL4 runs")
        try:
            core.remove_mod(game, "bl4_respec", log)
            check(False, "running: remove_mod should refuse while BL4 runs")
        except RuntimeError:
            check(True, "running: remove_mod refuses while BL4 runs")
        core.game_running = lambda: False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
