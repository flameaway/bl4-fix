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
core.firewall.apply_rule = lambda port, ips, lan: firewall_calls.append(("apply", port, list(ips), lan)) or "ok"
core.firewall.remove_rule = lambda: firewall_calls.append(("remove",)) or "ok"
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

        # --- not admin: only the firewall step asks for it (an elevated copy of the app) ---
        core.is_admin = lambda: False
        calls = len(firewall_calls)
        core.install(game, log)
        check(elevated_calls[-1:] == [["--apply-firewall", "--game", str(game)]] and len(firewall_calls) == calls,
              f"not admin: firewall rule through the elevated app {elevated_calls[-1:]}")
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
