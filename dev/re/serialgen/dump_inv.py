"""dump_inv.py <oo2core_*_win64.dll>: every inv NCS entry (all chunks) -> inv_entries.json next to this file."""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
import gen_item_cards as G  # noqa: E402

OODLE = Path(sys.argv[1])
paks = sorted((G.DEFAULT_GAME / "OakGame" / "Content" / "Paks").glob("*.pak"))
out = Path(__file__).with_name("inv_entries.json")
entries, _ = G._entries(paks, "inv", OODLE)
out.write_text(json.dumps(entries), encoding="utf-8")
print(len(entries), "->", out)
