# Records every boss's booth recipe for tools/gen_booth_recipes.py (dev machine only: after a game
# patch, docs/game-patch.md):
#   python tools/bridge.py <id> -f dev/record_booth.py
# Spawns each boss once, 200 m above your character (sky mode: out of sight), records it and takes
# its picture, all into <repo>/.bridge/booth_recording/ (booth.json + PNGs; nothing in the game's
# picture folder). Needs a character loaded and the loaded Kairosdex to have booth.py (the repo copy:
# dev/mod_from_dev.py). Spawning can crash a game: don't run it in a game someone else is using.
# Done when the SDK log says "booth recipes: N recorded"; then
#   python tools/gen_booth_recipes.py .bridge/booth_recording/booth.json
# Stop early: python tools/bridge.py <id> "import sys; sys.modules['bl4_weapondex.bake'].stop('stopped')"
import sys
from pathlib import Path

from mods_base import get_pc

# The repo: REPO set in the bridge namespace first, else where this checkout usually lives
REPO = Path(globals().get("REPO") or Path.home() / "Documents" / "projects" / "bl4-direct")
OUT = REPO / ".bridge" / "booth_recording"


def _bl4_record_booth() -> None:
    kdx = sys.modules.get("bl4_weapondex")
    bake = sys.modules.get("bl4_weapondex.bake")
    if kdx is None or bake is None or not hasattr(bake, "booth"):
        print("The loaded Kairosdex has no booth: load the repo copy first (dev/mod_from_dev.py).")
        return
    pc = get_pc()
    if pc is None or pc.Pawn is None:
        print("Load a character first: the bosses spawn above it.")
        return
    if bake.running():
        print("A bake is already running.")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    # Each run merges into booth.json: start empty, so one recording never mixes game builds
    (OUT / "booth.json").unlink(missing_ok=True)
    ids = [b["id"] for b in bake.data.dex()["bosses"]]
    # The mod's own frame tick drives the bake (bl4_weapondex's frametick calls bake.tick)
    print(len(ids), "bosses:", bake.start(kdx.GAME_DIR, ids, ground=False, record=True, out_dir=OUT))
    print("Recording into", OUT)


try:
    _bl4_record_booth()
finally:
    del _bl4_record_booth
