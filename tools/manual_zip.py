"""
The release's manual install: BL4Fix-Manual.zip (a fixed name, so README links to
releases/latest/download/BL4Fix-Manual.zip work), for players who'd rather not run the setup app.
Extracted into the game folder it gives what the setup app's Install gives with every mod ticked
(dev only; tools/release.py calls build()):

- the pinned Python SDK (installer/build.py SDK_URL / SDK_SHA256), without its sdk_mods/settings/
  (a re-extract never touches the player's settings; the setup app skips it the same way)
- sdk_mods/<id>.sdkmod for every mod in installer/catalog.py (the release's own files)
- the BL4 UI Link pak (Paks/~mods/BL4Fix_UILink_4251_P.pak) and the page scripts the game's pages
  load at startup (core.PAGE_SCRIPTS: bl4link/, bl4modmenu/, bl4instanttabs/). The setup app
  writes these before the first launch (core.prepare_pages), because paks only load at startup:
  without them a fresh install's first launch has no mod pages. The pak is the same bytes every
  link mod builds (uilink.build_pak), so the mods in game find it current.
- BL4Fix-README.txt: the steps, also in README.md ("Install without the setup app")

What the setup app does that the zip can't, and who does it instead:
- `"enabled": true` for every new mod (mods_base loads a mod with no settings file switched off):
  the Mod Menu at its import (bl4_mod_menu/firstrun.py)
- old page paks removed, Skip Intro's movies moved, Kairosdex's pictures: the mods in game (the
  pictures download once on the first launch, bl4_weapondex/fetch.py)
- the firewall rule (off by default in the app too): BL4 Direct Connect's F6 page
- the VC++ runtime, old loose mod folders, another dsound.dll loader: README steps

Deterministic (sorted names, fixed dates): the same commit and SDK give the same zip, so the GitHub
Actions rebuild (.github/workflows/verify-release.yml) can attest it.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for extra in (REPO_ROOT / "installer", REPO_ROOT / "mods" / "bl4_direct", REPO_ROOT / "mods" / "bl4_skip_intro"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import build as installer_build  # noqa: E402  # installer/build.py
import catalog  # noqa: E402
import core  # noqa: E402  # installer/core.py: PAGE_SCRIPTS and the page module loader

UI_REL = "OakGame/Content/uiresources"
PAKS_REL = "OakGame/Content/Paks/~mods"
README_NAME = "BL4Fix-README.txt"
REPO_URL = "https://github.com/flameaway/bl4-fix"


def zip_name(_tag: str = "") -> str:
    return "BL4Fix-Manual.zip"


def readme(tag: str) -> str:
    mods = "\n".join(f"  - {m.name}: {m.blurb}" for m in catalog.MODS)
    return f"""BL4 Fix {tag}: manual install (no setup app)

INSTALL
1. Close Borderlands 4.
2. Extract everything in this zip into the Borderlands 4 game folder, the one with OakGame in it
   (Steam: right-click the game > Manage > Browse local files). Say yes to replacing files.
3. Start the game. Every mod is on; F7 (or MODS in the pause menu) has their settings, where you
   can switch off the ones you don't want. F6 is BL4 Direct Connect (co-op).

Kairosdex downloads its boss and item pictures (~46 MB) from the release on the first launch.

IF NOTHING SHOWS IN GAME
- Install Microsoft's Visual C++ runtime (x64): https://aka.ms/vs/17/release/vc_redist.x64.exe
  The Python SDK needs a recent one; most PCs have it from other games.
- Another mod loader in the game folder (OakGame/Binaries/Win64/dsound.dll from something else)
  fights with the SDK: remove it.
- Old copies of these mods as folders (sdk_mods/bl4_<name>/) win over the .sdkmod files: delete
  those folders.

UPDATES
In game: MODS > Updates (it checks GitHub once per launch after you say yes).
Or download a newer zip and extract it the same way (your settings are kept).

UNINSTALL (game closed)
In game first: switch off Skip Intro (puts the logo movies back).
Then delete sdk_mods/bl4_*.sdkmod, {PAKS_REL}/{catalog.LINK_PAK}
and the bl4* folders in {UI_REL}/. Settings stay in sdk_mods/settings/.
To remove the SDK too: OakGame/Binaries/Win64/dsound.dll and OakGame/Binaries/Win64/Plugins/.

CHECK THE DOWNLOAD
SHA256SUMS.txt on the release page lists every file's SHA-256. With the GitHub CLI:
  gh attestation verify {zip_name(tag)} --repo flameaway/bl4-fix
shows GitHub itself rebuilt this zip from the tagged source.

THE MODS
{mods}

Source, issues and the setup app: {REPO_URL}
"""


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, installer_build.ZIP_DATE)
    info.external_attr = 0o644 << 16
    return info


def page_files(sdkmods: Path) -> dict[str, bytes]:
    """The UI Link pak and the startup page scripts, as zip paths -> bytes."""
    link_mods = [m.id for m in catalog.MODS if m.link]
    wanted = sorted({*link_mods, *(m for m, _, _ in core.PAGE_SCRIPTS)})
    files: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for mod_id in wanted:
            with zipfile.ZipFile(sdkmods / f"{mod_id}.sdkmod") as zf:
                zf.extractall(root)
        uilink = core._load_page_module(root, link_mods[0], "uilink")  # noqa: SLF001
        if uilink.PAK_NAME != catalog.LINK_PAK:
            raise SystemExit(f"uilink.PAK_NAME {uilink.PAK_NAME} != catalog.LINK_PAK {catalog.LINK_PAK}")
        files[f"{PAKS_REL}/{uilink.PAK_NAME}"] = uilink.build_pak()
        for mod_id, folder, ui_dir in core.PAGE_SCRIPTS:
            for path in sorted((root / mod_id / folder).glob("*")):
                if not path.is_file():
                    continue
                name = f"{UI_REL}/{ui_dir}/{path.name}"
                data = path.read_bytes()
                if files.setdefault(name, data) != data:
                    # Shared packages are byte-identical in every mod (tools/sync_shared.py)
                    raise SystemExit(f"{mod_id} carries a different {name} than another mod: run tools/sync_shared.py")
        for alias in [n for n in sys.modules if n.startswith("_bl4setup_")]:
            del sys.modules[alias]
    return files


def build(out: Path, sdkmods: Path, sdk_zip: Path, tag: str) -> Path:
    """Writes the zip (`out` is its path). `sdkmods` holds the release's .sdkmod files."""
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(sdk_zip) as zf:
        for item in zf.infolist():
            if item.is_dir() or item.filename.startswith("sdk_mods/settings/"):
                continue
            files[item.filename] = zf.read(item)
    for info in catalog.MODS:
        files[f"sdk_mods/{info.id}.sdkmod"] = (sdkmods / f"{info.id}.sdkmod").read_bytes()
    files |= page_files(sdkmods)
    files[README_NAME] = readme(tag).replace("\n", "\r\n").encode("utf-8")  # Notepad-friendly
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        for name in sorted(files):
            entry = _entry(name)
            # .sdkmod files are zips already
            entry.compress_type = zipfile.ZIP_STORED if name.endswith(".sdkmod") else zipfile.ZIP_DEFLATED
            zf.writestr(entry, files[name])
    tmp.replace(out)
    return out
