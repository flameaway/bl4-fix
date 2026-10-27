"""
Writes mods/bl4_instant_travel/stations.json for the Instant Fast Travel mod (dev only).

    python tools/gen_instant_travel.py --oodle <path to an oo2core_*_win64.dll>

The station handle the ECHO map passes to RequestTeleportToStation is unresolved in Python (only
its name, `World_P.FT_GRA_West_Silo2`; no `loc`/`rot`/`TypeDef`), and far stations aren't streamed
in, so the mod needs every station's position up front. From the game's NCS data: each
`gbx_discovery_location_meta_data` record of type `DiscoveryLocation_MenuIO_FastTravel` names its
station (`travelstationdef` = `map'<Map>.<Station>'`, the handle's name) and where its actor
stands (`actorworldlocation`, `actorworldtransform` rotation). Checked against the live actors of
World_P stations (same position, same yaw).

A station inside a level instance (`owningmapchain` longer than one) is marked `nested`: whether
its location is world space wasn't checked, so the mod uses those only once the actor is loaded.

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
OUT = REPO / "mods" / "bl4_instant_travel" / "stations.json"
FAST_TRAVEL = "'DiscoveryLocation_MenuIO_FastTravel'"


def build(load: Loader) -> dict[str, Any]:
    data = gen_drops._Data(load)  # noqa: SLF001
    stations: dict[str, list[Any]] = {}
    for record in data.records("gbx_discovery_location_meta_data").values():
        if not str(record.get("locationtype") or "").endswith(FAST_TRAVEL):
            continue
        name = _quoted(record.get("travelstationdef"))
        where = record.get("actorworldlocation") or {}
        yaw = gen_drops.machine_yaw(record)
        if not name or name.lower() == "none" or not where or yaw is None:
            continue
        nested = len(record.get("owningmapchain") or []) > 1
        x, y, z = (round(float(where[axis]), 1) for axis in "xyz")
        stations[name] = [x, y, z, yaw, 1 if nested else 0]
    return {"stations": dict(sorted(stations.items())), "source_data": dict(sorted(data.fingerprints.items()))}


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
    # One line per station
    lines = [f"  {json.dumps(name)}: {json.dumps(spot)}" for name, spot in result["stations"].items()]
    text = '{\n "stations": {\n' + ",\n".join(lines) + '\n },\n "source_data": '
    text += json.dumps(result["source_data"], indent=2).replace("\n", "\n ") + "\n}\n"
    OUT.write_text(text, encoding="utf-8")
    nested = sum(1 for s in result["stations"].values() if s[4])
    print(f"{len(result['stations'])} fast travel stations ({nested} in level instances) -> {OUT}")


if __name__ == "__main__":
    main()
