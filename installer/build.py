"""
Builds dist/BL4FixSetup.exe (the BL4 Fix setup app).

    python installer/build.py

Steps: fetch + verify the pinned SDK release, pack every mod in catalog.py (mods/ and ported/) into
.sdkmod files, zip the mods' pictures (catalog `assets`, e.g. Kairosdex's boss and item pictures,
from assets/ in the repo: they're baked in game, tools/sync_pictures.py copies a new bake in),
then bundle everything with PyInstaller (installed into installer/.venv on first run). The setup
window's page (installer/ui/) uses the game's own menus, fonts and art, which aren't in git: when
installer/ui/game/ is missing, tools/gen_setup_ui.py takes them out of the installed game's paks
(in the venv, with pyooz for the Oodle compression; BL4_GAME_DIR overrides the game folder). So a
clone plus an installed game is enough to build.
The ported mods are GPL-3.0: each of their .sdkmod files carries ported/LICENSE.
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
# The mods' pictures (catalog `assets`), laid out like uiresources/
ASSETS = REPO / "assets"
# The installed game: the setup window's UI files come out of its paks
GAME_DIR = Path(os.environ.get("BL4_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4"))

SDK_URL = "https://github.com/bl-sdk/oak2-mod-manager/releases/download/v0.3/oak2-sdk.zip"
SDK_SHA256 = "602675446abed184169fa158be3c8bc81777a71203581e4a248eca8a3d00b5c7"
PYINSTALLER = "pyinstaller==6.11.1"
# The setup window: an HTML page on Edge WebView2 (gui.py, Tkinter, is the fallback)
PYWEBVIEW = "pywebview==5.4"
# Only for tools/gen_setup_ui.py (the exe doesn't import them): Pillow + numpy for the icon and
# Claptrap, pyooz (ooz, an open-source Oodle decoder, GPL-3.0) to read the game's paks
SETUP_UI_TOOLS = ("pillow", "numpy", "pyooz==0.0.8")


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
    Zips a mod's pictures from assets/ (made in game: Kairosdex's `dex bake all` / `dex bake
    items`, copied in by tools/sync_pictures.py). Each folder and its small/ copies: index.json plus
    the pictures it lists. Returns the picture count.
    """
    ui = ASSETS
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


def warn_unsynced_pictures() -> None:
    """A bake in this machine's game folder that assets/ doesn't have yet (the dev forgot to sync)."""
    sys.path.insert(0, str(REPO / "tools"))
    import sync_pictures  # noqa: PLC0415

    try:
        changes = sync_pictures.sync(GAME_DIR, check=True)
    except SystemExit as ex:
        changes = [str(ex)]
    if changes:
        print(f"warning: the pictures in {GAME_DIR} differ from assets/ (run tools/sync_pictures.py):")
        for line in changes:
            print(f"  {line}")


def ensure_pyinstaller() -> Path:
    python = VENV / "Scripts" / "python.exe"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-q", PYINSTALLER, PYWEBVIEW], check=True)
    return python


def main() -> None:
    version = check_versions()
    python = ensure_pyinstaller()
    # The setup window's game UI files (fonts, menus, Claptrap), taken from this machine's install
    if not all((ICON / name).is_file() for name in ("claptrap.png", "icon.ico", "icon.png")):
        print(f"installer/ui/game/ is missing: taking the game's UI files from {GAME_DIR}")
        subprocess.run([str(python), "-m", "pip", "install", "-q", *SETUP_UI_TOOLS], check=True)
        subprocess.run([str(python), str(REPO / "tools" / "gen_setup_ui.py"), "--game", str(GAME_DIR)], check=True)
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
            print(f"{info.id}: {build_assets(info, PAYLOAD / f'{info.id}.assets.zip')} pictures from assets/")
    warn_unsynced_pictures()
    shutil.copyfile(MOD_SRC / "shared.py", PAYLOAD / "shared.py")
    # The installer moves the logo movies itself too (its Skip intro switch)
    shutil.copyfile(SKIP_INTRO_SRC / "movies.py", PAYLOAD / "movies.py")
    print(f"payload ready (BL4 Fix {version}, {len(catalog.MODS)} mods)")

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
