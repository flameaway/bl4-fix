"""
Offline test of shared/firston (a mod with no settings file starts on): python dev/firston_test.py

Runs ensure_on() against temp settings folders, and checks that every mod carrying the package
calls ensure_on(__name__) in its __init__.py before build_mod(). dev/standalone_test.py checks
the same through each built .sdkmod.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(("ok   " if ok else "FAIL ") + name)


def load_firston():
    spec = importlib.util.spec_from_file_location("firston_under_test", REPO / "shared" / "firston" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    firston = load_firston()
    with tempfile.TemporaryDirectory(prefix="bl4_firston_") as tmp:
        settings = Path(tmp) / "sdk_mods" / "settings"  # doesn't exist yet: a fresh install

        check("no file: writes one", firston.ensure_on("bl4_x", settings) is True)
        data = json.loads((settings / "bl4_x.json").read_text(encoding="utf-8"))
        check('the file says "enabled": true', data == {"enabled": True})
        check("no temp file left", not any(p.name.endswith(".tmp") for p in settings.iterdir()))
        check("second call does nothing", firston.ensure_on("bl4_x", settings) is False)

        off = settings / "bl4_off.json"
        off.write_text(json.dumps({"enabled": False, "options": {"a": 1}}), encoding="utf-8")
        before = off.read_bytes()
        check("switched off by the player: left alone", firston.ensure_on("bl4_off", settings) is False)
        check("switched off file unchanged", off.read_bytes() == before)

        bad = settings / "bl4_bad.json"
        bad.write_bytes(b"{not json")
        check("unreadable file: left alone", firston.ensure_on("bl4_bad", settings) is False)
        check("unreadable file unchanged", bad.read_bytes() == b"{not json")

        blocker = Path(tmp) / "afile"
        blocker.write_text("x", encoding="utf-8")
        check("settings folder can't be made: no exception, False",
              firston.ensure_on("bl4_y", blocker / "settings") is False)

        # Without a settings_dir it reads mods_base.SETTINGS_DIR (stand-in module)
        stub = type(sys)("mods_base")
        stub.SETTINGS_DIR = settings
        sys.modules["mods_base"] = stub
        try:
            check("default folder from mods_base.SETTINGS_DIR", firston.ensure_on("bl4_z") is True
                  and (settings / "bl4_z.json").is_file())
        finally:
            del sys.modules["mods_base"]
        check("no mods_base at all: no exception, False", firston.ensure_on("bl4_w") is False)

    # Every carrier calls ensure_on(__name__) before build_mod (text order in __init__.py)
    sys.path.insert(0, str(REPO / "tools"))
    import sync_shared  # noqa: PLC0415

    carriers = sync_shared.PACKAGES["firston"]
    check("Mod Menu (a Library) not a carrier", "mods/bl4_mod_menu" not in carriers)
    for rel in carriers:
        tree = ast.parse((REPO / rel / "__init__.py").read_text(encoding="utf-8"))
        call_line = build_line = None
        for node in tree.body:
            value = getattr(node, "value", None)
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
                continue
            if value.func.id == "ensure_on" and call_line is None:
                arg = value.args[0] if value.args else None
                if isinstance(arg, ast.Name) and arg.id == "__name__":
                    call_line = node.lineno
            elif value.func.id == "build_mod" and build_line is None:
                build_line = node.lineno
        check(f"{rel}: ensure_on(__name__) before build_mod",
              call_line is not None and build_line is not None and call_line < build_line)

    # Every catalog mod but the Mod Menu carries it
    sys.path.insert(0, str(REPO / "installer"))
    import catalog  # noqa: PLC0415

    wanted = {f"{info.source}/{info.id}" for info in catalog.MODS if info.id != "bl4_mod_menu"}
    missing = sorted(wanted - set(carriers))
    check("every catalog mod but the Mod Menu carries firston" + (f" (missing {missing})" if missing else ""),
          not missing)

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    os.chdir(REPO)
    sys.exit(main())
