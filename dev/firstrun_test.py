"""
Offline test of the Mod Menu's first-run switch-on (mods/bl4_mod_menu/firstrun.py): new mods of
ours with no settings file start on, mods the player switched off and other modders' mods are
left alone. Plain Python, no SDK.

    python dev/firstrun_test.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("firstrun", REPO / "mods" / "bl4_mod_menu" / "firstrun.py")
firstrun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(firstrun)  # type: ignore[union-attr]

results: list[bool] = []


def check(ok: bool, what: str) -> None:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def sdkmod(folder: Path, mod_id: str, author: str = "flame") -> None:
    with zipfile.ZipFile(folder / f"{mod_id}.sdkmod", "w") as zf:
        zf.writestr(f"{mod_id}/__init__.py", "")
        zf.writestr(f"{mod_id}/pyproject.toml", f'[project]\nname = "x"\nauthors = [{{ name = "{author}" }}]\n')


class FakeMod:
    def __init__(self, settings: Path) -> None:
        self.settings_file = settings
        self.is_enabled = False
        self.enables = 0

    def enable(self) -> None:  # like mods_base: enabling saves "enabled": true
        self.is_enabled = True
        self.enables += 1
        self.settings_file.write_text(json.dumps({"enabled": True}), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        sdk_mods = Path(tmp) / "sdk_mods"
        settings = sdk_mods / "settings"
        settings.mkdir(parents=True)
        for mod_id in ("bl4_direct", "bl4_builds", "bl4_minimap", "bl4_off"):
            sdkmod(sdk_mods, mod_id)
        sdkmod(sdk_mods, "bl4_someone_else", author="another modder")
        sdkmod(sdk_mods, "other_mod")
        (sdk_mods / "bl4_broken.sdkmod").write_bytes(b"not a zip")
        (settings / "bl4_off.json").write_text('{"enabled": false, "options": {"x": 1}}', encoding="utf-8")
        builds = FakeMod(settings / "bl4_builds.json")  # imported before the Mod Menu
        logs: list[str] = []

        done = firstrun.enable_new(sdk_mods, settings, [builds], logs.append)
        check(sorted(done) == ["bl4_builds", "bl4_direct", "bl4_minimap"], f"new mods of ours switched on: {done}")
        check(builds.enables == 1 and builds.is_enabled, "an already registered mod enabled through mod.enable()")
        check(json.loads((settings / "bl4_minimap.json").read_text()) == {"enabled": True},
              "a not yet imported mod gets enabled: true written")
        check(json.loads((settings / "bl4_off.json").read_text()) == {"enabled": False, "options": {"x": 1}},
              "a mod the player switched off is untouched")
        check(not (settings / "bl4_someone_else.json").exists(), "another modder's bl4_ mod untouched")
        check(not (settings / "other_mod.json").exists(), "non-bl4 mod untouched")
        check(not (settings / "bl4_broken.json").exists(), "an unreadable .sdkmod skipped")
        check(any("switched on 3" in line for line in logs), "logged once")

        again = firstrun.enable_new(sdk_mods, settings, [builds], logs.append)
        check(again == [] and builds.enables == 1, "a second start does nothing")

        class Exploding(FakeMod):
            def enable(self) -> None:
                raise RuntimeError("boom")

        sdkmod(sdk_mods, "bl4_bad")
        sdkmod(sdk_mods, "bl4_good")
        done = firstrun.enable_new(sdk_mods, settings, [Exploding(settings / "bl4_bad.json")], logs.append)
        check(done == ["bl4_good"] and any("bl4_bad" in line and "boom" in line for line in logs),
              "one mod's failure doesn't stop the others")
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
