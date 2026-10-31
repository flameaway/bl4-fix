"""
Offline check that every mod loads on its own: python dev/standalone_test.py [mod id ...]

Builds each catalog mod's .sdkmod the way installer/build.py does (into a temp folder), then
imports it in a fresh Python with stand-in SDK modules (unrealsdk, mods_base, ...), a fake game
folder (SETTINGS_DIR under the temp folder) and every other bl4_* package made unimportable, as
for a player who installed only that mod. Catches a hard import of a sibling mod (or anything
else that fails at import); runtime paths (hooks, on_enable) aren't run.
"""

from __future__ import annotations

import importlib.abc
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _child(zip_path: str, mod_id: str, game: str) -> None:
    class Meta(type):
        def __getattr__(cls, name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            return stub_class(name)

        def __or__(cls, other: object):
            return cls

        __ror__ = __or__

    def stub_class(name: str = "Stub") -> type:
        return Meta(name, (Stub,), {})

    class Stub:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __call__(self, *a: object, **k: object):
            if len(a) == 1 and callable(a[0]) and not k:
                return Wrapped(a[0])  # used as a decorator
            return Stub()

        def __getattr__(self, name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            return Stub()

        def __iter__(self):
            return iter(())

        def __bool__(self) -> bool:
            return False

        def __len__(self) -> int:
            return 0

        def __setitem__(self, k: object, v: object) -> None:
            pass

        def __getitem__(self, k: object):
            return Stub()

        def __or__(self, o: object):
            return self

        __ror__ = __and__ = __add__ = __or__

    class Wrapped(Stub):
        def __init__(self, fn) -> None:
            self.fn = fn

        def __call__(self, *a: object, **k: object):
            return self.fn(*a, **k)

    class StubModule(types.ModuleType):
        def __getattr__(self, name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            return stub_class(name)

    for name in ("unrealsdk", "unrealsdk.hooks", "unrealsdk.unreal", "unrealsdk.logging", "unrealsdk.commands",
                 "pyunrealsdk", "mods_base", "mods_base.options", "mods_base.keybinds", "mods_base.hook",
                 "mods_base.mod", "mods_base.mod_list", "mods_base.command", "mods_base.settings", "ui_utils"):
        module = StubModule(name)
        module.__path__ = []
        sys.modules[name] = module
    sdk = sys.modules["unrealsdk"]
    sdk.hooks, sdk.unreal, sdk.logging = sys.modules["unrealsdk.hooks"], sys.modules["unrealsdk.unreal"], sys.modules["unrealsdk.logging"]
    sdk.__version_info__ = (3, 2, 0)
    built: list[dict] = []
    base = sys.modules["mods_base"]
    base.build_mod = lambda *a, **k: built.append(k) or Stub()
    base.SETTINGS_DIR = Path(game) / "sdk_mods" / "settings"
    base.mod_list = []
    base.__version_info__ = (1, 10)
    base.ENGINE = Stub()

    class NoSiblings(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: object, target: object = None):
            top = fullname.split(".")[0]
            if top.startswith("bl4_") and top != mod_id:
                raise ModuleNotFoundError(f"No module named {top!r}", name=top)
            return None

    sys.meta_path.insert(0, NoSiblings())
    sys.path.insert(0, zip_path)
    try:
        __import__(mod_id)
    except BaseException as ex:  # noqa: BLE001
        print("".join(traceback.format_exception(ex)))
        sys.exit(1)
    print(f"build_mod calls: {len(built)}")


def main() -> None:
    sys.path.insert(0, str(REPO / "installer"))
    import build  # noqa: PLC0415
    import catalog  # noqa: PLC0415

    only = set(sys.argv[1:])
    tmp = Path(tempfile.mkdtemp(prefix="bl4_standalone_"))
    try:
        game = tmp / "game"
        (game / "sdk_mods" / "settings").mkdir(parents=True)
        (game / "OakGame" / "Content" / "Paks" / "~mods").mkdir(parents=True)
        fails = []
        for info in catalog.MODS:
            if only and info.id not in only:
                continue
            sdkmod = tmp / f"{info.id}.sdkmod"
            build.build_sdkmod(REPO / info.source / info.id, sdkmod, catalog.license_files(info, REPO))
            run = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--child", str(sdkmod), info.id, str(game)],
                capture_output=True, text=True, timeout=120, cwd=str(tmp),
            )
            out = (run.stdout + run.stderr).strip()
            if run.returncode or "build_mod calls: 1" not in out:
                fails.append(info.id)
                print(f"FAIL {info.id}\n{out}\n")
            else:
                print(f"ok   {info.id}")
        print(f"\n{len(fails)} failed" + (f": {', '.join(fails)}" if fails else ""))
        sys.exit(1 if fails else 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        _child(*sys.argv[2:5])
    else:
        main()
