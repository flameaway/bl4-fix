"""
Re-pins the ECHO menu pages Instant Menu Tabs' fade skip was checked with (dev only).

    python tools/gen_instant_tabs_pages.py

Each ECHO menu page fades in when you switch to it. That fade is played by the menu page's own
JavaScript (ingame.html?view=view_status_menus), where our fade skip (ui/loader.js + ui/fade.js)
runs through the BL4 UI Link since 0.2.0, without replacing any page. This writes:

- pages/vanilla.json the SHA1 Unreal stores for the game's copies of the inventory and map pages.
                     The mod only runs the fade skip while the game still ships exactly those
                     copies, so a game patch that touches either pauses it ("Needs update"; the
                     tab delay patch keeps working) until it's checked and this is run again.

Until 0.1.x this also wrote pages/menu_equip.html and pages/menu_bigmap.html (each page + our
loader tag) for a pak (4246). --oodle is still accepted (tools/game_patch.py passes it) but not
needed: only the pak indexes are read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mods" / "bl4_instant_tabs" / "pages"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
UI_PREFIX = "OakGame/Content/uiresources/"
# The pages the fade skip was checked with
PAGES = ("menu_equip/menu_equip.html", "menu_bigmap/menu_bigmap.html")

sys.path.insert(0, str(REPO / "mods" / "bl4_instant_tabs"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_instant_tabs/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, help="not needed any more (only the pak indexes are read)")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    # Ignore paks in ~mods (ours included): the pages have to come from the game itself
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = {"game_build": shared.game_build_id(args.game), "pages": {}}
    for page in PAGES:
        entry = pak.winning_entry(paks, UI_PREFIX + page)
        if entry is None:
            raise SystemExit(f"{page} not found in {args.game}")
        fingerprint["pages"][page] = {"sha1": entry.sha1, "source_pak": entry.pak.name}
        print(f"{page}: pinned from {entry.pak.name} ({entry.sha1})")
    (OUT_DIR / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
