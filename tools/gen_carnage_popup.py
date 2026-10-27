"""
Regenerates the HUD page shipped in mods/bl4_damage_meter/popup_page/ (dev only).

    python tools/gen_carnage_popup.py --oodle <path to an oo2core_*_win64.dll>

The Carnage Meter's boss fight pop-up is drawn in the HUD page. Our HTML gets there through one
HUD widget the game always loads, the loot feed (loot_feed/loot_feed.html): this takes the copy of
that page the installed game uses, adds our loader script after the page's own, and writes:

- popup_page/loot_feed.html  the edited page, which the mod packs into its pak (4249)
- popup_page/vanilla.json    the SHA1 Unreal stores for the game's copy. The mod only installs its
                             pak while the game still ships exactly that copy, so a game patch that
                             touches the loot feed pauses the pop-up instead of undoing the patch.

The pop-up itself lives in popup_page/popup.js (a small fixed loader), popup_main.js and
popup.css: new loose files that replace nothing and reload themselves. The loader appends to the
HUD page's body, not inside the loot feed. Same arrangement as tools/gen_kairosdex_celebrate.py.

Needs an Oodle DLL for the same reason as tools/gen_item_cards.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mods" / "bl4_damage_meter" / "popup_page"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
PAGE_PATH = "OakGame/Content/uiresources/loot_feed/loot_feed.html"
# The page's own script; ours goes right after it
ANCHOR = '<script src="coui://uiresources/loot_feed/js/loot_feed.js"></script>'
OUR_TAG = '\n<script src="coui://uiresources/bl4carnage/popup.js"></script>'

sys.path.insert(0, str(REPO / "mods" / "bl4_damage_meter"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_damage_meter/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id
from gen_item_cards import extract  # noqa: E402


def edit(html: str) -> str:
    if html.count(ANCHOR) != 1:
        raise SystemExit(f"expected exactly one {ANCHOR!r} in the loot feed page")
    return html.replace(ANCHOR, ANCHOR + OUR_TAG)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    # Ignore paks in ~mods (ours included): the page has to come from the game itself
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    entry = pak.winning_entry(paks, PAGE_PATH)
    if entry is None:
        raise SystemExit(f"{PAGE_PATH} not found in {args.game}")

    vanilla = extract(entry, args.oodle).decode("utf-8")
    edited = edit(vanilla)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "loot_feed.html").write_text(edited, encoding="utf-8", newline="")
    fingerprint = {
        "path": PAGE_PATH,
        "sha1": entry.sha1,
        "source_pak": entry.pak.name,
        "game_build": shared.game_build_id(args.game),
    }
    (OUT_DIR / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")
    print(f"built from {entry.pak.name} ({entry.sha1}), {len(vanilla)} -> {len(edited)} bytes")


if __name__ == "__main__":
    main()
