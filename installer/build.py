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
Every .sdkmod carries the GPL-3.0 text (LICENSE), and LICENSE-AGPL for mods with AGPL-3.0 files
(catalog.license_files).
Last, the exe is signed with signtool when BL4FIX_SIGN_* environment variables configure a signer
(see "exe signing" below; unsigned otherwise), and its SHA-256 goes to dist/BL4FixSetup.exe.sha256.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Mapping
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




def _local_only() -> frozenset[str]:
    """File names this clone keeps out of git (.git/info/exclude): local files never ship."""
    try:
        lines = (REPO / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(s for line in lines if (s := line.strip()) and not s.startswith("#") and not set("/*?[") & set(s))


LOCAL_ONLY = _local_only()
# Fixed zip timestamps, so the same files always give the same bytes: tools/release.py compares
# hashes with the last release to spot a mod that changed without a version bump
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _raw_patterns() -> tuple[str, ...]:
    """The .gitattributes patterns git stores byte for byte (`-text`), as fnmatch patterns."""
    try:
        lines = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    return tuple(
        parts[0].replace("**", "*") for line in lines
        if (parts := line.split()) and not parts[0].startswith("#") and ("-text" in parts[1:] or "binary" in parts[1:])
    )  # fmt: skip


RAW_PATTERNS = _raw_patterns()


def file_bytes(path: Path) -> bytes:
    """
    A file's bytes as git stores them, so the same commit gives the same .sdkmod on any machine
    (a Windows checkout writes CRLF, this tree has LF and CRLF files mixed, and the GitHub Actions
    rebuild of a release must match it byte for byte): text files with LF line endings; files
    marked `-text` in .gitattributes (shared packages, pictures) and binary ones (a NUL byte, git's
    own test) as they are.
    """
    data = path.read_bytes()
    if b"\r\n" not in data or b"\0" in data:
        return data
    try:
        rel = path.resolve().relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return data
    if any(fnmatch.fnmatchcase(rel, pattern) for pattern in RAW_PATTERNS):
        return data
    return data.replace(b"\r\n", b"\n")


def build_sdkmod(src: Path, out: Path, extra: dict[str, Path] | None = None) -> None:
    files = {
        (Path(src.name) / path.relative_to(src)).as_posix(): path
        for path in src.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.name not in LOCAL_ONLY
    }
    files |= {(Path(src.name) / name).as_posix(): path for name, path in (extra or {}).items()}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, file_bytes(files[name]))


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


def check_picture_lists() -> None:
    """
    Each mod with pictures carries pictures.json (what assets/ holds), which it compares with the
    game folder to download missing pictures from the release (Kairosdex fetch.py). A stale list
    would fetch for nothing or miss new pictures.
    """
    sys.path.insert(0, str(REPO / "tools"))
    import sync_pictures  # noqa: PLC0415

    if stale := sync_pictures.stale_lists():
        raise SystemExit(f"pictures.json doesn't match assets/ for {', '.join(stale)}: run tools/sync_pictures.py")


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


# PyInstaller's own bootloader (the stub that unpacks and starts the app) is one binary shared by
# thousands of programs, malware too, so antivirus programs flag it on sight. One compiled here
# (from PyInstaller's sources, with Visual Studio's C++ tools) is ours alone: far fewer false
# positives. Recorded in the venv so it's compiled once per PyInstaller version.
OWN_BOOTLOADER_MARK = VENV / "bl4fix_own_bootloader.txt"


def ensure_pyinstaller() -> Path:
    python = VENV / "Scripts" / "python.exe"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-q", PYINSTALLER, PYWEBVIEW], check=True)
    try:
        own = OWN_BOOTLOADER_MARK.read_text(encoding="utf-8").strip() == PYINSTALLER
    except OSError:
        own = False
    if not own:
        print(f"compiling PyInstaller's bootloader from source ({PYINSTALLER}, once)")
        result = subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", "--no-cache-dir",
             "--no-binary", "pyinstaller", PYINSTALLER],
            env={**os.environ, "PYINSTALLER_COMPILE_BOOTLOADER": "1"}, check=False,
        )  # fmt: skip
        if result.returncode == 0:
            OWN_BOOTLOADER_MARK.write_text(PYINSTALLER + "\n", encoding="utf-8")
        else:
            # Still a working exe, just the stock bootloader antivirus programs know
            print("warning: couldn't compile the bootloader (needs Visual Studio's C++ build tools): stock one used")
            subprocess.run([str(python), "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", PYINSTALLER], check=True)
    return python


def version_file(version: str) -> Path:
    """
    The exe's Windows version resource (Explorer > Properties > Details; antivirus heuristics look
    for one too): product, description, version, license.
    """
    numbers = [int(n) for n in re.findall(r"\d+", version)[:4]]
    numbers += [0] * (4 - len(numbers))
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(numbers)}, prodvers={tuple(numbers)}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'BL4 Fix (open-source mods, github.com/flameaway/bl4-fix)'),
      StringStruct('FileDescription', 'BL4 Fix Setup: installs Borderlands 4 mods'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'BL4FixSetup'),
      StringStruct('LegalCopyright', 'GPL-3.0. Not affiliated with Gearbox or 2K.'),
      StringStruct('OriginalFilename', 'BL4FixSetup.exe'),
      StringStruct('ProductName', 'BL4 Fix'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = BUILD / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def check_shared() -> None:
    """Every mod's copy of a shared package (shared/uilink, shared/health) matches the source."""
    result = subprocess.run([sys.executable, str(REPO / "tools" / "sync_shared.py"), "--check"], check=False)
    if result.returncode != 0:
        raise SystemExit("a mod's copy of a shared package is stale: run python tools/sync_shared.py")


def main() -> None:
    check_shared()
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
    check_picture_lists()
    for info in catalog.MODS:
        src = REPO / info.source / info.id
        if not (src / "__init__.py").is_file():
            raise SystemExit(f"{src} isn't a mod folder")
        build_sdkmod(src, PAYLOAD / f"{info.id}.sdkmod", catalog.license_files(info, REPO))
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
            "--noconfirm", "--clean", "--onefile", "--windowed",
            # UPX-packed exes look like malware packers to antivirus programs
            "--noupx",
            "--version-file", str(version_file(version)),
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
            "--hidden-import", "selfupdate",
            "--hidden-import", "signing",
            "--hidden-import", "slim",
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
    exe = REPO / "dist" / "BL4FixSetup.exe"
    print(f"built {exe}")
    sign_exe(exe)
    print(f"SHA-256 {write_sha256(exe)} ({exe.name}.sha256)")


# region exe signing (optional)
#
# Configured by environment variables, never by files in the repo:
#   BL4FIX_SIGN_PFX (+ BL4FIX_SIGN_PFX_PASSWORD): a code signing certificate file
#   BL4FIX_SIGN_DLIB + BL4FIX_SIGN_METADATA: Azure Artifact Signing (the dlib
#     Azure.CodeSigning.Dlib.dll and its metadata.json: endpoint, account, certificate profile)
#   BL4FIX_SIGN_TIMESTAMP: the RFC 3161 timestamp server (default: the signer's usual one)
#   BL4FIX_SIGNTOOL: signtool.exe (default: PATH, else the newest Windows 10/11 SDK)
# Without any, the exe is built unsigned. A configured signing that fails fails the build.

WINDOWS_KITS = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
TIMESTAMP_PFX = "http://timestamp.digicert.com"
TIMESTAMP_AZURE = "http://timestamp.acs.microsoft.com"


def sign_config(env: Mapping[str, str]) -> dict | None:
    """The signing setup the environment asks for (None: unsigned). Raises SystemExit on a half one."""
    pfx, dlib, metadata = (env.get(k, "").strip() for k in ("BL4FIX_SIGN_PFX", "BL4FIX_SIGN_DLIB", "BL4FIX_SIGN_METADATA"))
    if pfx and (dlib or metadata):
        raise SystemExit("signing: set BL4FIX_SIGN_PFX or BL4FIX_SIGN_DLIB + BL4FIX_SIGN_METADATA, not both")
    if pfx:
        if not Path(pfx).is_file():
            raise SystemExit(f"signing: BL4FIX_SIGN_PFX {pfx} doesn't exist")
        return {"kind": "pfx", "pfx": pfx, "password": env.get("BL4FIX_SIGN_PFX_PASSWORD", ""),
                "timestamp": env.get("BL4FIX_SIGN_TIMESTAMP", "").strip() or TIMESTAMP_PFX}
    if dlib or metadata:
        if not (dlib and metadata):
            raise SystemExit("signing: Azure Artifact Signing needs both BL4FIX_SIGN_DLIB and BL4FIX_SIGN_METADATA")
        for name, path in (("BL4FIX_SIGN_DLIB", dlib), ("BL4FIX_SIGN_METADATA", metadata)):
            if not Path(path).is_file():
                raise SystemExit(f"signing: {name} {path} doesn't exist")
        return {"kind": "azure", "dlib": dlib, "metadata": metadata,
                "timestamp": env.get("BL4FIX_SIGN_TIMESTAMP", "").strip() or TIMESTAMP_AZURE}
    return None


def _version_tuple(name: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in name.split("."))
    except ValueError:
        return None


def find_signtool(env: Mapping[str, str] = os.environ, kits: Path = WINDOWS_KITS) -> Path | None:
    """BL4FIX_SIGNTOOL, signtool in PATH, or the newest Windows SDK's x64 signtool."""
    if explicit := env.get("BL4FIX_SIGNTOOL", "").strip():
        return Path(explicit) if Path(explicit).is_file() else None
    if found := shutil.which("signtool"):
        return Path(found)
    candidates = []
    for tool in kits.glob("*/x64/signtool.exe"):
        if (version := _version_tuple(tool.parent.parent.name)) is not None:
            candidates.append((version, tool))
    return max(candidates)[1] if candidates else None


def sign_commands(signtool: Path, exe: Path, config: dict) -> list[list[str]]:
    """The signtool runs: sign (SHA-256, RFC 3161 timestamp), then verify against the Authenticode policy."""
    sign = [str(signtool), "sign", "/fd", "SHA256", "/tr", config["timestamp"], "/td", "SHA256"]
    if config["kind"] == "pfx":
        sign += ["/f", config["pfx"]]
        if config["password"]:
            sign += ["/p", config["password"]]
    else:
        sign += ["/dlib", config["dlib"], "/dmdf", config["metadata"]]
    return [[*sign, str(exe)], [str(signtool), "verify", "/pa", str(exe)]]


def _shown(cmd: list[str]) -> str:
    """A command line for the build log, with the certificate password masked."""
    return subprocess.list2cmdline(["***" if i and cmd[i - 1] == "/p" else part for i, part in enumerate(cmd)])


def sign_exe(exe: Path, env: Mapping[str, str] = os.environ, run=subprocess.run) -> bool:  # noqa: ANN001
    """Signs and verifies the exe when the environment configures a signer. True when signed."""
    config = sign_config(env)
    if config is None:
        print(f"{exe.name} is unsigned (no BL4FIX_SIGN_PFX or BL4FIX_SIGN_DLIB + BL4FIX_SIGN_METADATA set)")
        return False
    signtool = find_signtool(env)
    if signtool is None:
        raise SystemExit("signing: signtool.exe not found (install the Windows SDK, or set BL4FIX_SIGNTOOL)")
    for cmd in sign_commands(signtool, exe, config):
        print(f"signing: {_shown(cmd)}")
        if run(cmd, check=False).returncode != 0:
            raise SystemExit(f"signing failed: {cmd[1]} returned an error (see above)")
    print(f"signed {exe.name} ({'certificate file' if config['kind'] == 'pfx' else 'Azure Artifact Signing'})")
    return True


def write_sha256(exe: Path) -> str:
    """dist/BL4FixSetup.exe.sha256, in sha256sum's format (after signing: the signature is part of the file)."""
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    out = exe.with_name(exe.name + ".sha256")
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(f"{digest} *{exe.name}\n", encoding="utf-8")
    tmp.replace(out)
    return digest


# endregion


if __name__ == "__main__":
    main()
