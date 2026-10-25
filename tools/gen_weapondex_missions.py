"""
Which missions mean a dex boss was beaten, for the Weapondex's save reader (savegame.py):
mods/bl4_weapondex/missions.json, {boss id: {"missions": [...], "unlockables": [...]}}.

    python tools/gen_weapondex_missions.py --oodle <path to an oo2core_*_win64.dll>

A mission counts for a boss when finishing it unlocks that boss's Big Encore machine: one of its
end states sets `Script_EligibleForReplay` to True on an alias whose actor (map + registry name)
is the actor of the boss's fight marker (dex.json `fight`, a `gbx_discovery_location_meta_data`
record). Missions only mentioning a machine (the first story mission sets up Splashzone's) don't
count for a boss that has such a mission. For the bosses that don't, in order:
- a mission that mentions the marker's actor (Shadowpelt's Auger Mine, whose data has no unlock);
- a mission that runs a boss fight in the marker's own arena level (`registry.<map>.<arena>.base.
  oakbossfightinfo...`: The Timekeeper's fight is in mission_main_city3).
A mission's `accountwideunlockable` (activities: drill sites, bunkers, mines) is the
profile-wide record of it, which the game's shared progress keeps for every character.

Reads the `mission` and `gbx_discovery_location_meta_data` NCS types and dex.json (run
tools/gen_weapondex.py first when the dex changes).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEX = REPO / "mods" / "bl4_weapondex" / "dex.json"
OUT = REPO / "mods" / "bl4_weapondex" / "missions.json"
sys.path[:0] = [str(REPO / "tools"), str(REPO / "mods" / "bl4_item_cards")]


def _unlocked_aliases(node: Any, out: set[str]) -> None:
    """Aliases some end state sets Script_EligibleForReplay = True on."""
    if isinstance(node, dict):
        change = node.get("factchange")
        if (isinstance(change, dict) and str(change.get("fact", "")).lower() == "script_eligibleforreplay"
                and str((change.get("value") or {}).get("namevalue", "")).lower() == "true"):  # fmt: skip
            out.add(str(node.get("alias", "")).lower())
        for value in node.values():
            _unlocked_aliases(value, out)
    elif isinstance(node, list):
        for value in node:
            _unlocked_aliases(value, out)


def _unquote(ref: str) -> str:
    """unlockable'echo_upgrade_challenges.Activity_X' -> echo_upgrade_challenges.Activity_X"""
    return ref.split("'")[1] if "'" in ref else ref


def build(missions: list[dict], markers: dict[str, dict], dex: dict[str, Any]) -> dict[str, Any]:
    fight_bosses: dict[str, list[str]] = {}
    for boss in dex["bosses"]:
        if boss.get("fight"):
            fight_bosses.setdefault(boss["fight"], []).append(boss["id"])
    by_actor = {}
    for key, marker in markers.items():
        if key in fight_bosses:
            actor = marker.get("actorref") or {}
            by_actor[(str(actor.get("map", "")).lower(), str(actor.get("registryname", "")).lower())] = key
    out: dict[str, dict[str, set[str]]] = {}
    for entry in missions:
        mission = entry["value"]
        aliases = {}
        for node in (mission.get("alias_nodes") or {}).get("alias", []):
            for name, alias in node.items():
                actor = (alias.get("aliasdata") or {}).get("actor")
                if actor:
                    aliases[name.lower()] = (str(actor.get("map", "")).lower(), str(actor.get("registryname", "")).lower())
        unlocked: set[str] = set()
        _unlocked_aliases(mission, unlocked)
        for alias in unlocked:
            fight = by_actor.get(aliases.get(alias))  # type: ignore[arg-type]
            for boss in fight_bosses.get(fight, []):
                record = out.setdefault(boss, {"missions": set(), "unlockables": set()})
                record["missions"].add(entry["key"].lower())
                if unlockable := mission.get("accountwideunlockable"):
                    record["unlockables"].add(_unquote(unlockable).lower())
    # Fallbacks for markers no mission unlocks
    texts = {entry["key"]: json.dumps(entry).lower() for entry in missions}
    for (world, registry), fight in by_actor.items():
        if any(boss in out for boss in fight_bosses[fight]):
            continue
        actor = markers[fight].get("actorref") or {}
        arena = str(actor.get("submap", "none")).lower()
        found = [k for k, text in texts.items() if f'"{registry}"' in text and f'"{world}"' in text]
        if not found and arena != "none":
            fight_info = f"registry.{world}.{arena}.base.oakbossfightinfo"
            found = [k for k, text in texts.items() if fight_info in text]
        for key in found:
            mission = next(e["value"] for e in missions if e["key"] == key)
            for boss in fight_bosses[fight]:
                record = out.setdefault(boss, {"missions": set(), "unlockables": set()})
                record["missions"].add(key.lower())
                if unlockable := mission.get("accountwideunlockable"):
                    record["unlockables"].add(_unquote(unlockable).lower())
    return {boss: {k: sorted(v) for k, v in record.items()} for boss, record in sorted(out.items())}


def main() -> None:
    import gen_item_cards as G  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=G.DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    missions, _ = G._entries(paks, "mission", args.oodle)  # noqa: SLF001
    markers, _ = G._records(paks, "gbx_discovery_location_meta_data", args.oodle)  # noqa: SLF001
    dex = json.loads(DEX.read_text(encoding="utf-8"))
    result = build(missions, markers, dex)
    OUT.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"{len(result)} bosses linked to {len({m for r in result.values() for m in r['missions']})} missions -> {OUT}")


if __name__ == "__main__":
    main()
