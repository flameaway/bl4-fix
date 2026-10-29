"""
Re-pins the Skills page Easy Respec's script was checked with (dev only).

    python tools/gen_respec_page.py

The Skills menu is menu_skills/menu_skills.html inside the game's paks. Our script (respec.js, a
loader for respec_main.js and respec.css) runs in it through the BL4 UI Link since 0.2.0, without
replacing the page. This writes:

- skills_page/vanilla.json  the SHA1 Unreal stores for the game's copy. The mod only runs its script
                            while the game still ships exactly that copy, so a game patch that
                            touches the Skills page pauses the feature ("Needs update") until the
                            script is checked against the new page and this is run again.

Until 0.1.x this also wrote skills_page/menu_skills.html (the page + our tags) for a pak (4244).
--oodle is still accepted (tools/game_patch.py passes it) but not needed: only the pak index is read.
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
sys.path.insert(0, str(REPO / "mods" / "bl4_respec"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # mods/bl4_respec/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, help="not needed any more (only the pak index is read)")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    # Ignore paks in ~mods (ours included): the page has to come from the game itself
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    entry = pak.winning_entry(paks, PAGE_PATH)
    if entry is None:
        raise SystemExit(f"{PAGE_PATH} not found in {args.game}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = {
        "path": PAGE_PATH,
        "sha1": entry.sha1,
        "source_pak": entry.pak.name,
        "game_build": shared.game_build_id(args.game),
    }
    (OUT_DIR / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")
    print(f"pinned {PAGE_PATH} from {entry.pak.name} ({entry.sha1})")


if __name__ == "__main__":
    main()
