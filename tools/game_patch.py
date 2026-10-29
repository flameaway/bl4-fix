"""
A game patch came out: what it changed for our mods, regenerate what it hit, check a running game.

    python tools/game_patch.py check                 what the installed game changed (no game needed)
    python tools/game_patch.py regen [--all | GEN...] rerun generators in dependency order (default:
                                                     the stale ones and what depends on them)
    python tools/game_patch.py baseline              regenerate everything into a scratch copy: records
                                                     tools/game_pins.json and diffs against the repo
    python tools/game_patch.py live <bridge id>      a running game: each mod's native parts, hook
                                                     targets, and patch symptoms in the SDK log
    python tools/game_patch.py log [unrealsdk.log]   patch symptoms in a log (a player's too)

The runbook is docs/game-patch.md. What this knows:

- GENERATORS: every tools/gen_*.py that turns game data into a file a mod ships, in run order
  (a tool comes after the ones whose output it reads), and what players get while its output is
  stale. This is the one list of them; add a new generator here.
- tools/game_pins.json (the baseline): per generator, every game file it looked up in the paks with
  the SHA1 the pak index stores (null for a path that wasn't there, so a new NCS chunk shows up),
  recorded while it ran (pak.winning_entry is wrapped). Plus the build: Steam build id, exe size
  and PE stamp. Native code (RVAs, byte patterns) is pinned to the exe; `check` can only say the
  exe changed, `live` says which finders still match.
- Runtime pins: the vanilla.json / source_data SHA1s the mods compare at startup. `check` shows which
  mods are stepping aside on players' machines right now, and which ship data nothing checks.

Generators decompress with Oodle: --oodle <dll>, else $BL4_OODLE, else a DLL from another installed
game (Cyberpunk 2077's bin/x64/oo2ext_7_win64.dll works).
"""

from __future__ import annotations

import argparse
import builtins
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
PINS = TOOLS / "game_pins.json"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
EXE_REL = Path("OakGame/Binaries/Win64/Borderlands4.exe")
LOG_REL = Path("OakGame/Binaries/Win64/Plugins/unrealsdk.log")
STEAM_APP_ID = "1285190"
# DLLs other games ship that decompress BL4's Kraken / Leviathan blocks
OODLE_CANDIDATES = (
    "Cyberpunk 2077/bin/x64/oo2ext_7_win64.dll",
)


@dataclass(frozen=True)
class Gen:
    tool: str  # tools/<tool>.py
    mods: tuple[str, ...]  # the mods whose files it writes
    stale: str  # what players get while its output is stale (after a patch, before our update)
    needs: tuple[str, ...] = ()  # generators whose output it reads: run them first
    oodle: bool = True  # takes --oodle
    dev: bool = False  # output isn't shipped


# Run order. A tool's `needs` must come before it.
GENERATORS: tuple[Gen, ...] = (
    Gen("gen_uilink_page", ("bl4_builds", "bl4_tidy_pause", "bl4_reroll", "bl4_uvh_teleport", "bl4_damage_meter",
                            "bl4_weapondex", "bl4_mod_menu", "bl4_item_cards", "bl4_respec", "bl4_instant_tabs"),
        "debug.js changed: the BL4 UI Link steps aside (its pak removed after exit): Build Presets' squares, "
        "Tidy Pause, Reroll's card keys, UVH Teleport's prompt, the Carnage Report and its pop-up, the Kairosdex screen and "
        "banners, detailed item cards, Easy Respec, the fade skip and MODS (F7 still opens the basic menu) go quiet "
        "(menu-page scripts through the Mod Menu's link if it's installed), and they show 'Needs update'."),
    Gen("gen_respec_page", ("bl4_respec",),
        "Page changed: Easy Respec steps aside (its link script isn't registered; 'Needs update')."),
    Gen("gen_carnage_popup", ("bl4_damage_meter",),
        "Page changed: the boss fight pop-up steps aside (its link script); the F9 screen still works."),
    Gen("gen_kairosdex_celebrate", ("bl4_weapondex",),
        "Page changed: first-kill / new-legendary banners step aside (their link script)."),
    Gen("gen_instant_tabs_pages", ("bl4_instant_tabs",),
        "Page changed: the fade skip steps aside (its link script); the code patches are separate (see live)."),
    Gen("gen_item_cards", ("bl4_item_cards",),
        "Page changed: detailed cards step aside (their link script). Data changed: skill numbers / firmware / drop "
        "sources switch off one by one. encore.json (Big Encore travel) is NOT checked: stale spots are used."),
    Gen("gen_weapondex", ("bl4_weapondex",),
        "dex.json changed: the dex says 'chances may be off', nothing is disabled. stats.json / rolls.json "
        "aren't pinned on their own (stats also reads progress_graph, uitooltipdata, skilltrees_data, Resident)."),
    Gen("gen_weapondex_missions", ("bl4_weapondex",),
        "NOT checked: save backfill may miss or wrongly credit bosses whose missions changed.",
        needs=("gen_weapondex",)),
    Gen("gen_weapondex_travel", ("bl4_weapondex",),
        "Pinned but NOT checked: Waypoint / Travel chips use stale spots.", needs=("gen_weapondex",)),
    Gen("gen_reroll_parts", ("bl4_reroll",),
        "Data changed: every reroll is blocked ('Reroll needs an update for this game version')."),
    Gen("gen_reroll_legendaries", ("bl4_reroll",),
        "Data changed: every reroll is blocked (same check as parts.json).", needs=("gen_weapondex",)),
    Gen("gen_damage_meter", ("bl4_damage_meter",),
        "Data changed: legendary names fall back to manufacturer + kind (logged)."),
    Gen("gen_uvh_teleport", ("bl4_uvh_teleport",),
        "Pinned but NOT checked: stale boss / station data teleports to the wrong place or the game refuses."),
    Gen("gen_instant_travel", ("bl4_instant_travel",),
        "Data changed: fast travel goes through the normal portal (logged)."),
    Gen("gen_minimap", ("bl4_minimap",),
        "Pinned but NOT checked: a changed map def misplaces the map art; a new map shows no minimap."),
    Gen("gen_builds_trees", ("bl4_builds",),
        "Not pinned (display only): tree names / colours in the presets may be off."),
    Gen("gen_loot_pools", ("bl4_loot_spawner",), "Dev only.", dev=True),
)
BY_NAME = {g.tool: g for g in GENERATORS}


# region Game build


def build_info(game: Path) -> dict:
    """Steam build id, exe size and PE stamp (TimeDateStamp + SizeOfImage, read from the header on disk)."""
    info: dict = {"steam_build": None, "exe_size": None, "pe_stamp": None}
    manifest = game.parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    try:
        if match := re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8")):
            info["steam_build"] = match.group(1)
    except OSError:
        pass
    exe = game / EXE_REL
    try:
        info["exe_size"] = exe.stat().st_size
        with exe.open("rb") as file:
            head = file.read(4096)
        (pe,) = struct.unpack_from("<I", head, 0x3C)
        if head[pe:pe + 4] == b"PE\0\0":
            (stamp,) = struct.unpack_from("<I", head, pe + 8)
            (image,) = struct.unpack_from("<I", head, pe + 0x18 + 0x38)
            info["pe_stamp"] = f"{stamp:08x}/{image:x}"
    except (OSError, struct.error):
        pass
    return info


# endregion
# region Pak lookups


def _pak_module():
    sys.path.insert(0, str(REPO / "mods" / "bl4_item_cards"))
    import pak  # noqa: PLC0415  # plain Python, no SDK imports
    return pak


def game_paks(game: Path) -> list[Path]:
    # What the mods and generators look at: the base folder only (~mods is ours / other mods)
    return sorted((game / "OakGame" / "Content" / "Paks").glob("*.pak"))


def current_sha1s(game: Path, paths: list[str]) -> dict[str, str | None]:
    pak = _pak_module()
    paks = game_paks(game)
    out: dict[str, str | None] = {}
    for path in paths:
        entry = pak.winning_entry(paks, path)
        out[path] = entry.sha1 if entry is not None else None
    return out


# endregion
# region Runtime pins (what the mods compare at startup)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def runtime_pins() -> list[tuple[str, Path, dict[str, str]]]:
    """(mod, file, {game path: sha1}) for every vanilla.json / source_data the mods ship."""
    found = []
    for root in (REPO / "mods", REPO / "ported"):
        for file in sorted(root.rglob("*.json")):
            if "__pycache__" in file.parts:
                continue
            if file.name != "vanilla.json" and file.stat().st_size > 2_000_000:
                continue
            try:
                doc = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            prints: dict[str, str] = {}

            def page(d: dict, prints: dict[str, str] = prints) -> None:
                if isinstance(d.get("path"), str) and isinstance(d.get("sha1"), str):
                    prints[d["path"]] = d["sha1"]

            if file.name == "vanilla.json":
                page(doc)
                for key, value in doc.items():
                    if key == "pages" and isinstance(value, dict):
                        for path, entry in value.items():
                            if isinstance(entry, dict) and isinstance(entry.get("sha1"), str):
                                prints["OakGame/Content/uiresources/" + path] = entry["sha1"]
                    elif isinstance(value, dict):
                        prints.update({k: v for k, v in value.items() if isinstance(v, str) and _SHA1.match(v)})
            src = doc.get("source_data")
            if isinstance(src, dict):
                prints.update({k: v for k, v in src.items() if isinstance(v, str) and _SHA1.match(v)})
            if prints:
                found.append((file.relative_to(root).parts[0], file, prints))
    return found


# endregion
# region check


def load_pins() -> dict:
    try:
        return json.loads(PINS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stale_generators(game: Path, pins: dict) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """{generator: {game path: (pinned sha1, current sha1)}} for every generator whose inputs changed."""
    recorded = pins.get("generators", {})
    every = sorted({p for g in recorded.values() for p in g.get("inputs", {})})
    now = current_sha1s(game, every)
    stale = {}
    for name, rec in recorded.items():
        changed = {p: (old, now.get(p)) for p, old in rec.get("inputs", {}).items() if now.get(p) != old}
        if changed:
            stale[name] = changed
    return stale


def with_dependents(names: set[str]) -> list[str]:
    """The given generators plus everything that reads their output, in run order."""
    chosen = set(names)
    for gen in GENERATORS:
        if any(n in chosen for n in gen.needs):
            chosen.add(gen.tool)
    return [g.tool for g in GENERATORS if g.tool in chosen]


def cmd_check(args: argparse.Namespace) -> int:
    game: Path = args.game
    pins = load_pins()
    now = build_info(game)
    problems = 0

    print("== Game build")
    base = pins.get("build", {})
    for key in ("steam_build", "exe_size", "pe_stamp"):
        was, cur = base.get(key), now.get(key)
        mark = "same" if was == cur else "CHANGED"
        print(f"  {key:12} {cur}  (baseline {was}: {mark})")
    if not pins:
        print("  no baseline yet: run `python tools/game_patch.py baseline`")
        return 2
    exe_changed = any(base.get(k) != now.get(k) for k in ("exe_size", "pe_stamp"))
    if exe_changed:
        problems += 1
        print("  The exe changed: native finders may fail. Start the game and run "
              "`python tools/game_patch.py live <bridge id>` (docs/game-patch.md, step 4).")

    print("\n== Generated data (inputs recorded in tools/game_pins.json)")
    stale = stale_generators(game, pins)
    missing = [g.tool for g in GENERATORS if g.tool not in pins.get("generators", {})]
    for gen in GENERATORS:
        if gen.tool in stale:
            changed = stale[gen.tool]
            print(f"  STALE  {gen.tool}  ({', '.join(gen.mods)})")
            for path, (old, cur) in sorted(changed.items())[:8]:
                what = "new" if old is None else "gone" if cur is None else "changed"
                print(f"           {what}: {path}")
            if len(changed) > 8:
                print(f"           ... {len(changed) - 8} more")
            print(f"           players meanwhile: {gen.stale}")
        elif gen.tool in missing:
            print(f"  ?      {gen.tool}  (not in the baseline: run `baseline` or `regen {gen.tool}`)")
        else:
            print(f"  ok     {gen.tool}")
    problems += len(stale)

    print("\n== Runtime pins (what players' mods compare at startup)")
    for mod, file, prints in runtime_pins():
        cur = current_sha1s(game, list(prints))
        bad = [p for p, sha in prints.items() if cur.get(p) != sha]
        rel = file.relative_to(REPO).as_posix()
        if bad:
            print(f"  MISMATCH {rel}: {len(bad)} of {len(prints)} (e.g. {bad[0]})")
        elif args.verbose:
            print(f"  ok       {rel} ({len(prints)})")
    if not args.verbose:
        print("  (matching pins hidden; -v lists them)")

    print("\n== Loose game files")
    # Skip Intro renames these to <name>.skipped; a patch restoring them is fine (it renames them again
    # at the next start). Neither name there means the game renamed its logo movies.
    loose_ok = True
    for name in ("2KLOGO.bk2", "GBXLOGO.bk2"):
        movie = game / "OakGame" / "Content" / "Movies" / name
        if not movie.exists() and not movie.with_name(name + ".skipped").exists():
            loose_ok = False
            problems += 1
            print(f"  Movies/{name} is gone: Skip Intro's LOGOS (mods/bl4_skip_intro/movies.py) need the new names")
    if loose_ok:
        print("  ok")

    if stale:
        print(f"\nNext: python tools/game_patch.py regen   (runs: {', '.join(with_dependents(set(stale)))})")
    return 1 if problems else 0


# endregion
# region regen / baseline


def find_oodle(given: Path | None) -> Path | None:
    if given:
        return given
    if os.environ.get("BL4_OODLE"):
        return Path(os.environ["BL4_OODLE"])
    common = DEFAULT_GAME.parent
    for rel in OODLE_CANDIDATES:
        if (common / rel).exists():
            return common / rel
    return None


def run_generator(gen: Gen, root: Path, game: Path, oodle: Path | None) -> dict[str, str | None]:
    """Runs tools/<gen>.py under `root` (the repo or a scratch copy); returns the game files it looked up."""
    handle, name = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    record = Path(name)
    cmd = [sys.executable, str(Path(__file__).resolve()), "_record", str(record), str(root / "tools" / f"{gen.tool}.py")]
    if gen.oodle:
        if oodle is None:
            raise SystemExit("no Oodle DLL found: pass --oodle <oo2core_*_win64.dll> or set BL4_OODLE")
        cmd += ["--oodle", str(oodle), "--game", str(game)]
    start = time.monotonic()
    print(f"-- {gen.tool}", flush=True)
    # Some generators iterate sets of strings (rolls.json's interned tags, pools.json): with Python's
    # random hash seed every run wrote the same data in a new order, and a release then saw a change
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    result = subprocess.run(cmd, cwd=root, env=env, check=False)
    try:
        inputs = json.loads(record.read_text(encoding="utf-8") or "{}")
    finally:
        record.unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(f"{gen.tool} failed (exit {result.returncode}): an anchor or record it expects is gone; "
                         "fix the generator for the new game data (docs/game-patch.md, step 3)")
    print(f"   {len(inputs)} game files read, {time.monotonic() - start:.0f} s")
    return inputs


def save_pins(game: Path, recorded: dict[str, dict], replace_all: bool) -> None:
    pins = {} if replace_all else load_pins()
    gens = pins.get("generators", {})
    gens.update(recorded)
    pins = {
        "_note": "Written by tools/game_patch.py (regen / baseline): the game files each generator read. Don't edit.",
        "build": build_info(game),
        "recorded": time.strftime("%Y-%m-%d"),
        "generators": {g.tool: gens[g.tool] for g in GENERATORS if g.tool in gens},
    }
    tmp = PINS.with_suffix(".tmp")
    tmp.write_text(json.dumps(pins, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(PINS)


def cmd_regen(args: argparse.Namespace) -> int:
    game: Path = args.game
    if args.names:
        unknown = [n for n in args.names if n not in BY_NAME]
        if unknown:
            raise SystemExit(f"unknown generator(s): {', '.join(unknown)}; known: {', '.join(BY_NAME)}")
        order = with_dependents(set(args.names))
    elif args.all:
        order = [g.tool for g in GENERATORS]
    else:
        pins = load_pins()
        if not pins:
            raise SystemExit("no baseline: run `baseline` first, or `regen --all`")
        order = with_dependents(set(stale_generators(game, pins)))
        if not order:
            print("nothing stale (check `python tools/game_patch.py check`)")
            return 0
    oodle = find_oodle(args.oodle)
    recorded = {name: {"inputs": run_generator(BY_NAME[name], REPO, game, oodle)} for name in order}
    save_pins(game, recorded, replace_all=False)
    print(f"\nRecorded {len(recorded)} generator(s) in {PINS.relative_to(REPO)}.")
    status = subprocess.run(["git", "status", "--porcelain", "--", "mods", "dev/bl4_loot_spawner"],
                            cwd=REPO, capture_output=True, text=True, check=False).stdout
    touched = sorted({line[3:].split("/")[1] for line in status.splitlines() if line[3:].startswith("mods/")})
    if touched:
        print("Mods with changed files (review `git diff`, then bump each one's pyproject.toml version):")
        for mod in touched:
            print(f"  {mod}")
    return 0


_COPY = ("tools", "mods", "shared", "dev/bl4_loot_spawner")


def cmd_baseline(args: argparse.Namespace) -> int:
    """Regenerates everything into a scratch copy of the repo, records the inputs, diffs the outputs."""
    game: Path = args.game
    oodle = find_oodle(args.oodle)
    scratch = Path(tempfile.mkdtemp(prefix="bl4_game_patch_"))
    try:
        for rel in _COPY:
            shutil.copytree(REPO / rel, scratch / rel, symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        started = time.time() - 2  # file times are coarse; anything written from here on is a generator's
        recorded = {g.tool: {"inputs": run_generator(g, scratch, game, oodle)} for g in GENERATORS}
        print("\n== Regenerated vs the repo")
        differ = 0
        written = 0
        for rel in _COPY:
            for new in sorted((scratch / rel).rglob("*")):
                if not new.is_file() or "__pycache__" in new.parts or new.stat().st_mtime < started:
                    continue
                written += 1
                old = REPO / new.relative_to(scratch)
                a = new.read_bytes()
                b = old.read_bytes() if old.exists() else None
                if a == b:
                    continue
                name = old.relative_to(REPO).as_posix()
                if b is not None and a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"):
                    print(f"  line endings only: {name}")
                    continue
                if b is not None and new.suffix == ".json":
                    try:
                        if json.loads(a) == json.loads(b):
                            print(f"  same data, different order: {name}")
                            continue
                    except ValueError:
                        pass
                differ += 1
                print(f"  DIFFERS: {name}")
        print(f"  ({written} files written by the generators)")
        if differ:
            print("  The shipped files don't all match what the installed game regenerates. If the game wasn't "
                  "patched, a generator changed since its output was made (or isn't deterministic): look before "
                  "trusting this baseline.")
        else:
            print("  identical: the shipped data is exactly what this game build regenerates")
        if differ and not args.force:
            print(f"\nBaseline NOT written (pass --force to write it anyway). Scratch copy kept: {scratch}")
            scratch = None
            return 1
        save_pins(game, recorded, replace_all=True)
        print(f"\nWrote {PINS.relative_to(REPO)} ({len(recorded)} generators).")
        return 0
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def cmd_record(argv: list[str]) -> int:
    """Internal: run a generator with pak.winning_entry wrapped, write {path: sha1 | None} it looked up."""
    record, script, *rest = argv
    seen: dict[str, str | None] = {}

    def wrap(module) -> None:  # noqa: ANN001
        original = module.winning_entry

        def winning_entry(paks, game_path):  # noqa: ANN001, ANN202
            entry = original(paks, game_path)
            seen[game_path] = None if entry is None else entry.sha1
            return entry

        module.winning_entry = winning_entry
        module._game_patch_wrapped = True  # noqa: SLF001

    real_import = builtins.__import__

    def watching_import(name, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        module = real_import(name, *a, **k)
        pak = sys.modules.get("pak")
        if pak is not None and hasattr(pak, "winning_entry") and not getattr(pak, "_game_patch_wrapped", False):
            wrap(pak)
        return module

    builtins.__import__ = watching_import
    sys.argv = [script, *rest]
    sys.path.insert(0, str(Path(script).parent))
    import runpy  # noqa: PLC0415

    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        Path(record).write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
    return 0


# endregion
# region live / log

# Lines our mods log when a patch made them step aside, and the SDK's own failures
SYMPTOMS = re.compile(
    r"game version|a game patch|a patch\?|needs an update|game code changed|paused:|"
    r"Failed to import mod|doesn't look as expected|not found \(\d+ candidates\)|couldn't find \w+; that part|"
    r"this game version moved|layout changed|check failed",
    re.IGNORECASE,
)


def cmd_log(args: argparse.Namespace) -> int:
    path: Path = args.path or (args.game / LOG_REL)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as ex:
        raise SystemExit(f"can't read {path}: {ex}") from None
    hits = [line for line in lines if SYMPTOMS.search(line)]
    tracebacks = sum(1 for line in lines if line.lstrip().startswith("Traceback"))
    print(f"== {path} ({len(lines)} lines)")
    seen: set[str] = set()
    for line in hits:
        key = re.sub(r"^\S+\s+\S+\s+", "", line)[:200]  # drop the timestamp so repeats collapse
        if key not in seen:
            seen.add(key)
            print("  " + line.strip()[:300])
    if not hits:
        print("  no patch symptoms")
    if tracebacks:
        print(f"  {tracebacks} traceback(s) in the log: read them (a renamed class or property shows up there)")
    return 1 if hits else 0


def cmd_live(args: argparse.Namespace) -> int:
    script = REPO / "dev" / "patch_status.py"
    out = subprocess.run([sys.executable, str(TOOLS / "bridge.py"), args.id, "-f", str(script), "--timeout", "60"],
                         cwd=REPO, check=False)
    print()
    log_args = argparse.Namespace(path=None, game=args.game)
    return out.returncode or cmd_log(log_args)


# endregion


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_record":
        return cmd_record(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="what the installed game changed (offline)")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("regen", help="rerun generators in dependency order")
    p.add_argument("names", nargs="*", help="generators (plus what depends on them); default: the stale ones")
    p.add_argument("--all", action="store_true")
    p.add_argument("--oodle", type=Path)
    p = sub.add_parser("baseline", help="regenerate into a scratch copy, record tools/game_pins.json")
    p.add_argument("--oodle", type=Path)
    p.add_argument("--force", action="store_true", help="write the baseline even if outputs differ from the repo")
    p = sub.add_parser("live", help="check a running game through the bridge")
    p.add_argument("id", help="bridge id (C for scenario.py, else the game's PID; ls .bridge/)")
    p = sub.add_parser("log", help="patch symptoms in an unrealsdk.log")
    p.add_argument("path", type=Path, nargs="?")
    args = parser.parse_args()
    return {"check": cmd_check, "regen": cmd_regen, "baseline": cmd_baseline, "live": cmd_live, "log": cmd_log}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
