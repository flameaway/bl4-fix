"""
Regenerates the Skills page shipped in mods/bl4_respec/skills_page/ (dev only).

    python tools/gen_respec_page.py --oodle <path to an oo2core_*_win64.dll>

The Skills menu is menu_skills/menu_skills.html inside the game's paks. This takes the copy the
installed game currently uses, adds our stylesheet and script after the page's own, and writes:

- skills_page/menu_skills.html  the edited page, which the mod packs into its own pak
- skills_page/vanilla.json      the SHA1 Unreal stores for the game's copy. The mod only installs its
                                pak while the game still ships exactly that copy, so a game patch
                                that touches the Skills page pauses the feature instead of undoing
                                the patch.

Our behaviour lives in skills_page/respec.js (a loader), respec_main.js and respec.css: new files,
written loose by the mod, that replace nothing.

Needs an Oodle DLL for the same reason as tools/gen_item_cards.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mods" / "bl4_respec" / "skills_page"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
PAGE_PATH = "OakGame/Content/uiresources/menu_skills/menu_skills.html"
# The page's own script; ours has to run after it so Widget_Skills exists
ANCHOR = '<script src="coui://uiresources/menu_skills/js/menu_skills.js"></script>'
OUR_TAGS = (
    '\n<link href="coui://uiresources/bl4respec/respec.css"\n'
    '      type="text/css"\n'
    '      rel="stylesheet" />\n'
    '<script src="coui://uiresources/bl4respec/respec.js"></script>'
)

sys.path.insert(0, str(REPO / "mods" / "bl4_respec"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_respec/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id
from gen_item_cards import extract  # noqa: E402


def edit(html: str) -> str:
    if html.count(ANCHOR) != 1:
        raise SystemExit(f"expected exactly one {ANCHOR!r} in the Skills page")
    return html.replace(ANCHOR, ANCHOR + OUR_TAGS)


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
    (OUT_DIR / "menu_skills.html").write_text(edited, encoding="utf-8", newline="")
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
