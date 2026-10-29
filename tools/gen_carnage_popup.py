"""
Regenerates the pin of the HUD loot feed page in mods/bl4_damage_meter/popup_page/ (dev only).

    python tools/gen_carnage_popup.py --oodle <path to an oo2core_*_win64.dll>

The Carnage Meter's boss fight pop-up is drawn in the HUD page. Its script gets there through the
BL4 UI Link (no page replaced). Until 0.2.0 it came through one HUD widget the game always loads,
the loot feed (loot_feed/loot_feed.html), with this tool writing that page plus one <script> for
our pak 4249. The pin stayed: this reads the copy of that page the installed game uses and writes

- popup_page/vanilla.json    the SHA1 Unreal stores for the game's copy. The mod only runs the
                             pop-up while the game still ships exactly that copy, so a game patch
                             that touches the HUD's loot feed pauses the pop-up until it's checked.

The pop-up itself lives in popup_page/popup.js (a small fixed loader), popup_main.js and
popup.css: loose files that replace nothing and reload themselves. The loader appends to the HUD
page's body. Same arrangement as tools/gen_kairosdex_celebrate.py.

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

sys.path.insert(0, str(REPO / "mods" / "bl4_damage_meter"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_damage_meter/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id
from gen_item_cards import extract  # noqa: E402


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
    # Read it anyway: a page that no longer unpacks is a change worth stopping for
    vanilla = extract(entry, args.oodle)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = {
        "path": PAGE_PATH,
        "sha1": entry.sha1,
        "source_pak": entry.pak.name,
        "game_build": shared.game_build_id(args.game),
    }
    (OUT_DIR / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")
    print(f"pinned {entry.pak.name} ({entry.sha1}), {len(vanilla)} bytes")


if __name__ == "__main__":
    main()
