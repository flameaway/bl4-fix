"""
Install / status / uninstall logic for BL4 Fix, shared by the GUI and the CLI.

Payload files (the pinned SDK zip and the built .sdkmod files) are bundled next to this module by the
build script, under `payload/`.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import winreg
import zipfile
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import catalog

if getattr(sys, "frozen", False):
    PAYLOAD = Path(sys._MEIPASS) / "payload"  # type: ignore[attr-defined]  # noqa: SLF001
else:
    PAYLOAD = Path(__file__).resolve().parent / "build" / "payload"

sys.path.insert(0, str(PAYLOAD))
import movies  # noqa: E402  # bundled copy of mods/bl4_skip_intro/movies.py
import shared  # noqa: E402  # bundled copy of mods/bl4_direct/shared.py
import firewall  # noqa: E402  # builds its rule with shared

SDK_ZIP = PAYLOAD / "oak2-sdk.zip"
SDK_ZIP_SHA256 = "602675446abed184169fa158be3c8bc81777a71203581e4a248eca8a3d00b5c7"
MOD_FILE = PAYLOAD / "bl4_direct.sdkmod"
# Skip Intro: the 2K / Gearbox logo movies moved aside. Its checkbox is the mod's enabled flag
# (which the in-game mod follows too, so it re-applies after a Steam file check)
SKIP_INTRO = "bl4_skip_intro"
# Where mods put the shim paks and loose UI files they write themselves (catalog.ModInfo)
MOD_PAKS_REL = Path("OakGame/Content/Paks/~mods")
UI_REL = Path("OakGame/Content/uiresources")
# A Start menu entry for this app (the exe the player ran), to add or remove mods later
SHORTCUT_NAME = "BL4 Fix Setup.lnk"
# What the shortcut was called before the project was BL4 Fix (2026-10-25): removed on install
OLD_SHORTCUT_NAMES = ("BL4 Mods Setup.lnk",)

# The SDK is built with a recent MSVC; older runtimes fail to load its DLLs
VCREDIST_MIN = (14, 44)
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

GAME_EXE_REL = Path("OakGame/Binaries/Win64/Borderlands4.exe")
WIN64_REL = Path("OakGame/Binaries/Win64")
SDK_MARKER_REL = WIN64_REL / "Plugins/unrealsdk.toml"
INSTALL_RECORD = "bl4_direct_install.json"
# Where setup apps before 2026-10-28 kept a ~100 MB copy of themselves (dot-folders in sdk_mods
# aren't loaded as mods), for BL4 Direct Connect's firewall button and the Start menu. Removed on
# install (the mod updates the firewall itself now; the Mod Menu removes it too)
TOOLS_DIR_REL = Path("sdk_mods/.bl4_direct")
OLD_COPY_NAME = "BL4DirectSetup.exe"
# Where the app moves itself when the player started it from that old copy (their only one)
APP_HOME_REL = Path("Programs/BL4 Fix/BL4FixSetup.exe")  # in %LOCALAPPDATA%


def payload_mod(mod_id: str) -> Path:
    return PAYLOAD / f"{mod_id}.sdkmod"


def payload_assets(mod_id: str) -> Path:
    return PAYLOAD / f"{mod_id}.assets.zip"


def _version_key(version: str | None) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version or "")[:4])


def sdkmod_version(path: Path, mod_id: str) -> str | None:
    """The version in <id>/pyproject.toml inside an .sdkmod (None if missing or unreadable)."""
    try:
        with zipfile.ZipFile(path) as zf:
            text = zf.read(f"{mod_id}/pyproject.toml").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else None


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


# region Admin on demand
#
# The app runs as the player (no UAC prompt to start it). Only a few steps need admin: the firewall
# rule (netsh itself elevated, so the prompt names Windows' firewall tool: firewall.py), the VC++
# runtime, and a game folder the player can't write to (Steam's own folders let every user write).
# The last runs in an elevated copy of this app (its CLI), behind one UAC prompt.

# Set by main.py in an elevated copy: never ask again from there (no prompt loops)
NO_ELEVATE = False


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    )  # fmt: skip


# Own instances, so these prototypes don't change the shared ctypes.windll ones
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
_shell32.ShellExecuteExW.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def run_as_admin(exe: str, params: str, on_wait: Callable[[], None] | None = None) -> int:
    """Runs a program elevated (UAC prompt), hidden, and waits for it. Returns its exit code."""
    if NO_ELEVATE:
        raise RuntimeError("this step needs admin rights, but this copy of the app isn't elevated")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.nShow = 0  # SW_HIDE
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:  # ERROR_CANCELLED: the player said no
            raise RuntimeError("Windows didn't get permission (the admin prompt was declined).")
        raise ctypes.WinError(error)
    code = wintypes.DWORD()
    try:
        while _kernel32.WaitForSingleObject(info.hProcess, 200) == 0x102:  # WAIT_TIMEOUT
            if on_wait:
                on_wait()
        _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    finally:
        _kernel32.CloseHandle(info.hProcess)
    if on_wait:
        on_wait()
    return code.value


def _run_self_elevated(args: list[str], log=print) -> None:  # noqa: ANN001
    """Runs this app's CLI elevated and relays its output to `log`. Raises its error, if any."""
    if getattr(sys, "frozen", False):
        exe, argv = sys.executable, list(args)
    else:
        exe, argv = sys.executable, [str(Path(__file__).with_name("main.py")), *args]
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "elevated.log"
        argv += ["--log-file", str(out)]
        seen = 0

        def relay() -> None:
            nonlocal seen
            try:
                new = out.read_text(encoding="utf-8")[seen:]
            except OSError:
                return
            cut = new.rfind("\n")
            if cut < 0:
                return
            seen += cut + 1
            for line in new[:cut].splitlines():
                if line.strip():
                    lines.append(line)
                    log(line)

        code = run_as_admin(exe, subprocess.list2cmdline(argv), relay)
    if code:
        error = next((ln for ln in reversed(lines) if ln.startswith("Error: ")), None)
        raise RuntimeError(error.removeprefix("Error: ") if error else f"the admin step failed (exit code {code})")


def _writable(game_dir: Path) -> bool:
    """Whether this process can change the game folder (Steam's folders let every user write)."""
    for folder in (game_dir, game_dir / WIN64_REL):
        try:
            with tempfile.TemporaryFile(dir=folder):
                pass
        except OSError:
            return False
    return True


def _needs_admin_for(game_dir: Path) -> bool:
    return not is_admin() and not _writable(game_dir)


# endregion


# region Game detection


def _steam_libraries() -> list[Path]:
    roots: list[Path] = []
    for hive, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                roots.append(Path(winreg.QueryValueEx(handle, value)[0]))
        except OSError:
            pass

    libraries: list[Path] = []
    for root in roots:
        libraries.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            libraries += [
                Path(p.replace("\\\\", "\\"))
                for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8"))
            ]
        except OSError:
            pass
    return list(dict.fromkeys(libraries))


def find_game_dirs() -> list[Path]:
    """All Borderlands 4 installs we can find (Steam and Epic)."""
    found: list[Path] = []
    for lib in _steam_libraries():
        manifest = lib / "steamapps" / f"appmanifest_{shared.STEAM_APP_ID}.acf"
        try:
            if match := re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8")):
                found.append(lib / "steamapps" / "common" / match.group(1))
        except OSError:
            pass

    epic = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic/EpicGamesLauncher/Data/Manifests"
    for item in epic.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "borderlands 4" in str(data.get("DisplayName", "")).lower():
            found.append(Path(data["InstallLocation"]))

    return [d for d in dict.fromkeys(found) if is_game_dir(d)]


def is_game_dir(path: Path) -> bool:
    return (path / GAME_EXE_REL).is_file()


def game_running() -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Borderlands4.exe", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    return "borderlands4.exe" in out.lower()


# endregion
# region Status


def vcredist_version() -> tuple[int, int] | None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
        ) as key:
            if not winreg.QueryValueEx(key, "Installed")[0]:
                return None
            return (winreg.QueryValueEx(key, "Major")[0], winreg.QueryValueEx(key, "Minor")[0])
    except OSError:
        return None


def installed_sdk_version(game_dir: Path) -> str | None:
    try:
        text = (game_dir / SDK_MARKER_REL).read_text(encoding="utf-8")
    except OSError:
        return None
    if match := re.search(r'\[mod_manager\][^\[]*display_version\s*=\s*"([^"\s]+)', text):
        return match.group(1)
    return "unknown"


def installed_mod_version(game_dir: Path) -> str | None:
    mod = game_dir / "sdk_mods" / "bl4_direct.sdkmod"
    try:
        with zipfile.ZipFile(mod) as zf:
            text = zf.read("bl4_direct/shared.py").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    match = re.search(r'MOD_VERSION = "([^"]+)"', text)
    return match.group(1) if match else "unknown"


def friends_file(game_dir: Path) -> Path:
    return game_dir / "sdk_mods" / "settings" / shared.FRIENDS_FILE_NAME


def load_config(game_dir: Path) -> shared.Config:
    path = friends_file(game_dir)
    config = shared.Config.load(path)
    if not path.exists() and firewall.rule_summary() is not None:
        # No friends file (deleted?) but our rule is there: keep it on, don't fall back to "off"
        config.firewall = True
    return config


@dataclass
class Status:
    game_dir: Path
    vcredist: tuple[int, int] | None
    sdk: str | None
    mod: str | None
    firewall: str | None
    conflicts: list[str]
    skip_intro: bool
    intro_skipped: bool
    mods: frozenset[str]
    dev_mods: frozenset[str] = frozenset()
    # The player's choice (Config.firewall): False = no rule wanted
    firewall_wanted: bool = True

    @property
    def vcredist_ok(self) -> bool:
        return self.vcredist is not None and self.vcredist >= VCREDIST_MIN

    @property
    def sdk_ok(self) -> bool:
        return self.sdk == shared.SDK_VERSION

    @property
    def mod_ok(self) -> bool:
        return self.mod == shared.MOD_VERSION

    @property
    def ready(self) -> bool:
        return self.vcredist_ok and self.sdk_ok and self.mod_ok and not self.conflicts

    def lines(self) -> list[str]:
        def mark(ok: bool) -> str:
            return "OK " if ok else "!! "

        vc = ".".join(map(str, self.vcredist)) if self.vcredist else "not installed"
        return [
            f"Game:       {self.game_dir}",
            f"{mark(self.vcredist_ok)}VC++ runtime: {vc} (need {'.'.join(map(str, VCREDIST_MIN))}+)",
            f"{mark(self.sdk_ok)}Python SDK:   {self.sdk or 'not installed'} (need {shared.SDK_VERSION})",
            f"{mark(self.mod_ok)}BL4 Direct Connect: {self.mod or 'not installed'} (this app: {shared.MOD_VERSION})",
            f"{mark(self.firewall_ok)}Firewall:     {self._firewall_line()}",
            f"Skip intro:   {self._intro_line()}",
            f"Mods:         {self._mods_line()}",
            *(f"!! {c}" for c in self.conflicts),
            f"Compatibility: {shared.compat_code(self.game_dir)}",
        ]


    @property
    def firewall_ok(self) -> bool:
        return (self.firewall is not None) == self.firewall_wanted

    def _firewall_line(self) -> str:
        if not self.firewall_wanted:
            return "off (your choice; no rule)" if self.firewall is None else "off, but the old rule is still there (repair removes it)"
        return self.firewall or "no rule (hosted games reachable by anyone who can reach the port)"

    def _mods_line(self) -> str:
        optional = [catalog.BY_ID[m].name for m in catalog.OPTIONAL if m in self.mods]
        line = f"{len(optional)} of {len(catalog.OPTIONAL)} optional installed" + (
            f" ({', '.join(optional)})" if optional else ""
        )
        if self.dev_mods:
            # A dev machine: the SDK loads the repo's copies (unrealsdk.user.toml), and those win
            line += f"; {len(self.dev_mods)} of {len(catalog.MODS)} load from a dev folder instead"
        return line

    def _intro_line(self) -> str:
        if self.skip_intro:
            return "on (2K / Gearbox logos moved aside)" if self.intro_skipped else "on (applies on the next launch)"
        return "off" if not self.intro_skipped else "off (logos still moved aside - toggle it to put them back)"


def _sdk_zip_names() -> set[str]:
    with zipfile.ZipFile(SDK_ZIP) as zf:
        return {n for n in zf.namelist() if not n.endswith("/")}


def find_conflicts(game_dir: Path) -> list[str]:
    """Other mod loaders that would fight with the SDK over dsound.dll."""
    conflicts = []
    dsound = game_dir / WIN64_REL / "dsound.dll"
    if dsound.exists() and installed_sdk_version(game_dir) is None:
        conflicts.append(f"{dsound} exists but isn't the Python SDK's - another mod loader?")
    return conflicts


def status(game_dir: Path) -> Status:
    return Status(
        game_dir=game_dir,
        vcredist=vcredist_version(),
        sdk=installed_sdk_version(game_dir),
        mod=installed_mod_version(game_dir),
        firewall=firewall.rule_summary(),
        conflicts=find_conflicts(game_dir),
        firewall_wanted=load_config(game_dir).firewall,
        skip_intro=skip_intro_enabled(game_dir),
        intro_skipped=movies.skipped(game_dir),
        mods=installed_mods(game_dir),
        dev_mods=dev_mods(game_dir),
    )


def installed_mods(game_dir: Path) -> frozenset[str]:
    """Catalog mods in sdk_mods (Skip Intro counts only while it's switched on)."""
    sdk_mods = game_dir / "sdk_mods"
    found = {
        m.id for m in catalog.MODS if (sdk_mods / f"{m.id}.sdkmod").is_file() or (sdk_mods / m.id).is_dir()
    }
    if SKIP_INTRO in found and not skip_intro_enabled(game_dir):
        found.discard(SKIP_INTRO)
    return frozenset(found)


def dev_mods(game_dir: Path) -> frozenset[str]:
    """
    Catalog mods the SDK loads from `[mod_manager] extra_folders` in unrealsdk.user.toml (the dev
    setup: junctions into the repo) rather than from sdk_mods. Players don't have that file.
    """
    try:
        with (game_dir / WIN64_REL / "Plugins" / "unrealsdk.user.toml").open("rb") as f:
            folders = tomllib.load(f).get("mod_manager", {}).get("extra_folders", [])
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(folders, list):
        return frozenset()
    return frozenset(
        m.id for m in catalog.MODS
        if any(isinstance(d, str) and (Path(d) / m.id / "__init__.py").is_file() for d in folders)
    )


# endregion
# region Install / uninstall


def _verify_payload() -> None:
    digest = hashlib.sha256(SDK_ZIP.read_bytes()).hexdigest()
    if digest != SDK_ZIP_SHA256:
        raise RuntimeError(f"bundled SDK zip is corrupt (sha256 {digest})")
    if missing := [m.id for m in catalog.MODS if not payload_mod(m.id).is_file()]:
        raise RuntimeError(f"bundled mod file is missing: {', '.join(missing)}")
    if missing := [m.id for m in catalog.MODS if m.assets and not payload_assets(m.id).is_file()]:
        raise RuntimeError(f"bundled pictures are missing: {', '.join(missing)}")


def install_vcredist(log) -> None:  # noqa: ANN001
    log("Downloading Microsoft Visual C++ runtime...")
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "vc_redist.x64.exe"
        urllib.request.urlretrieve(VCREDIST_URL, exe)  # noqa: S310
        if is_admin():
            log("Installing Microsoft Visual C++ runtime...")
            code = subprocess.run([str(exe), "/install", "/quiet", "/norestart"], check=False).returncode
        else:
            log("Installing Microsoft Visual C++ runtime (Windows asks for admin)...")
            code = run_as_admin(str(exe), "/install /quiet /norestart")
        # 3010 = success, reboot required; 1638 = newer version already installed
        if code not in (0, 3010, 1638):
            raise RuntimeError(f"VC++ runtime installer failed with code {code}")


def choose_mods(
    game_dir: Path,
    mods: set[str] | frozenset[str] | None = None,
    skip_intro: bool | None = None,
) -> set[str]:
    """
    What an install leaves installed: `mods` if given, else what's installed now (the defaults on
    a first install). skip_intro (the old switch) adds or drops Skip Intro. Required mods always.
    """
    if mods is None:
        current = installed_mods(game_dir)
        first = not current and not (game_dir / "sdk_mods" / "settings" / INSTALL_RECORD).exists()
        chosen = set(catalog.DEFAULTS if first else current)
    else:
        if unknown := set(mods) - set(catalog.BY_ID):
            raise RuntimeError(f"unknown mod(s): {', '.join(sorted(unknown))}")
        chosen = set(mods)
    if skip_intro is True:
        chosen.add(SKIP_INTRO)
    elif skip_intro is False:
        chosen.discard(SKIP_INTRO)
    return chosen | set(catalog.REQUIRED)


def install(
    game_dir: Path,
    log=print,  # noqa: ANN001
    skip_intro: bool | None = None,
    mods: set[str] | frozenset[str] | None = None,
    use_firewall: bool | None = None,
) -> None:
    """
    Installs/repairs everything, and adds/removes optional mods. Asks for admin (UAC) only for
    what needs it: the VC++ runtime, the firewall rule, a game folder this user can't write to.

    mods: the mods to have installed (required ones are added); None keeps what's installed (the
    defaults on a first install). skip_intro: the old Skip Intro switch, applied on top.
    use_firewall: the hosting firewall rule on/off (Config.firewall); None keeps the saved choice.
    """
    if not is_game_dir(game_dir):
        raise RuntimeError(f"{game_dir} doesn't look like a Borderlands 4 install")
    if game_running():
        raise RuntimeError("Close Borderlands 4 first.")
    _verify_payload()
    if conflicts := find_conflicts(game_dir):
        raise RuntimeError("\n".join(conflicts))
    chosen = choose_mods(game_dir, mods, skip_intro)
    if _needs_admin_for(game_dir):
        log("This user can't change the game folder: Windows asks for admin to install.")
        optional = ",".join(sorted(chosen - set(catalog.REQUIRED))) or "none"
        args = ["--install", "--game", str(game_dir), "--mods", optional]
        if use_firewall is not None:
            args += ["--firewall", "on" if use_firewall else "off"]
        _run_self_elevated(args, log)
        return

    vc = vcredist_version()
    if vc is None or vc < VCREDIST_MIN:
        install_vcredist(log)

    current_sdk = installed_sdk_version(game_dir)
    if current_sdk != shared.SDK_VERSION:
        log(f"Installing Python SDK {shared.SDK_VERSION} (was: {current_sdk or 'none'})...")
        with zipfile.ZipFile(SDK_ZIP) as zf:
            # Never overwrite user settings that might be in the zip
            members = [n for n in zf.namelist() if not n.startswith("sdk_mods/settings/")]
            zf.extractall(game_dir, members)
    else:
        log(f"Python SDK {current_sdk} already installed.")

    sdk_mods = game_dir / "sdk_mods"
    settings = sdk_mods / "settings"
    settings.mkdir(exist_ok=True)
    before = installed_mods(game_dir)
    for info in catalog.MODS:
        if info.id in chosen:
            _install_mod(game_dir, info, new=info.id not in before, log=log)
        elif info.id in before or (sdk_mods / f"{info.id}.sdkmod").exists() or (
            info.id == SKIP_INTRO and skip_intro_enabled(game_dir)  # switched on without the mod
        ):
            remove_mod(game_dir, info.id, log)
    added = [catalog.BY_ID[m].name for m in catalog.OPTIONAL if m in chosen and m not in before]
    optional = len(chosen) - len(catalog.REQUIRED)
    log(
        f"Installed BL4 Fix {shared.MOD_VERSION}: BL4 Direct Connect, the Mod Menu and {optional} optional mod(s)"
        + (f" (new: {', '.join(added)})." if added else "."),
    )
    if SKIP_INTRO in chosen:
        set_skip_intro(game_dir, True, log)
    prepare_pages(game_dir, chosen, log)

    _settle_app(game_dir, log)

    config = load_config(game_dir)
    if use_firewall is not None:
        config.firewall = use_firewall
    if use_firewall is not None or not friends_file(game_dir).exists():
        config.save(friends_file(game_dir))
    apply_firewall(game_dir, log)

    _write_json(
        settings / INSTALL_RECORD,
        {"mod": shared.MOD_VERSION, "sdk": shared.SDK_VERSION, "mods": sorted(chosen)},
    )
    log("Done. In game: F6 for co-op (BL4 Direct Connect), F7 (or MODS in the pause menu) for mod settings.")


def _write_json(path: Path, data: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(path)


def _install_mod(game_dir: Path, info: catalog.ModInfo, new: bool, log=print) -> None:  # noqa: ANN001
    sdk_mods = game_dir / "sdk_mods"
    # A loose folder copy takes priority over the .sdkmod, so move any old manual install aside
    loose = sdk_mods / info.id
    if loose.is_dir():
        backup = sdk_mods / f".{info.id}_old_folder"
        shutil.rmtree(backup, ignore_errors=True)
        loose.rename(backup)
        log(f"Moved old mod folder to {backup}")
    target = sdk_mods / f"{info.id}.sdkmod"
    have, ours = sdkmod_version(target, info.id), sdkmod_version(payload_mod(info.id), info.id)
    if have and ours and _version_key(have) > _version_key(ours):
        # The in-game updater installed a newer release since this app was built: keep it
        log(f"Kept {info.name} {have} (newer than this app's {ours}).")
    else:
        shutil.copyfile(payload_mod(info.id), target)
    if info.assets:
        _install_assets(game_dir, info, log)
    if info.id == SKIP_INTRO:
        return  # its enabled flag is the movie switch (set_skip_intro)

    # Required mods are always switched on; an optional one only when it's newly installed, so a
    # repair doesn't undo the player turning it off in the mod list
    if not (info.required or new):
        return
    mod_settings = sdk_mods / "settings" / f"{info.id}.json"
    try:
        data = json.loads(mod_settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["enabled"] = True
    _write_json(mod_settings, data)


def _install_assets(game_dir: Path, info: catalog.ModInfo, log=print) -> None:  # noqa: ANN001
    """
    Unpacks a mod's bundled pictures into uiresources. Each folder has an index.json of picture ->
    version (the page only shows listed pictures): a picture the player already has in a newer
    version (taken in their own game) is kept, and the indexes are merged. Full-size folders go
    before their small/ copies, so a small copy is never older than its picture (Weapondex would
    remake it otherwise).
    """
    ui = game_dir / UI_REL
    added = 0
    with zipfile.ZipFile(payload_assets(info.id)) as zf:
        indexes = [n.rsplit("/", 1)[0] for n in zf.namelist() if n.endswith("/index.json")]
        folders = sorted(indexes, key=lambda f: (f.count("/"), f))
        for folder in folders:
            ours = json.loads(zf.read(f"{folder}/index.json"))
            target = ui / folder
            try:
                theirs = json.loads((target / "index.json").read_text(encoding="utf-8"))
                if not isinstance(theirs, dict):
                    theirs = {}
            except (OSError, ValueError):
                theirs = {}
            target.mkdir(parents=True, exist_ok=True)
            merged = dict(theirs)
            for key, version in ours.items():
                png = target / f"{key}.png"
                if theirs.get(key, 0) > version and png.is_file():
                    continue
                content = zf.read(f"{folder}/{key}.png")
                try:
                    if png.read_bytes() == content:
                        merged[key] = version
                        continue
                except OSError:
                    pass
                png.write_bytes(content)
                merged[key] = version
                added += 1
            _write_json(target / "index.json", merged)
    if added:
        log(f"Installed {added} {info.name} pictures.")


def remove_mod(game_dir: Path, mod_id: str, log=print) -> None:  # noqa: ANN001
    """Removes one mod and the files it wrote into the game folder (its settings are kept)."""
    if game_running():
        raise RuntimeError("Close Borderlands 4 first.")
    info = catalog.BY_ID[mod_id]
    sdk_mods = game_dir / "sdk_mods"
    had = (sdk_mods / f"{mod_id}.sdkmod").exists()
    if mod_id == SKIP_INTRO:
        if restored := movies.restore(game_dir):
            log(f"Put the intro movies back ({', '.join(restored)}).")
        (sdk_mods / "settings" / SKIP_INTRO_SETTINGS).unlink(missing_ok=True)
    (sdk_mods / f"{mod_id}.sdkmod").unlink(missing_ok=True)
    # Shim paks the mod wrote: the game would keep loading them otherwise. Never while the game
    # runs (checked above): the game re-reads paks by path
    for pak in info.paks:
        for name in (pak, f"{pak}.next", f"{pak}.remove"):
            (game_dir / MOD_PAKS_REL / name).unlink(missing_ok=True)
    for folder in info.ui_dirs:
        shutil.rmtree(game_dir / UI_REL / folder, ignore_errors=True)
    # The BL4 UI Link's pak and loose files are shared: only the last mod using it takes them away
    if info.link and not any(m.link and (sdk_mods / f"{m.id}.sdkmod").exists() for m in catalog.MODS):
        for name in (catalog.LINK_PAK, f"{catalog.LINK_PAK}.next", f"{catalog.LINK_PAK}.remove"):
            (game_dir / MOD_PAKS_REL / name).unlink(missing_ok=True)
        shutil.rmtree(game_dir / UI_REL / catalog.LINK_UI_DIR, ignore_errors=True)
    if had:
        log(f"Removed {info.name}.")


def _shortcut_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs" / SHORTCUT_NAME if appdata else None


def _refresh_icons() -> None:
    """
    Explorer caches icons by path, so a replaced exe kept showing its old icon (shortcut, pins)
    until the cache refreshed: SHCNE_ASSOCCHANGED makes the shell reload them.
    """
    try:
        ctypes.WinDLL("shell32").SHChangeNotify(0x08000000, 0x0000, None, None)  # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
    except (OSError, AttributeError):
        pass


def _shortcut_targets(link: Path, exe: Path) -> bool:
    """Whether a .lnk file points at `exe` (the path is stored in it as ANSI and/or UTF-16 text)."""
    try:
        data = link.read_bytes().lower()
    except OSError:
        return False
    path = str(exe).lower()
    return path.encode("utf-16-le") in data or path.encode("mbcs", "replace") in data


def _settle_app(game_dir: Path, log=print) -> None:  # noqa: ANN001
    """
    Removes the old copy of this app from the game folder and points Start menu > BL4 Fix Setup at
    the exe the player ran (it updates itself in place, so the entry stays good). Started from the
    old copy itself (the old Start menu entry), the app first moves out of the game folder.
    """
    if not getattr(sys, "frozen", False):
        return
    exe = Path(sys.executable).resolve()
    tools = game_dir / TOOLS_DIR_REL
    old_copy = tools / OLD_COPY_NAME
    if exe.is_relative_to(tools.resolve()) and (local := os.environ.get("LOCALAPPDATA")):
        home = Path(local) / APP_HOME_REL
        try:
            home.parent.mkdir(parents=True, exist_ok=True)
            home.unlink(missing_ok=True)
            try:
                exe.rename(home)  # Windows lets a running exe be moved on the same drive
            except OSError:
                # Another drive: copy. The running original goes at the next run (or the Mod Menu's)
                shutil.copyfile(exe, home)
            exe = home
            log(f"Moved BL4 Fix Setup out of the game folder, to {home}.")
        except OSError as ex:
            log(f"Couldn't move BL4 Fix Setup out of the game folder ({ex}).")
    if tools.is_dir():
        shutil.rmtree(tools, ignore_errors=True)
        if not old_copy.exists():
            log("Removed the old copy of BL4 Fix Setup from the game folder.")
    if exe.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        # Opened from inside a zip (Windows unpacks it into a temp folder): no entry to a file that
        # will vanish. Drop an entry pointing at the old copy
        if (link := _shortcut_path()) is not None and _shortcut_targets(link, old_copy):
            link.unlink(missing_ok=True)
        return
    _make_shortcut(exe, log)


def _make_shortcut(target: Path, log=print) -> None:  # noqa: ANN001
    """Start menu > BL4 Fix Setup, to add or remove mods any time."""
    if (link := _shortcut_path()) is None:
        return
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:BL4_LINK);"
        "$s.TargetPath = $env:BL4_TARGET; $s.WorkingDirectory = $env:BL4_DIR;"
        "$s.IconLocation = $env:BL4_TARGET + ',0';"
        "$s.Description = 'Add or remove Borderlands 4 mods'; $s.Save()"
    )
    env = {**os.environ, "BL4_LINK": str(link), "BL4_TARGET": str(target), "BL4_DIR": str(target.parent)}
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, capture_output=True, text=True, check=False, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as ex:
        log(f"Couldn't make the Start menu shortcut ({ex}).")
        return
    if res.returncode == 0:
        log("Start menu: 'BL4 Fix Setup' adds or removes mods any time.")
        for old in OLD_SHORTCUT_NAMES:
            (link.parent / old).unlink(missing_ok=True)
        _refresh_icons()
    else:
        log(f"Couldn't make the Start menu shortcut: {res.stderr.strip()[:200]}")


def apply_firewall(game_dir: Path, log=print) -> None:  # noqa: ANN001
    """Makes our rule match the friends file: the allowlist, or no rule if the player turned it off."""
    config = load_config(game_dir)
    rule = firewall.rule_summary()
    if not config.firewall and rule is None:
        # Off: never touch the firewall (no admin needed either)
        log("Firewall: left alone (rule turned off).")
        config.firewall_applied = config.firewall_signature()
        config.save(friends_file(game_dir))
        return
    if not config.firewall:
        # Off: take away the rule we added before
        firewall.remove_rule(_firewall_runner(log))
        log("Firewall: removed the BL4 Direct rule (turned off).")
        config.firewall_applied = config.firewall_signature()
        config.save(friends_file(game_dir))
        return
    ips = list(dict.fromkeys(f.address for f in config.friends if shared.is_ipv4(f.address)))
    allowed = ", ".join(["this PC", *(["your local network/VPN"] if config.allow_lan else []), *ips])
    if firewall.rule_is_current(shared.firewall_rule(config.host_port, ips, config.allow_lan)):
        # Windows has this exact rule already: a repair or an update asks for no admin
        log(f"Firewall: already up to date (UDP {config.host_port} only accepts {allowed}).")
    else:
        firewall.apply_rule(config.host_port, ips, config.allow_lan, _firewall_runner(log))
        log(f"Firewall: UDP {config.host_port} only accepts {allowed}")
    config.firewall_applied = config.firewall_signature()
    config.save(friends_file(game_dir))


def remove_firewall_rule(log=print) -> None:  # noqa: ANN001
    """Takes our firewall rule away, if there is one (asks for admin then)."""
    if firewall.rule_summary() is None:
        return
    firewall.remove_rule(_firewall_runner(log))
    log("Removed the BL4 Direct firewall rule.")


def _firewall_runner(log=print):  # noqa: ANN001, ANN202
    """
    None when this app is admin (netsh runs directly), else run_as_admin: the UAC prompt then shows
    netsh ("Network Command Shell") with the rule in plain words, not this app (2026-10-30).
    """
    if is_admin():
        return None
    log(shared.UAC_HINT)
    return run_as_admin


def uninstall(game_dir: Path, remove_sdk: bool, log=print) -> None:  # noqa: ANN001
    if game_running():
        raise RuntimeError("Close Borderlands 4 first.")
    if _needs_admin_for(game_dir):
        log("This user can't change the game folder: Windows asks for admin to uninstall.")
        _run_self_elevated(["--uninstall", "--game", str(game_dir), *(["--remove-sdk"] if remove_sdk else [])], log)
        return
    sdk_mods = game_dir / "sdk_mods"
    for info in catalog.MODS:
        # Skip Intro always: its movies may be moved aside even with the mod gone
        if (sdk_mods / f"{info.id}.sdkmod").exists() or info.id == SKIP_INTRO:
            remove_mod(game_dir, info.id, log)
    (sdk_mods / "settings" / INSTALL_RECORD).unlink(missing_ok=True)
    shutil.rmtree(game_dir / TOOLS_DIR_REL, ignore_errors=True)
    if (link := _shortcut_path()) is not None:
        for name in (link.name, *OLD_SHORTCUT_NAMES):
            (link.parent / name).unlink(missing_ok=True)
    remove_firewall_rule(log)
    log("Removed BL4 Fix and its mods (your friends list and mod settings are kept).")

    if remove_sdk:
        for name in _sdk_zip_names():
            if not name.startswith("sdk_mods/settings/"):
                (game_dir / name).unlink(missing_ok=True)
        log("Removed the Python SDK files (other mods in sdk_mods were left alone).")


# endregion
# region Game pages
#
# Some mods replace a game page (pause menu, title screen, item card, Skills, ECHO pages) with a
# shim pak that loads a loose script of theirs. A mod builds that pak itself when it's enabled, but
# the game reads paks only at startup: on its own, a fresh install's first launch had no MODS
# button, no Item Cards and old F6/F7 panels until a second restart. So the installer runs the
# mods' own page code first, while the game is closed: every check the mod makes (the game's page
# unchanged since the shim was made, no other ~mods pak replacing it) and the same pak bytes, so
# the mod in game finds its pak current and leaves it alone.

# (mod id, page module in the mod, whether its sync() takes `enabled`)
PAGE_MODULES: tuple[tuple[str, str, bool], ...] = (
    # Since 0.2.0 these only remove the Mod Menu's old pause / title page paks (4245 / 4247); the
    # rest remove their mod's old page pak too (scripts run through the UI Link now) and publish
    # its loose files
    ("bl4_mod_menu", "pause_page", False),
    ("bl4_mod_menu", "main_page", False),
    ("bl4_item_cards", "card_page", True),
    ("bl4_respec", "skills_page", True),
    ("bl4_instant_tabs", "pages", False),
    ("bl4_weapondex", "celebrate_page", True),
    ("bl4_damage_meter", "popup_page", True),
    # The BL4 UI Link (shared; whichever of these is installed builds the same pak)
    ("bl4_builds", "uilink", False),
    ("bl4_tidy_pause", "uilink", False),
    ("bl4_reroll", "uilink", False),
    ("bl4_uvh_teleport", "uilink", False),
    ("bl4_damage_meter", "uilink", False),
    ("bl4_weapondex", "uilink", False),
    ("bl4_mod_menu", "uilink", False),
    ("bl4_item_cards", "uilink", False),
    ("bl4_respec", "uilink", False),
    ("bl4_instant_tabs", "uilink", False),
    ("bl4_item_pictures", "uilink", False),
)
# Loose scripts those pages load at once, which these mods publish from code that only runs in
# game (it imports the SDK): (mod id, folder in the .sdkmod, folder under uiresources). Item
# Cards and Easy Respec publish theirs from their page module's sync().
PAGE_SCRIPTS: tuple[tuple[str, str, str], ...] = (
    ("bl4_mod_menu", "ui", "bl4modmenu"),
    ("bl4_instant_tabs", "ui", "bl4instanttabs"),
    # The link's boot.js must be there when the game creates its pages (before any mod runs)
    ("bl4_builds", "uilink/ui", "bl4link"),
    ("bl4_tidy_pause", "uilink/ui", "bl4link"),
    ("bl4_reroll", "uilink/ui", "bl4link"),
    ("bl4_uvh_teleport", "uilink/ui", "bl4link"),
    ("bl4_damage_meter", "uilink/ui", "bl4link"),
    ("bl4_weapondex", "uilink/ui", "bl4link"),
    ("bl4_mod_menu", "uilink/ui", "bl4link"),
    ("bl4_item_cards", "uilink/ui", "bl4link"),
    ("bl4_respec", "uilink/ui", "bl4link"),
    ("bl4_instant_tabs", "uilink/ui", "bl4link"),
    ("bl4_item_pictures", "uilink/ui", "bl4link"),
)


def mod_enabled(game_dir: Path, mod_id: str) -> bool:
    """The mod manager's enabled flag (settings/<id>.json); a mod without one loads enabled."""
    try:
        data = json.loads((game_dir / "sdk_mods" / "settings" / f"{mod_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return bool(data.get("enabled", True)) if isinstance(data, dict) else True


def _load_page_module(root: Path, mod_id: str, module: str):  # noqa: ANN202
    """
    Imports `<mod>.<module>` from an unpacked copy of the mod without running the mod's own
    __init__ (which needs the game): a stand-in parent package under a private name.
    """
    import importlib  # noqa: PLC0415
    import types  # noqa: PLC0415

    alias = f"_bl4setup_{mod_id}"
    for name in [n for n in sys.modules if n == alias or n.startswith(alias + ".")]:
        del sys.modules[name]
    package = types.ModuleType(alias)
    package.__path__ = [str(root / mod_id)]  # type: ignore[attr-defined]
    sys.modules[alias] = package
    return importlib.import_module(f"{alias}.{module}")


def prepare_pages(game_dir: Path, chosen: set[str] | frozenset[str], log=print) -> None:  # noqa: ANN001
    """Puts the installed mods' shim paks and page scripts in place before the game first starts."""
    wanted = [(m, page, flag) for m, page, flag in PAGE_MODULES if m in chosen]
    if not wanted:
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for mod_id in dict.fromkeys(m for m, _, _ in wanted):
            with zipfile.ZipFile(payload_mod(mod_id)) as zf:
                zf.extractall(root)
        for mod_id, page, takes_enabled in wanted:
            enabled = mod_enabled(game_dir, mod_id)
            if not enabled and not takes_enabled:
                continue  # left to the mod: it removes its own page when switched off in game
            try:
                module = _load_page_module(root, mod_id, page)
                status = module.sync(game_dir, enabled) if takes_enabled else module.sync(game_dir)
            except Exception as ex:  # noqa: BLE001 - the mod sets its page up in game anyway
                status = f"couldn't prepare its page ({ex}); it does that itself in game (one extra restart)"
            if status:
                log(f"{catalog.BY_ID[mod_id].name}: {status}")
        for mod_id, folder, ui_dir in PAGE_SCRIPTS:
            if mod_id not in chosen or not mod_enabled(game_dir, mod_id):
                continue
            source = root / mod_id / folder
            target = game_dir / UI_REL / ui_dir
            target.mkdir(parents=True, exist_ok=True)
            for path in sorted(source.glob("*")):
                if path.is_file():
                    tmp_file = target / (path.name + ".tmp")
                    tmp_file.write_bytes(path.read_bytes())
                    tmp_file.replace(target / path.name)
        for alias in [n for n in sys.modules if n.startswith("_bl4setup_")]:
            del sys.modules[alias]


# endregion
# region Skip intro

SKIP_INTRO_SETTINGS = "bl4_skip_intro.json"


def skip_intro_enabled(game_dir: Path) -> bool:
    """The in-game mod's enabled flag, which is also this app's switch."""
    try:
        data = json.loads((game_dir / "sdk_mods" / "settings" / SKIP_INTRO_SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("enabled"))


def set_skip_intro(game_dir: Path, on: bool, log=print) -> None:  # noqa: ANN001
    """
    Moves the logo movies aside (on) or puts them back (off), and sets the in-game mod to match.

    Not while the game runs: the mod manager writes its enabled flags back on exit, and the mod would
    then undo this on the next launch (in game, use the mod's own toggle instead).
    """
    if game_running():
        raise RuntimeError("Close Borderlands 4 first (or toggle Skip Intro in the game's mod list).")
    settings = game_dir / "sdk_mods" / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    path = settings / SKIP_INTRO_SETTINGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["enabled"] = on
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")

    if on:
        movies.skip(game_dir)
        log("Skip intro: on - the 2K and Gearbox logo movies won't play.")
    else:
        movies.restore(game_dir)
        log("Skip intro: off - the logo movies are back.")


# endregion
# region Safe mode
#
# "Start without other modders' mods": for when a mod breaks the game. Every mod in sdk_mods that
# isn't in the catalog (and isn't the SDK's own) moves into sdk_mods/.bl4_safe_mode/, which the SDK
# doesn't load (it skips dot entries); nothing is deleted. The record there lists what moved, by
# the name it had in sdk_mods. The Mod Menu reads the same folder and record in game ("N mods set
# aside"): keep the format. Safe mode is on while the record exists.

SAFE_MODE_REL = Path("sdk_mods/.bl4_safe_mode")
SAFE_MODE_RECORD = "safe_mode.json"
SAFE_MODE_FORMAT = 1
# The SDK's own entries in sdk_mods (oak2-sdk.zip and what it makes), never set aside
SDK_OWN = frozenset({"mods_base", "console_mod_menu", "keybinds", "unrealsdk", "pyunrealsdk", "settings", "__main__.py"})


def _other_mod_entry(entry: Path) -> bool:
    """Whether an sdk_mods entry is a mod the SDK would load that isn't BL4 Fix's or the SDK's."""
    name = entry.name
    if name.startswith((".", "_")) or name.lower() in SDK_OWN:
        return False
    if entry.is_dir():
        # The SDK skips folders with a dot in the name (not a valid module): nothing to set aside
        mod_id = name if "." not in name else None
    elif entry.is_file() and entry.suffix.lower() == ".sdkmod":
        mod_id = entry.stem
    else:
        return False
    return mod_id is not None and mod_id not in catalog.BY_ID and mod_id.lower() not in SDK_OWN


def other_mods(game_dir: Path) -> list[str]:
    """Other modders' mods in sdk_mods right now (names as they are there: foo.sdkmod, foo)."""
    sdk_mods = game_dir / "sdk_mods"
    try:
        entries = sorted(sdk_mods.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [e.name for e in entries if _other_mod_entry(e)]


def _safe_mode_record(game_dir: Path) -> dict | None:
    """The record (safe mode is on), or None. An unreadable record still counts as on."""
    path = game_dir / SAFE_MODE_REL / SAFE_MODE_RECORD
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"format": SAFE_MODE_FORMAT, "moved": [], "time": 0}
    if not isinstance(data, dict):
        return {"format": SAFE_MODE_FORMAT, "moved": [], "time": 0}
    moved = data.get("moved")
    data["moved"] = [m for m in moved if isinstance(m, str) and m] if isinstance(moved, list) else []
    return data


def _safe_name(name: str) -> bool:
    """A name from the record that stays one entry directly in sdk_mods (no paths smuggled in)."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name and ":" not in name


def _aside_entries(game_dir: Path) -> list[str]:
    """What sits in the safe mode folder besides the record (and its temp file)."""
    try:
        entries = (game_dir / SAFE_MODE_REL).iterdir()
        return sorted((e.name for e in entries if not e.name.startswith(SAFE_MODE_RECORD)), key=str.lower)
    except OSError:
        return []


def safe_mode_status(game_dir: Path) -> dict:
    """
    on: the record exists. aside: what the record lists that is in the folder. missing: listed but
    gone (moved back by hand). left: in the folder but not listed (a copy that couldn't go back
    because sdk_mods had one again, or a move a crash didn't record). others: other modders' mods
    in sdk_mods now (what turning it on would set aside). time: when it was turned on.
    """
    record = _safe_mode_record(game_dir)
    present = _aside_entries(game_dir)
    listed = record["moved"] if record else []
    return {
        "on": record is not None,
        "aside": [n for n in listed if n in present],
        "missing": [n for n in listed if n not in present],
        "left": [n for n in present if n not in listed],
        "others": other_mods(game_dir),
        "time": (record or {}).get("time", 0),
    }


def set_safe_mode(game_dir: Path, on: bool, log=print) -> dict:  # noqa: ANN001
    """
    On: moves every other modder's mod into sdk_mods/.bl4_safe_mode and writes the record (turning
    it on again sets aside mods added since). Off: moves them back, skipping (and reporting) any
    whose name sdk_mods has again, then removes the record and the folder if it's empty.
    Returns {"moved": [names], "skipped": [[name, reason]]}. Not while the game runs: it has the
    mods open (and a mod moved under it would vanish mid-session).
    """
    if game_running():
        raise RuntimeError("Close Borderlands 4 first.")
    if _needs_admin_for(game_dir):
        log("This user can't change the game folder: Windows asks for admin to change safe mode.")
        _run_self_elevated(["--safe-mode", "on" if on else "off", "--game", str(game_dir)], log)
        return {"moved": [], "skipped": [], "elevated": True}
    sdk_mods = game_dir / "sdk_mods"
    aside = game_dir / SAFE_MODE_REL
    record_path = aside / SAFE_MODE_RECORD
    moved: list[str] = []
    skipped: list[list[str]] = []
    if on:
        record = _safe_mode_record(game_dir) or {"format": SAFE_MODE_FORMAT, "moved": []}
        aside.mkdir(parents=True, exist_ok=True)
        for name in other_mods(game_dir):
            target = aside / name
            if target.exists() or target.is_symlink():
                skipped.append([name, "a copy with that name is already set aside"])
                log(f"Left {name} in place: a copy with that name is already in {SAFE_MODE_REL.as_posix()}.")
                continue
            try:
                (sdk_mods / name).rename(target)
            except OSError as ex:
                skipped.append([name, f"couldn't move it ({ex.strerror or ex})"])
                log(f"Couldn't set {name} aside: {ex}")
                continue
            moved.append(name)
        # Keep what's still set aside from before; drop names put back by hand since
        listed = [n for n in record["moved"] if n not in moved and _safe_name(n) and (aside / n).exists()]
        _write_json(record_path, {"format": SAFE_MODE_FORMAT, "moved": [*listed, *moved], "time": int(time.time())})
        total = len(listed) + len(moved)
        log(f"Safe mode on: {total} mod{'' if total == 1 else 's'} by other modders set aside"
            f" ({len(moved)} now). BL4 Fix's mods keep working.")
        return {"moved": moved, "skipped": skipped}

    record = _safe_mode_record(game_dir)
    listed = [n for n in (record["moved"] if record else []) if _safe_name(n)]
    # Also anything else in the folder: a copy an earlier Off couldn't put back, or a move a crash
    # left unrecorded
    for name in dict.fromkeys([*listed, *_aside_entries(game_dir)]):
        source, target = aside / name, sdk_mods / name
        if not (source.exists() or source.is_symlink()):
            continue  # already moved back by hand
        if target.exists() or target.is_symlink():
            skipped.append([name, "sdk_mods has one with that name again"])
            log(f"Kept {name} aside: sdk_mods has one with that name again (it's still in {SAFE_MODE_REL.as_posix()}).")
            continue
        try:
            source.rename(target)
        except OSError as ex:
            skipped.append([name, f"couldn't move it back ({ex.strerror or ex})"])
            log(f"Couldn't put {name} back: {ex}")
            continue
        moved.append(name)
    record_path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        aside.rmdir()  # only when empty
    log(f"Safe mode off: {len(moved)} mod{'' if len(moved) == 1 else 's'} put back"
        + (f", {len(skipped)} left aside." if skipped else "."))
    return {"moved": moved, "skipped": skipped}


# endregion
# region Bug reports
#
# The Mod Menu's report.py builds the zip (versions, errors, logs, the newest crash, mods and
# their settings, private details masked). The app runs it with the game closed, for when the game
# won't start: the installed Mod Menu's copy (it matches the files the game writes), else this
# app's payload copy. Like the page modules, without the mod's __init__ (which needs the game).

REPORT_ENTRY = "bl4_mod_menu/report.py"


def _report_module(game_dir: Path):  # noqa: ANN202
    import types  # noqa: PLC0415

    sources = [game_dir / "sdk_mods" / "bl4_mod_menu.sdkmod", payload_mod("bl4_mod_menu")]
    for sdkmod in sources:
        try:
            with zipfile.ZipFile(sdkmod) as zf:
                code = zf.read(REPORT_ENTRY)
        except (OSError, KeyError, zipfile.BadZipFile):
            continue  # missing, or a Mod Menu from before bug reports
        module = types.ModuleType("_bl4setup_report")
        module.__file__ = f"{sdkmod}/{REPORT_ENTRY}"
        exec(compile(code, module.__file__, "exec"), module.__dict__)  # noqa: S102 - our own mod's file
        return module
    source = Path(__file__).resolve().parents[1] / "mods" / REPORT_ENTRY
    if not getattr(sys, "frozen", False) and source.is_file():  # run from the repo, payload not rebuilt yet
        module = types.ModuleType("_bl4setup_report")
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)  # noqa: S102
        return module
    raise RuntimeError("No Mod Menu with bug reports found: Install (or repair) BL4 Fix first.")


def bug_report(game_dir: Path, out_dir: Path | None = None, log=print) -> tuple[Path, str]:  # noqa: ANN001
    """Builds a report zip (default: sdk_mods/.bug_reports). Returns it and the GitHub issue link."""
    if not is_game_dir(game_dir):
        raise RuntimeError(f"{game_dir} doesn't look like a Borderlands 4 install")
    report = _report_module(game_dir)
    log("Collecting versions, errors, logs and the newest crash...")
    path = report.build(game_dir, out_dir)
    log(f"Done. Report saved: {path}")
    return path, report.issue_url(path)


def show_report(path: Path) -> None:
    """Opens Explorer with the report selected."""
    # One string, not a list: subprocess quotes the whole "/select,<path with spaces>", and Explorer
    # ignores a quoted /select and opens Documents
    subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"', creationflags=subprocess.CREATE_NO_WINDOW)  # noqa: S603, S607


# endregion
# region Friends


def public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                ip = resp.read().decode().strip()
            if shared.is_ipv4(ip):
                return ip
        except OSError:
            continue
    raise RuntimeError("couldn't look up your public IP")


def lan_ip() -> str | None:
    import socket  # noqa: PLC0415

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


# endregion
