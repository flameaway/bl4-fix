"""
Regenerates the pages shipped in mods/bl4_instant_tabs/pages/ (dev only).

    python tools/gen_instant_tabs_pages.py --oodle <path to an oo2core_*_win64.dll>

Each ECHO menu page fades in when you switch to it. That fade is played by the menu page's own
JavaScript (ingame.html?view=view_status_menus), so skipping it needs a script of ours in that
page. Every ECHO menu page shares one JS context there, so it's enough for one of them to load our
script. We add it to the two pages the menu usually opens on: the inventory (Tab, I) and the map
(M). This takes the copies the installed game currently uses, adds our loader script after each
page's own, and writes:

- pages/<page>.html  the edited pages, which the mod packs into its own pak
- pages/vanilla.json the SHA1 Unreal stores for each of the game's copies. The mod only installs
                     its pak while the game still ships exactly those copies, so a game patch that
                     touches either page pauses the fade skip instead of undoing the patch (the
                     tab delay patch keeps working).

The fade skip itself lives in ui/loader.js and ui/fade.js: new loose files that replace nothing
and reload themselves, so only these pages need a restart to change.

Needs an Oodle DLL for the same reason as tools/gen_item_cards.py.
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
# Page -> the page's own script, which ours goes after
PAGES = {
    "menu_equip/menu_equip.html": '<script src="coui://uiresources/menu_equip/js/menu_equip.js"></script>',
    "menu_bigmap/menu_bigmap.html": '<script src="coui://uiresources/menu_bigmap/js/menu_bigmap.js"></script>',
}
OUR_TAGS = '\n<script src="coui://uiresources/bl4instanttabs/loader.js"></script>'

sys.path.insert(0, str(REPO / "mods" / "bl4_instant_tabs"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_instant_tabs/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id
from gen_item_cards import extract  # noqa: E402


def edit(html: str, anchor: str) -> str:
    if html.count(anchor) != 1:
        raise SystemExit(f"expected exactly one {anchor!r}")
    return html.replace(anchor, anchor + OUR_TAGS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    # Ignore paks in ~mods (ours included): the pages have to come from the game itself
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = {"game_build": shared.game_build_id(args.game), "pages": {}}
    for page, anchor in PAGES.items():
        entry = pak.winning_entry(paks, UI_PREFIX + page)
        if entry is None:
            raise SystemExit(f"{page} not found in {args.game}")
        vanilla = extract(entry, args.oodle).decode("utf-8")
        edited = edit(vanilla, anchor)
        (OUT_DIR / Path(page).name).write_text(edited, encoding="utf-8", newline="")
        fingerprint["pages"][page] = {"sha1": entry.sha1, "source_pak": entry.pak.name}
        print(f"{page}: from {entry.pak.name} ({entry.sha1}), {len(vanilla)} -> {len(edited)} bytes")
    (OUT_DIR / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
