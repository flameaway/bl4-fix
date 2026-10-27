"""
Writes mods/bl4_weapondex/travel.json for the Weapondex's boss waypoint / travel buttons (dev only).

    python tools/gen_weapondex_travel.py --oodle <path to an oo2core_*_win64.dll>

Run it after tools/gen_weapondex.py: it reads that dex.json. A boss's fight marker (its `fight`, a
`gbx_discovery_location_meta_data` record of type `DiscoveryLocation_Activity_BossFight*`) is the
fight's Big Encore machine, where Item Cards' Big Encore travel and UVH Boss Teleport go too. From
the marker: its map and position, and the nearest fast travel station on that map (ranked as
gen_drops / gen_uvh_teleport do: fast travel first, level travel if a map has no other kind).

Keyed by boss id. Bosses with no fight marker (most open-world bosses) aren't listed.

Also records the SHA1s of the data files used ("source_data").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_drops  # noqa: E402
from gen_drops import Loader, _quoted  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEX = REPO / "mods" / "bl4_weapondex" / "dex.json"
OUT = REPO / "mods" / "bl4_weapondex" / "travel.json"


def build(load: Loader) -> dict[str, Any]:
    data = gen_drops._Data(load)  # noqa: SLF001
    markers = data.records("gbx_discovery_location_meta_data")
    dex = json.loads(DEX.read_text(encoding="utf-8"))

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

    out: dict[str, dict[str, Any]] = {}
    for boss in dex["bosses"]:
        fight = boss.get("fight")
        marker = markers.get(fight) if fight else None
        if marker is None:
            continue
        where = marker.get("actorworldlocation") or {}
        world = marker.get("worldmapname") or ""
        if not where or not world:
            continue
        x, y, z = (float(where.get(axis, 0)) for axis in "xyz")
        entry: dict[str, Any] = {"map": world, "location": fight, "x": x, "y": y, "z": z,
                                 "yaw": gen_drops.machine_yaw(marker)}
        here = [s for s in stations if s[2].lower() == world.lower()]
        if here:
            best = min(here, key=lambda s: (s[0], (s[3] - x) ** 2 + (s[4] - y) ** 2 + (s[5] - z) ** 2))
            entry["station"] = best[1]
        out[boss["id"]] = entry

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
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    no_station = [k for k, v in result["bosses"].items() if "station" not in v]
    print(f"{len(result['bosses'])} bosses with a Big Encore machine -> {OUT}")
    if no_station:
        print(f"  no fast travel station on their map: {', '.join(no_station)}")


if __name__ == "__main__":
    main()
