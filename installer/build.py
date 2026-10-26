"""
Builds dist/BL4FixSetup.exe (the BL4 Fix setup app).

    python installer/build.py

Steps: fetch + verify the pinned SDK release, pack every mod in catalog.py (mods/ and ported/) into
.sdkmod files, zip the mods' pictures (catalog `assets`, e.g. Weapondex's boss and item pictures,
taken from this machine's game folder: BL4_GAME_DIR overrides it), then bundle everything with
PyInstaller (installed into installer/.venv on first run). The setup window's page (installer/ui/)
uses the game's own menus, fonts and art: tools/gen_setup_ui.py copies them into installer/ui/game/
first (dev machine only, not in git). The ported mods are GPL-3.0: each of
their .sdkmod files carries ported/LICENSE.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import catalog  # noqa: E402

MOD_SRC = REPO / "mods" / "bl4_direct"
SKIP_INTRO_SRC = REPO / "mods" / "bl4_skip_intro"
BUILD = HERE / "build"
PAYLOAD = BUILD / "payload"
CACHE = BUILD / "cache"
VENV = HERE / ".venv"
ICON = HERE / "ui" / "game" / "_setup"
# Where the mods' pictures are taken from (catalog `assets`): the dev's own install
GAME_DIR = Path(os.environ.get("BL4_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4"))

SDK_URL = "https://github.com/bl-sdk/oak2-mod-manager/releases/download/v0.3/oak2-sdk.zip"
SDK_SHA256 = "602675446abed184169fa158be3c8bc81777a71203581e4a248eca8a3d00b5c7"
PYINSTALLER = "pyinstaller==6.11.1"
# The setup window: an HTML page on Edge WebView2 (gui.py, Tkinter, is the fallback)
PYWEBVIEW = "pywebview==5.4"


def fetch_sdk() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE / "oak2-sdk-v0.3.zip"
    if not zip_path.exists():
        print(f"downloading {SDK_URL}")
        urllib.request.urlretrieve(SDK_URL, zip_path)  # noqa: S310
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != SDK_SHA256:
        zip_path.unlink()
        raise SystemExit(f"SDK zip hash mismatch: {digest}")
    return zip_path


def check_versions() -> str:
    shared_src = (MOD_SRC / "shared.py").read_text(encoding="utf-8")
    mod_version = re.search(r'MOD_VERSION = "([^"]+)"', shared_src).group(1)  # type: ignore[union-attr]
    pyproject = (MOD_SRC / "pyproject.toml").read_text(encoding="utf-8")
    py_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)  # type: ignore[union-attr]
    if mod_version != py_version:
        raise SystemExit(f"version mismatch: shared.py {mod_version} vs pyproject.toml {py_version}")
    return mod_version


# Fixed zip timestamps, so the same files always give the same bytes: tools/release.py compares
# hashes with the last release to spot a mod that changed without a version bump
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def build_sdkmod(src: Path, out: Path, extra: dict[str, Path] | None = None) -> None:
    files = {
        (Path(src.name) / path.relative_to(src)).as_posix(): path
        for path in src.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.name != "CLAUDE.md"
    }
    files |= {(Path(src.name) / name).as_posix(): path for name, path in (extra or {}).items()}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name].read_bytes())


def build_assets(info: catalog.ModInfo, out: Path) -> int:
    """
    Zips a mod's loose pictures from this machine's game folder (they're made in game: Weapondex's
    `dex bake all` / `dex bake items`). Each folder and its small/ copies: index.json plus the
    pictures it lists, nothing else (raw/ and trials/ are bake scratch). Returns the picture count.
    """
    ui = GAME_DIR / "OakGame/Content/uiresources"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:  # PNGs don't compress further
        for rel in info.assets:
            for sub in (rel, f"{rel}/small"):
                index_path = ui / sub / "index.json"
                try:
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as ex:
                    raise SystemExit(f"{info.id}: no picture index at {index_path} ({ex})") from None
                if not index:
                    raise SystemExit(f"{info.id}: {index_path} lists no pictures")
                if missing := [key for key in index if not (ui / sub / f"{key}.png").is_file()]:
                    raise SystemExit(f"{info.id}: {len(missing)} pictures in {index_path} are missing, e.g. {missing[0]}")
                for name in [*sorted(f"{key}.png" for key in index), "index.json"]:
                    zinfo = zipfile.ZipInfo(f"{sub}/{name}", ZIP_DATE)
                    zinfo.external_attr = 0o644 << 16
                    zf.writestr(zinfo, (ui / sub / name).read_bytes())
                count += len(index)
    return count


def ensure_pyinstaller() -> Path:
    python = VENV / "Scripts" / "python.exe"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-q", PYINSTALLER, PYWEBVIEW], check=True)
    return python


def main() -> None:
    version = check_versions()
    # The setup window's game UI files (fonts, menus, Claptrap), taken from this machine's install
    if not all((ICON / name).is_file() for name in ("claptrap.png", "icon.ico", "icon.png")):
        raise SystemExit("installer/ui/game/ is missing: run python tools/gen_setup_ui.py --oodle <oo2core_*_win64.dll>")
    shutil.rmtree(PAYLOAD, ignore_errors=True)
    PAYLOAD.mkdir(parents=True)
    shutil.copyfile(fetch_sdk(), PAYLOAD / "oak2-sdk.zip")
    for info in catalog.MODS:
        src = REPO / info.source / info.id
        if not (src / "__init__.py").is_file():
            raise SystemExit(f"{src} isn't a mod folder")
        extra = {"LICENSE": REPO / "ported" / "LICENSE"} if info.source == "ported" else None
        build_sdkmod(src, PAYLOAD / f"{info.id}.sdkmod", extra)
        if info.assets:
            print(f"{info.id}: {build_assets(info, PAYLOAD / f'{info.id}.assets.zip')} pictures from {GAME_DIR}")
    shutil.copyfile(MOD_SRC / "shared.py", PAYLOAD / "shared.py")
    # The installer moves the logo movies itself too (its Skip intro switch)
    shutil.copyfile(SKIP_INTRO_SRC / "movies.py", PAYLOAD / "movies.py")
    print(f"payload ready (BL4 Fix {version}, {len(catalog.MODS)} mods)")

    python = ensure_pyinstaller()
    subprocess.run(
        [
            str(python), "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--windowed", "--uac-admin",
            "--name", "BL4FixSetup",
            "--distpath", str(REPO / "dist"),
            "--workpath", str(BUILD / "pyinstaller"),
            "--specpath", str(BUILD),
            "--add-data", f"{PAYLOAD};payload",
            # The game's vault symbol (tools/gen_setup_ui.py), for the exe, its windows and shortcut
            "--icon", str(ICON / "icon.ico"),
            "--add-data", f"{ICON / 'icon.ico'};.",
            "--add-data", f"{ICON / 'icon.png'};.",
            "--add-data", f"{HERE / 'ui'};ui",
            "--paths", str(HERE),
            "--paths", str(PAYLOAD),
            "--hidden-import", "shared",
            "--hidden-import", "movies",
            "--hidden-import", "catalog",
            "--hidden-import", "webgui",
            "--hidden-import", "gui",
            # What the mods' page modules import when core.prepare_pages runs them from the
            # .sdkmod (PyInstaller only bundles what the app itself imports)
            "--hidden-import", "importlib.resources",
            "--hidden-import", "dataclasses",
            "--hidden-import", "zlib",
            "--hidden-import", "struct",
            "--hidden-import", "hashlib",
            "--hidden-import", "threading",
            str(HERE / "main.py"),
        ],
        check=True,
        cwd=HERE,
    )
    print(f"built {REPO / 'dist' / 'BL4FixSetup.exe'}")


if __name__ == "__main__":
    main()
