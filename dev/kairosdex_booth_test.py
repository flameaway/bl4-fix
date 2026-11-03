"""
Checks Kairosdex's booth recipe file offline: tools/gen_booth_recipes.py (what goes into booth.jsonl)
and booth.read_recipes (what the mod takes from it). No game needed; writes only into a temp folder.

    python dev/kairosdex_booth_test.py [--recording booth.json]

--recording: a real recording (bake.start(..., record=True)): also generated, read back and
compared piece by piece with what booth.build would build from the recording itself.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_weapondex"
for name in ("unrealsdk", "unrealsdk.unreal"):
    stub = types.ModuleType(name)
    stub.BoundFunction = stub.UObject = stub.WeakPointer = object  # type: ignore[attr-defined]
    sys.modules[name] = stub
sys.modules["unrealsdk"].unreal = sys.modules["unrealsdk.unreal"]  # type: ignore[attr-defined]
pkg = types.ModuleType("kdx_booth_test")
pkg.__path__ = [str(MOD)]
sys.modules["kdx_booth_test"] = pkg
booth = importlib.import_module("kdx_booth_test.booth")

BUILD = "f38df04c-26e12000"
results: list[tuple[bool, str]] = []


def check(ok: object, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def generate(recording: dict, folder: Path, *extra: str) -> tuple[int, str, str]:
    src = folder / "rec.json"
    out = folder / "booth.jsonl"
    src.write_text(json.dumps(recording), encoding="utf-8")
    out.unlink(missing_ok=True)
    run = subprocess.run([sys.executable, str(REPO / "tools" / "gen_booth_recipes.py"), str(src), "--out", str(out), *extra],
                         capture_output=True, text=True, check=False)  # fmt: skip
    return run.returncode, run.stdout + run.stderr, out.read_text(encoding="utf-8") if out.exists() else ""


def piece(name: str, actor: str, **kw: object) -> dict:
    return {"name": name, "actor": actor, "kind": "static", "mesh": "/Game/X/SM_X.SM_X", "xf": [0, 0, 0, 0, 0, 0, 1], "mats": [], **kw}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording", type=Path)
    args = ap.parse_args()
    dex_ids = [b["id"] for b in json.loads((MOD / "dex.json").read_text(encoding="utf-8"))["bosses"]]
    a, b = dex_ids[0], dex_ids[1]
    recording = {
        a: {"v": 1, "build": BUILD, "bounds": [1, 2, 3, 4, 5, 6], "skipped": ["dev note"], "pieces": [
            piece("CharacterMesh", "OakCharacter", dbg={"x": 1}),
            piece("BuzzAxe", "OakProjectile"),  # held: kept
            piece("StaticMeshComponent", "OakProjectile"),  # thrown: out
            piece("Crystal", "OakInteractiveObject"),  # crystal: out
            piece("Gun", "OakWeapon")]},
        b: {"v": 1, "build": BUILD, "bounds": [0, 0, 0, 1, 1, 1], "pieces": [piece("CharacterMesh", "OakCharacter")]},
        "not-a-boss": {"v": 1, "build": BUILD, "pieces": []},
        dex_ids[2]: {"v": 99, "build": BUILD, "pieces": []},
    }
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        code, log, text = generate(recording, folder)
        check(code == 0 and text, f"generates ({log.strip().splitlines()[-1] if log.strip() else code})")
        lines = text.splitlines()
        header = json.loads(lines[0])
        check(header == {"format": booth.RECIPES_FORMAT, "build": BUILD, "bosses": 2}, f"header {header}")
        check(all(line.startswith('{"id":"') for line in lines[1:]), "every boss line starts with its id")
        check(b"\r\n" not in (folder / "booth.jsonl").read_bytes(), "LF line endings")
        got, why = booth.read_recipes(None, text=text, build=BUILD)
        check(sorted(got) == sorted([a, b]) and not why, f"reads both bosses ({sorted(got)}, {why!r})")
        names = [p["name"] for p in got[a]["pieces"]]
        check(names == ["CharacterMesh", "BuzzAxe", "Gun"], f"held weapon and gun kept, thrown and crystal out: {names}")
        check("dbg" not in got[a]["pieces"][0] and "skipped" not in got[a] and "build" not in got[a], "dev fields left out")
        check(got[a]["bounds"] == [1, 2, 3, 4, 5, 6], "bounds kept")
        check("not-a-boss" not in got and dex_ids[2] not in got, "unknown boss and other recipe version left out")
        only, _ = booth.read_recipes([b], text=text, build=BUILD)
        check(list(only) == [b], "reads only the bosses asked for")
        none, why = booth.read_recipes(None, text=text, build="00000000-1")
        check(not none and "need recording again" in why, f"another game build: nothing ({why})")
        none, why = booth.read_recipes(None, text='{"format":99}\n', build=BUILD)
        check(not none and "format" in why, "another file format: nothing")
        none, why = booth.read_recipes(None, text="", build=BUILD)
        check(not none and why, "empty file: nothing")
        mixed = {**recording, b: {**recording[b], "build": "00000000-1"}}
        code, log, _ = generate(mixed, folder)
        check(code != 0 and "builds" in log, "refuses a recording that mixes game builds")
        old = {k: {kk: vv for kk, vv in v.items() if kk != "build"} for k, v in recording.items()}
        code, _, _ = generate(old, folder)
        check(code != 0, "refuses a recording without builds unless --build is given")
        code, _, text = generate(old, folder, "--build", "f38df04c/26e12000")
        check(code == 0 and json.loads(text.splitlines()[0])["build"] == BUILD, "--build (game_pins.json's a/b form too)")

        if args.recording:
            real = json.loads(args.recording.read_text(encoding="utf-8"))
            builds = {r.get("build") for r in real.values()}
            extra = () if None not in builds else ("--build", BUILD)
            code, log, text = generate(real, folder, *extra)
            check(code == 0, f"real recording generates: {log.strip().splitlines()[-1] if log.strip() else code}")
            got, why = booth.read_recipes(None, text=text, build=json.loads(text.splitlines()[0])["build"])
            same = all([p for p in real[k]["pieces"] if not booth.skipped(p)] == [{x: y for x, y in p.items() if x != "dbg"} for p in got[k]["pieces"]]
                       and real[k].get("bounds") == got[k].get("bounds") for k in got)  # fmt: skip
            check(got and same, f"real recording: {len(got)} bosses read back, the same pieces as booth.build would use")

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
