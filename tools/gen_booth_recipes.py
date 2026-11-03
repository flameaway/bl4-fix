"""
Writes mods/bl4_weapondex/booth.jsonl, the boss recipes Kairosdex's booth builds its pictures from
(mods/bl4_weapondex/booth.py), from a recording made in game (dev only).

    python tools/gen_booth_recipes.py RECORDING [--build STAMP-SIZE] [--out FILE]

RECORDING: the booth.json a recording bake writes next to its pictures (bake.start(...,
record=True): every boss spawned once, high above the character). Recipes hold asset paths and
numbers only (no game images).

The file is a header line, then one boss a line, so the mod parses only the bosses it needs:
    {"format": 1, "build": "<exe PE stamp>-<image size>", "bosses": N}
    {"id": "<boss id>", "v": 1, "pieces": [...], "bounds": [...]}
Pieces the booth never builds (booth.skipped: crystals, thrown things) and dev fields are left
out. The mod uses the file only on the game build it was recorded on (booth.read_recipes): after
a patch, record again (a recording bake in game) and run this.

--build: the build the recording was made on, for recordings older than the per-recipe "build"
field (2026-11-02). Default: the recipes' own; it refuses a recording that mixes builds.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_weapondex"
OUT = MOD / "booth.jsonl"
DEV_FIELDS = ("dbg",)  # per piece
RECIPE_DEV_FIELDS = ("skipped", "build")  # per recipe (build goes in the header)


def _booth() -> types.ModuleType:
    """booth.py with stand-in unrealsdk modules (only its pure parts are used: skipped, constants)."""
    for name in ("unrealsdk", "unrealsdk.unreal"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.BoundFunction = stub.UObject = stub.WeakPointer = object  # type: ignore[attr-defined]
            sys.modules[name] = stub
    sys.modules["unrealsdk"].unreal = sys.modules["unrealsdk.unreal"]  # type: ignore[attr-defined]
    pkg = types.ModuleType("kdx_gen")
    pkg.__path__ = [str(MOD)]
    sys.modules["kdx_gen"] = pkg
    return importlib.import_module("kdx_gen.booth")


def build(recording: dict[str, Any], game_build: str | None) -> tuple[list[str], list[str]]:
    """The file's lines and notes for the log."""
    booth = _booth()
    notes = []
    builds = {r.get("build") for r in recording.values() if isinstance(r, dict) and r.get("build")}
    if game_build is None:
        if len(builds) != 1:
            raise SystemExit(f"The recording names {len(builds)} game builds ({sorted(builds)}): pass --build")
        game_build = builds.pop()
    elif builds - {game_build}:
        raise SystemExit(f"--build {game_build}, but the recording names {sorted(builds)}")
    dex = json.loads((MOD / "dex.json").read_text(encoding="utf-8"))
    known = {b["id"] for b in dex["bosses"]}
    lines = []
    dropped = 0
    for boss_id in sorted(recording):
        recipe = recording[boss_id]
        if not isinstance(recipe, dict) or recipe.get("v") != booth.RECIPE_VERSION:
            notes.append(f"skipped {boss_id}: recipe version {recipe.get('v') if isinstance(recipe, dict) else '?'}")
            continue
        if boss_id not in known:
            notes.append(f"skipped {boss_id}: not in dex.json")
            continue
        pieces = []
        for piece in recipe.get("pieces", []):
            if booth.skipped(piece):
                dropped += 1
                continue
            pieces.append({k: v for k, v in piece.items() if k not in DEV_FIELDS})
        out = {"id": boss_id, **{k: v for k, v in recipe.items() if k not in RECIPE_DEV_FIELDS}, "pieces": pieces}
        lines.append(json.dumps(out, separators=(",", ":")))
    missing = sorted(known - set(recording))
    if missing:
        notes.append(f"{len(missing)} boss(es) in dex.json have no recipe: {', '.join(missing)}")
    notes.append(f"{len(lines)} bosses, {dropped} pieces left out (booth.skipped), game build {game_build}")
    header = json.dumps({"format": booth.RECIPES_FORMAT, "build": game_build, "bosses": len(lines)}, separators=(",", ":"))
    return [header, *lines], notes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path)
    ap.add_argument("--build", help="the game build the recording was made on (exe PE stamp-size, as in game_pins.json)")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    recording = json.loads(args.recording.read_text(encoding="utf-8"))
    lines, notes = build(recording, args.build.replace("/", "-") if args.build else None)
    text = "\n".join(lines) + "\n"
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))  # LF, whatever the platform
    tmp.replace(args.out)
    for note in notes:
        print(note)
    print(f"wrote {args.out} ({len(text) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
