"""
Regenerates the pin of the HUD banner page in mods/bl4_weapondex/celebrate_page/ (dev only).

    python tools/gen_kairosdex_celebrate.py --oodle <path to an oo2core_*_win64.dll>

The game's big HUD banners (level up, mission complete, takedowns) are one widget,
celebratory_notifications/celebratory_notifications.html, loaded into the HUD page. The Kairosdex
shows its own banners (a boss beaten for the first time, a new legendary or pearl) in a copy of
that widget's markup, played with the widget's own animation. Its script gets into the HUD page
through the BL4 UI Link (no page replaced; until 0.7.0 this tool also wrote the page with one more
<script> for our pak 4248). This reads the copy of the page the installed game uses, checks the
markup our script copies is still there, and writes:

- celebrate_page/vanilla.json  the SHA1 Unreal stores for the game's copy. The mod only runs its
                               script while the game still ships exactly that copy, so a game
                               patch that touches the banners pauses ours instead of drawing a
                               broken copy.

Our behaviour lives in celebrate_page/celebrate.js (a small fixed loader), celebrate_main.js and
celebrate.css: loose files that replace nothing and reload themselves.

Needs an Oodle DLL for the same reason as tools/gen_item_cards.py (Cyberpunk 2077's
bin/x64/oo2ext_7_win64.dll works).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mods" / "bl4_weapondex" / "celebrate_page"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
PAGE_PATH = "OakGame/Content/uiresources/celebratory_notifications/celebratory_notifications.html"
# What celebrate_main.js copies: the widget's "special notification" banner
NEEDS = ('class="celeb_special_notif_cntr', "<widget-notification-message")

sys.path.insert(0, str(REPO / "mods" / "bl4_weapondex"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_weapondex/pak.py is plain Python, no SDK imports
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

    vanilla = extract(entry, args.oodle).decode("utf-8")
    missing = [n for n in NEEDS if n not in vanilla]
    if missing:
        raise SystemExit(f"the banner page lost what celebrate_main.js copies: {missing}")

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
