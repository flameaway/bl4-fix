"""
Offline checks of shared/health (no game): python dev/health_test.py

Stand-in SDK modules, fake mods (Mod-like objects with the fields the mod lists read), a fake
sdk_mods folder with a .sdkmod that "failed to import", and a fake notice.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import types
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "shared" / "health"
FAILS: list[str] = []


def ok(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


@dataclass(eq=False)
class FakeMod:
    name: str
    author: str = ""
    description: str = ""
    version: str = ""
    settings_file: Path | None = None
    keybinds: list = field(default_factory=list)
    options: list = field(default_factory=list)
    hooks: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    auto_enable: bool = True
    is_enabled: bool = True
    enabling_locked: bool = False
    saved: list = field(default_factory=list)

    def get_status(self) -> str:
        return "Enabled" if self.is_enabled else "Disabled"

    def enable(self) -> None:
        if self.enabling_locked or self.is_enabled:
            return
        self.is_enabled = True
        self.saved.append("enable")

    def disable(self, dont_update_setting: bool = False) -> None:
        if self.enabling_locked or not self.is_enabled:
            return
        self.is_enabled = False
        if not dont_update_setting:
            self.saved.append("disable")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="health_test_"))
    try:
        game = tmp / "game"
        sdk_mods = game / "sdk_mods"
        (sdk_mods / "settings").mkdir(parents=True)
        logged: list[str] = []
        sdk = types.ModuleType("unrealsdk")
        log = types.ModuleType("unrealsdk.logging")
        for n in ("info", "error", "warning", "dev_warning"):
            setattr(log, n, lambda msg, _n=n: logged.append(f"{_n}: {msg}"))
        sdk.logging = log
        sys.modules["unrealsdk"], sys.modules["unrealsdk.logging"] = sdk, log
        registered: list = []
        mb = types.ModuleType("mods_base")
        mb.SETTINGS_DIR = sdk_mods / "settings"
        mb.Mod = FakeMod
        pc = types.SimpleNamespace(Pawn=object())
        mb.get_pc = lambda possibly_loading=False: pc
        ml = types.ModuleType("mods_base.mod_list")
        ml.mod_list = registered
        ml.register_mod = registered.append
        sys.modules["mods_base"], sys.modules["mods_base.mod_list"] = mb, ml

        folder = tmp / "x" / "health"
        shutil.copytree(SOURCE, folder, ignore=shutil.ignore_patterns("__pycache__"))
        spec = importlib.util.spec_from_file_location("x_health", folder / "__init__.py", submodule_search_locations=[str(folder)])
        health = importlib.util.module_from_spec(spec)
        sys.modules["x_health"] = health
        spec.loader.exec_module(health)
        shown: list = []
        fake_notice = types.ModuleType("x_health.notice")
        fake_notice.show = lambda pc_, title, details: shown.append((title, details))
        fake_notice.tick = lambda: None
        sys.modules["x_health.notice"] = fake_notice
        hub = health._hub()  # noqa: SLF001
        clock = [1000.0]
        health.time = types.SimpleNamespace(monotonic=lambda: clock[0])

        def run(seconds: float) -> None:
            end = clock[0] + seconds
            while clock[0] < end:
                clock[0] += 0.3
                health.tick()

        # A whole-mod probe: Reroll's item data no longer matches the game
        reroll = types.ModuleType("bl4_reroll")
        reroll.mod = FakeMod("Reroll", description="Reroll stats.")
        reroll._data_ok = None  # still checking
        sys.modules["bl4_reroll"] = reroll
        # A partial one: Build Presets' equip code not found, reported by the mod itself
        builds = types.ModuleType("bl4_builds")
        builds.mod = FakeMod("Build Presets", description="Builds.")
        sys.modules["bl4_builds"] = builds

        run(3)
        ok(reroll.mod.get_status() == "Enabled", "still checking (None): nothing shown")
        reroll._data_ok = False
        run(3)
        ok("Needs update" in reroll.mod.get_status(), "stale data: status Needs update")
        ok(reroll.mod.description.startswith("<font") and "item data changed" in reroll.mod.description and reroll.mod.description.endswith("Reroll stats."), "reason above the description")
        ok(not reroll.mod.is_enabled and reroll.mod.enabling_locked, "whole problem: off this session, switch locked")
        ok(reroll.mod.saved == [], "its saved setting untouched")

        health.report(builds.mod, "equip", "loading gear is off")
        run(3)
        ok("Needs update" in builds.mod.get_status() and builds.mod.is_enabled and not builds.mod.enabling_locked, "partial report: status, still on")
        ok(set(health.problems()) == {"bl4_reroll", "bl4_builds"}, "problems() lists both")

        # A failed import: a .sdkmod of ours not in sys.modules; another modder's is left alone
        for package, author in (("bl4_broken", "flame"), ("bl4_someone", "someone")):
            with zipfile.ZipFile(sdk_mods / f"{package}.sdkmod", "w") as zf:
                zf.writestr(f"{package}/pyproject.toml", f'[project]\nname = "BL4 {package}"\nversion = "1.2.3"\nauthors = [{{ name = "{author}" }}]\n')
        run(health.SCAN_AFTER_S)
        for _ in range(50):
            if hub.scan is True:
                break
            import time as real_time  # noqa: PLC0415

            real_time.sleep(0.02)
            run(0.3)
        names = [m.name for m in registered]
        ok(names == ["BL4 bl4_broken"], f"stand-in only for our failed mod ({names})")
        standin = registered[0]
        ok(standin.enabling_locked and "Needs update" in standin.get_status() and standin.settings_file is None, "stand-in: locked, Needs update, no settings file")

        # The notice: once per game version and set of problems
        run(health.NOTICE_AFTER_S)
        ok(len(shown) == 1 and "3 mods need an update" in shown[0][0], f"one notice ({shown})")
        ok("Reroll" in shown[0][1][0] and "Build Presets" in shown[0][1][0], "it names the mods")
        settings = json.loads((sdk_mods / "settings" / health.SETTINGS_NAME).read_text())
        ok(len(settings["noticed"]["problems"]) == 3, "what it showed is remembered")
        run(10)
        ok(len(shown) == 1, "not shown again this session")
        hub.noticed = False
        run(1)
        ok(len(shown) == 1, "nor for the same problems in a later session")

        # Fixed: back to normal, turned on again as saved
        reroll._data_ok = True
        health.clear(builds.mod, "equip")
        run(3)
        ok(reroll.mod.get_status() == "Enabled" and reroll.mod.description == "Reroll stats.", "Reroll restored")
        ok(reroll.mod.is_enabled and not reroll.mod.enabling_locked, "Reroll on again, switch unlocked")
        ok(builds.mod.get_status() == "Enabled" and builds.mod.description == "Builds.", "Build Presets restored")
        ok(reroll.mod.saved == ["enable"] or reroll.mod.saved == [], f"settings writes: {reroll.mod.saved}")

        # A probe that raises (another version of that mod) says nothing
        health.PROBES["bl4_reroll"].append(("boom", False, lambda: 1 / 0))
        run(3)
        ok(reroll.mod.get_status() == "Enabled", "a probe that raises is ignored")
        errors = [m for m in logged if m.startswith("error")]
        ok(not errors, f"no errors logged {errors[:2]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
