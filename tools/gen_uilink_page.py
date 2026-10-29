"""
Regenerates shared/uilink/page/ and copies it into the mods that carry it (dev only).

    python tools/gen_uilink_page.py --oodle <path to an oo2core_*_win64.dll>

The BL4 UI Link's pak holds one file: the game's own `_shared/js/debug.js` (console helpers; the
last script of ingame.html, the root page of every game screen) plus one line that loads our
loose `uiresources/bl4link/boot.js`. This takes the copy the installed game currently uses and
writes:

- page/debug.js       the game's file + our line
- page/vanilla.json   the SHA1 Unreal stores for the game's copy. The link only installs its pak
                      while the game still ships exactly that copy, so a game patch that touches
                      it pauses the link instead of undoing the patch.

Needs an Oodle DLL for the same reason as tools/gen_item_cards.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "shared" / "uilink" / "page"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
PATH = "OakGame/Content/uiresources/_shared/js/debug.js"
# One line, after the game's code: a script element for our loose loader. Nothing else ever goes
# in the pak (a pak entry can't change while the game runs; boot.js and hub.js can).
OURS = (
    '\r\n;(function () { try { var s = document.createElement("script"); '
    's.src = "coui://uiresources/bl4link/boot.js"; (document.head || document.documentElement).appendChild(s); '
    "} catch (err) {} })();\r\n"
)

sys.path.insert(0, str(REPO / "shared" / "uilink"))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402  # shared/uilink/pak.py is plain Python, no SDK imports
import shared  # noqa: E402  # mods/bl4_direct/shared.py, for the build id
from gen_item_cards import extract  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, help="path to an oo2core_*_win64.dll (default: pyooz)")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    # Ignore paks in ~mods (ours included): the file has to come from the game itself
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    entry = pak.winning_entry(paks, PATH)
    if entry is None:
        raise SystemExit(f"{PATH} not found in {args.game}")
    vanilla = extract(entry, args.oodle)
    if b"bl4link" in vanilla:
        raise SystemExit("the game's copy already holds our line: is a ~mods pak in the list?")
    edited = vanilla + OURS.encode("utf-8")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "debug.js").write_bytes(edited)
    fingerprint = {
        "path": PATH,
        "sha1": entry.sha1,
        "source_pak": entry.pak.name,
        "game_build": shared.game_build_id(args.game),
    }
    (OUT / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")
    print(f"debug.js: built from {entry.pak.name} ({entry.sha1}), {len(vanilla)} -> {len(edited)} bytes")
    # Every mod that carries the link gets the new copy
    import sync_shared  # noqa: PLC0415

    sys.argv = [sys.argv[0]]
    sync_shared.main()


if __name__ == "__main__":
    main()
