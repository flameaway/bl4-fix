"""
Regenerates the pages shipped in mods/bl4_mod_menu/pause_page/ and main_page/ (dev only).

    python tools/gen_mod_menu_page.py --oodle <path to an oo2core_*_win64.dll>

The pause menu is menu_pause/menu_pause.html inside the game's paks, and it loads into the game's
menu page (ingame.html?view=view_status_menus) like every other menu. This takes the copy the
installed game currently uses, adds our loader script after the page's own, and writes:

- pause_page/menu_pause.html  the edited page, which the mod packs into its own pak
- pause_page/vanilla.json     the SHA1 Unreal stores for the game's copy. The mod only installs its
                              pak while the game still ships exactly that copy, so a game patch
                              that touches the pause menu pauses the HTML menu instead of undoing
                              the patch (the basic UMG menu keeps working).

The title screen's main menu (menu_main/menu_main.html) gets the same treatment into main_page/,
with main_loader.js (MODS under Options, with a "!" when mod updates are waiting).

The menu itself lives in ui/loader.js, menu.js and menu.css: new loose files that replace nothing
and reload themselves, so only these pages need a restart to change.

Needs an Oodle DLL for the same reason as tools/gen_item_cards.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_mod_menu"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
# (output folder, page in the game's paks, the tag we add ours after, our tag)
PAGES = [
    (
        MOD / "pause_page",
        "OakGame/Content/uiresources/menu_pause/menu_pause.html",
        '<script src="coui://uiresources/menu_pause/js/menu_pause.js"></script>',
        '\n<script src="coui://uiresources/bl4modmenu/loader.js"></script>',
    ),
    (
        MOD / "main_page",
        "OakGame/Content/uiresources/menu_main/menu_main.html",
        '<script src="coui://uiresources/menu_main/js/menu_main.js"></script>',
        '\n<script src="coui://uiresources/bl4modmenu/main_loader.js"></script>',
    ),
]

sys.path.insert(0, str(REPO / "mods" / "bl4_mod_menu"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_mod_menu/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id
from gen_item_cards import extract  # noqa: E402


def edit(html: str, anchor: str, ours: str) -> str:
    if html.count(anchor) != 1:
        raise SystemExit(f"expected exactly one {anchor!r} in the page")
    return html.replace(anchor, anchor + ours)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    # Ignore paks in ~mods (ours included): the page has to come from the game itself
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    for out_dir, page_path, anchor, ours in PAGES:
        entry = pak.winning_entry(paks, page_path)
        if entry is None:
            raise SystemExit(f"{page_path} not found in {args.game}")

        vanilla = extract(entry, args.oodle).decode("utf-8")
        edited = edit(vanilla, anchor, ours)

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / Path(page_path).name).write_text(edited, encoding="utf-8", newline="")
        fingerprint = {
            "path": page_path,
            "sha1": entry.sha1,
            "source_pak": entry.pak.name,
            "game_build": shared.game_build_id(args.game),
        }
        (out_dir / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")
        print(f"{Path(page_path).name}: built from {entry.pak.name} ({entry.sha1}), {len(vanilla)} -> {len(edited)} bytes")

if __name__ == "__main__":
    main()
