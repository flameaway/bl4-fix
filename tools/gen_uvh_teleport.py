"""
Writes mods/bl4_uvh_teleport/bosses.json for the UVH Boss Teleport mod (dev only).

    python tools/gen_uvh_teleport.py --oodle <path to an oo2core_*_win64.dll>

Everything comes from the game's NCS data:

- The challenges menu's "Ultimate Vault Hunter" category: `ui_challenge_list` ->
  `challengecategories[ident=uvh].challenges`, each a `challenge` record with its title
  (`displayname`), text (`description`) and, for kill challenges, the enemy
  (`damagetargetcharacterdef`, e.g. `gbxactor'Char_Drone_Bunker_Boss_Keeper'` = Core Observer).
- Where that boss is fought: its boss fight marker (`gbx_discovery_location_meta_data` of type
  `DiscoveryLocation_Activity_BossFight*`, which is the fight's Big Encore machine). The weapondex
  generator already ties each boss's actor defs to its marker (gen_weapondex.build_dex); a boss
  without dedicated drops there is matched by its name instead. A challenge without a kill target
  ("Defeat Subjugator the Invincible or Thol the Invincible") is matched by the marker names its
  text mentions.
- How to get there: the nearest fast travel station on the marker's map (as gen_drops does for
  the Big Encore machines), with its position so the mod can tell when the trip has landed.

Challenges that are missions (the Wildcard ones) have no single boss and aren't listed.

Also records the SHA1s of the data files used ("source_data").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_drops  # noqa: E402
import gen_weapondex  # noqa: E402
import ncs_parse  # noqa: E402
from gen_drops import Loader, _quoted  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_uvh_teleport" / "bosses.json"
CATEGORY = "uvh"
_UVH_IN_TEXT = re.compile(r"\bUVH\s*(\d+)", re.IGNORECASE)


def _text(value: Any) -> str:
    return ncs_parse.loc_text(value) if isinstance(value, str) and value else ""


def build(load: Loader) -> dict[str, Any]:
    data = gen_drops._Data(load)  # noqa: SLF001
    loot = gen_drops.Loot(load)
    dex = gen_weapondex.build_dex(load)
    markers = data.records("gbx_discovery_location_meta_data")
    challenges = data.records("challenge")

    fight_of_char = {char: boss["fight"] for boss in dex["bosses"] if boss.get("fight") for char in boss["chars"]}
    place_of_fight = {boss["fight"]: boss["area"] for boss in dex["bosses"] if boss.get("fight")}

    # Boss fight markers by the words of their name ("Core Observer", "Subjugator & Thol")
    fights: dict[str, list[str]] = {}
    for key, record in markers.items():
        if "BossFight" in str(record.get("locationtype")):
            display = _quoted(record.get("locationdisplaydata")) or ""
            if name := loot.texts.get(display.lower()):
                fights[key] = gen_weapondex._words(name)  # noqa: SLF001

    # Fast travel stations, as gen_drops ranks them (fast travel first, level travel if a map has
    # no other kind)
    stations: list[tuple[int, str, str, float, float, float]] = []
    for record in markers.values():
        kind = str(record.get("locationtype") or "")
        station = _quoted(record.get("travelstationdef"))
        where = record.get("actorworldlocation") or {}
        if not station or station.lower() == "none" or not where or "SendOnly" in kind:
            continue
        rank = 0 if "MenuIO_FastTravel" in kind else 1 if "MenuIO_LevelTravel" in kind else None
        if rank is not None:
            stations.append((rank, station, record.get("worldmapname") or "", float(where["x"]), float(where["y"]), float(where["z"])))

    def find_fight(char: str | None, description: str) -> str | None:
        if char:
            if (fight := fight_of_char.get(char.lower())) is not None:
                return fight
            name = loot.boss_name(char)
            words = gen_weapondex._words(gen_drops._ENCORE_WORDS.sub("", name or ""))  # noqa: SLF001
            exact = [key for key, fight_words in fights.items() if words and fight_words == words]
            return exact[0] if exact else None
        # No kill target: every word of a marker's name appears in the text
        text = set(gen_weapondex._words(description))  # noqa: SLF001
        named = [key for key, words in fights.items() if len(words) > 1 and set(words) - {"the"} <= text]
        return named[0] if len(named) == 1 else None

    category = next(
        (c for entry in data.entries("ui_challenge_list") if isinstance(entry.get("value"), dict)
         for c in entry["value"].get("challengecategories") or [] if c.get("ident") == CATEGORY),
        None,
    )  # fmt: skip
    if category is None:
        raise SystemExit(f"no '{CATEGORY}' challenge category")

    out: list[dict[str, Any]] = []
    for ref in category.get("challenges") or []:
        name = _quoted(ref) or ""
        record = challenges.get(name.lower())
        if record is None:
            continue
        title, description = _text(record.get("displayname")), _text(record.get("description"))
        char = _quoted(record.get("damagetargetcharacterdef"))
        char = None if not char or char.lower() == "none" else char
        if record.get("challengetype") not in ("Enemy_Kill", "FactTrigger"):
            continue
        fight = find_fight(char, description)
        if fight is None:
            print(f"  no boss fight for {name} ({title})")
            continue
        marker = markers[fight]
        where = marker.get("actorworldlocation") or {}
        world = marker.get("worldmapname") or ""
        x, y, z = (float(where.get(axis, 0)) for axis in "xyz")
        display = _quoted(marker.get("locationdisplaydata")) or ""
        boss = (loot.boss_name(char) if char else None) or loot.texts.get(display.lower()) or ""
        boss = gen_drops._ENCORE_WORDS.sub("", boss).strip()  # noqa: SLF001
        uvh = _UVH_IN_TEXT.search(description)
        entry: dict[str, Any] = {
            "challenge": record.get("challenge") or name,
            "title": title,
            "description": description,
            "uvh": int(uvh.group(1)) if uvh else None,
            "boss": " ".join(boss.split()),
            "place": place_of_fight.get(fight) or loot.map_name(world) or gen_drops._pretty_map(world),  # noqa: SLF001
            "map": world,
            "location": fight,
            "x": x,
            "y": y,
            "z": z,
        }
        here = [s for s in stations if s[2].lower() == world.lower()]
        if here:
            best = min(here, key=lambda s: (s[0], (s[3] - x) ** 2 + (s[4] - y) ** 2 + (s[5] - z) ** 2))
            entry["station"] = best[1]
            entry["station_at"] = [round(best[3], 1), round(best[4], 1), round(best[5], 1)]
        out.append(entry)

    return {"bosses": out, "source_data": dict(sorted(data.fingerprints.items()))}


def main() -> None:
    import gen_item_cards as G  # noqa: PLC0415  # needs the item cards mod's pak.py on the path

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=G.DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))

    cache: dict[str, tuple[list[dict], dict[str, str]]] = {}

    def load(name: str) -> tuple[list[dict], dict[str, str]]:
        if name not in cache:
            cache[name] = G._entries(paks, name, args.oodle)  # noqa: SLF001
        return cache[name]

    result = build(load)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for boss in result["bosses"]:
        print(f"  {boss['title']}: {boss['boss']} ({boss['place']}) via {boss.get('station')}")
    print(f"{len(result['bosses'])} challenges -> {OUT}")


if __name__ == "__main__":
    main()
