"""
Install / status / uninstall logic for BL4 Fix, shared by the GUI and the CLI.

Payload files (the pinned SDK zip and the built .sdkmod files) are bundled next to this module by the
build script, under `payload/`.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import winreg
import zipfile
from dataclasses import dataclass
from pathlib import Path

import catalog
import firewall

if getattr(sys, "frozen", False):
    PAYLOAD = Path(sys._MEIPASS) / "payload"  # type: ignore[attr-defined]  # noqa: SLF001
else:
    PAYLOAD = Path(__file__).resolve().parent / "build" / "payload"

sys.path.insert(0, str(PAYLOAD))
import movies  # noqa: E402  # bundled copy of mods/bl4_skip_intro/movies.py
import shared  # noqa: E402  # bundled copy of mods/bl4_direct/shared.py

SDK_ZIP = PAYLOAD / "oak2-sdk.zip"
SDK_ZIP_SHA256 = "602675446abed184169fa158be3c8bc81777a71203581e4a248eca8a3d00b5c7"
MOD_FILE = PAYLOAD / "bl4_direct.sdkmod"
# Skip Intro: the 2K / Gearbox logo movies moved aside. Its checkbox is the mod's enabled flag
# (which the in-game mod follows too, so it re-applies after a Steam file check)
SKIP_INTRO = "bl4_skip_intro"
# Where mods put the shim paks and loose UI files they write themselves (catalog.ModInfo)
MOD_PAKS_REL = Path("OakGame/Content/Paks/~mods")
UI_REL = Path("OakGame/Content/uiresources")
# A Start menu entry for the copy of this app in the game folder, to add or remove mods later
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
# Dot-folders in sdk_mods are skipped by the SDK's mod loader
TOOLS_DIR_REL = Path("sdk_mods/.bl4_direct")


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
    return shared.Config.load(friends_file(game_dir))


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
            f"{mark(self.firewall is not None)}Firewall:     {self.firewall or 'no rule (hosted games reachable by anyone who can reach the port)'}",
            f"Skip intro:   {self._intro_line()}",
            f"Mods:         {self._mods_line()}",
            *(f"!! {c}" for c in self.conflicts),
            f"Compatibility: {shared.compat_code(self.game_dir)}",
        ]


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
        log("Installing Microsoft Visual C++ runtime...")
        res = subprocess.run([str(exe), "/install", "/quiet", "/norestart"], check=False)
        # 3010 = success, reboot required; 1638 = newer version already installed
        if res.returncode not in (0, 3010, 1638):
            raise RuntimeError(f"VC++ runtime installer failed with code {res.returncode}")


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
) -> None:
    """
    Installs/repairs everything, and adds/removes optional mods. Needs admin (VC++ runtime and
    firewall).

    mods: the mods to have installed (required ones are added); None keeps what's installed (the
    defaults on a first install). skip_intro: the old Skip Intro switch, applied on top.
    """
    if not is_game_dir(game_dir):
        raise RuntimeError(f"{game_dir} doesn't look like a Borderlands 4 install")
    if game_running():
        raise RuntimeError("Close Borderlands 4 first.")
    _verify_payload()
    if conflicts := find_conflicts(game_dir):
        raise RuntimeError("\n".join(conflicts))
    chosen = choose_mods(game_dir, mods, skip_intro)

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

    # A copy of this app inside the game folder, so the in-game panel can ask it (with a UAC prompt)
    # to update the firewall when friends are added in game, and to add or remove mods later
    if getattr(sys, "frozen", False):
        tools = game_dir / TOOLS_DIR_REL
        tools.mkdir(parents=True, exist_ok=True)
        # Still named BL4DirectSetup.exe in the game folder: BL4 Direct Connect (its firewall button)
        # and the Mod Menu's updater look for it there, older versions included
        target = tools / "BL4DirectSetup.exe"
        version_file = tools / "version.txt"  # the in-game updater compares it with the latest release
        try:
            copied = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            copied = ""
        if Path(sys.executable).resolve() != target.resolve() and not (
            target.exists() and _version_key(copied) > _version_key(shared.MOD_VERSION)
        ):
            shutil.copyfile(sys.executable, target)
            version_file.write_text(shared.MOD_VERSION, encoding="utf-8")
        _make_shortcut(target, log)

    config = load_config(game_dir)
    if not friends_file(game_dir).exists():
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
    config = load_config(game_dir)
    ips = list(dict.fromkeys(f.address for f in config.friends if shared.is_ipv4(f.address)))
    firewall.apply_rule(config.host_port, ips, config.allow_lan)
    config.firewall_applied = config.firewall_signature()
    config.save(friends_file(game_dir))
    log(
        f"Firewall: UDP {config.host_port} only accepts "
        + ", ".join(["this PC", *(["your local network/VPN"] if config.allow_lan else []), *ips]),
    )


def uninstall(game_dir: Path, remove_sdk: bool, log=print) -> None:  # noqa: ANN001
    if game_running():
        raise RuntimeError("Close Borderlands 4 first.")
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
    firewall.remove_rule()
    log("Removed BL4 Fix: its mods and the firewall rule (your friends list and mod settings are kept).")

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
    ("bl4_mod_menu", "pause_page", False),
    ("bl4_mod_menu", "main_page", False),
    ("bl4_item_cards", "card_page", True),
    ("bl4_respec", "skills_page", True),
    ("bl4_instant_tabs", "pages", False),
    ("bl4_weapondex", "celebrate_page", True),
    ("bl4_damage_meter", "popup_page", True),
)
# Loose scripts those pages load at once, which these mods publish from code that only runs in
# game (it imports the SDK): mod id -> (folder in the .sdkmod, folder under uiresources). Item
# Cards and Easy Respec publish theirs from their page module's sync().
PAGE_SCRIPTS: dict[str, tuple[str, str]] = {
    "bl4_mod_menu": ("ui", "bl4modmenu"),
    "bl4_instant_tabs": ("ui", "bl4instanttabs"),
}


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
            log(f"{catalog.BY_ID[mod_id].name}: {status}")
        for mod_id, (folder, ui_dir) in PAGE_SCRIPTS.items():
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
