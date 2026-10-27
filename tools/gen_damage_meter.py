"""
Builds bl4_damage_meter/items.json: what to call the gear that dealt damage (dev only).

    python tools/gen_damage_meter.py --oodle <path to an oo2core_*_win64.dll>

The meter knows an item by its serial's first two numbers (item type, composition; see
bl4_weapondex/itemkey.py). From the game's `inv` NCS data:

- classes: character def name -> class name, hero name and the game's class portrait / trait art.
- types: serial index of each gear item type -> its manufacturer and kind ("Jakobs", "Shotgun"),
  the game's icon for the kind, and the class for class mods.
- comps: "<type>:<composition>" -> rarity (common .. pearlescent), plus the item's own name for
  legendaries and pearls ("Hellwalker"), named the way the Kairosdex names them
  (gen_drops.Loot.item_names: the composition's part with an InventoryNamingAspect).

Other items are shown as "<Manufacturer> <Kind>" in their rarity's colour: the game builds those
names from prefix/title parts at runtime, which isn't worth repeating for a damage list.

Also writes "source_data" (path -> SHA1 of the NCS files used): the mod checks them against the
install on a worker thread and drops the legendary names if a patch changed them.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import gen_drops
import gen_weapondex as W

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mods" / "bl4_damage_meter" / "items.json"

# Composition name -> rarity (the order the game sorts them in)
RARITIES = (("comp_06", "pearl"), ("pearl", "pearl"), ("comp_05", "legendary"), ("comp_04", "epic"),
            ("comp_03", "rare"), ("comp_02", "uncommon"), ("comp_01", "common"))  # fmt: skip
# Item kind -> the game's small item-type icon (uiresources/_shared/assets/...)
ICONS = {"ps": "pistol", "sm": "smg", "ar": "assault", "sg": "shotgun", "sr": "sniper", "hw": "heavy",
         "shield": "shield", "grenade_gadget": "grenade", "repair_kit": "repkit", "enhancement": "enhancement",
         "turret_gadget": "turret", "classmod": "classmod"}  # fmt: skip
SHIELD_KINDS = {"armor_shield": "Armor Shield", "energy_shield": "Energy Shield"}
# Character def ("Char_<Name>", lowercased without the prefix) -> the game's class portrait and
# trait art: a path under uiresources/_shared/assets/, or a full URL (DLC classes ship their UI as
# loose files under Content/DLC/<dlc>/uiresources, each reached through its own scheme:
# DLC/Cowbell = cowbell://uiresources/..., DLC/Common = common://...)
_VH = "ico_ui_art_vault_hunters/"
CLASSES = {
    "darksiren": (_VH + "player_class_dark_siren.png", _VH + "vh_trait_dark_siren_icon.png"),
    "exosoldier": (_VH + "player_class_exo_soldier.png", _VH + "vh_trait_exo_icon.png"),
    "gravitar": (_VH + "player_class_gravitar.png", _VH + "vh_trait_gravitar_icon.png"),
    "paladin": (_VH + "player_class_paladin.png", _VH + "vh_trait_paladin_icon.png"),
    "robodealer": ("cowbell://uiresources/portrait_icons/player_class_robodealer.png",
                   "cowbell://uiresources/skill_icons/trait/vh_trait_c4sh.png"),
}


def rarity_of(comp: str) -> str | None:
    comp = comp.lower()
    for prefix, rarity in RARITIES:
        if comp.startswith(prefix) or (prefix == "pearl" and prefix in comp):
            return rarity
    return None


def build(load: Any) -> dict[str, Any]:
    loot = gen_drops.Loot(load)
    texts = loot.texts
    makers = {k: W.ncs_parse.loc_text(v["displayname"]) for k, v in loot.data.records("Manufacturer").items()
              if "displayname" in v}  # fmt: skip
    names = loot.item_names(lambda comp: comp.startswith(("comp_05", "comp_06")) or "pearl" in comp)

    types: dict[str, dict[str, Any]] = {}
    comps: dict[str, Any] = {}
    unknown: set[str] = set()
    for entry in loot.data.entries("inv"):
        inv = entry["key"].lower()
        value = entry.get("value")
        index = (value.get("serialindex") or {}).get("index") if isinstance(value, dict) else None
        if index is not None and str(index) not in types:
            info = describe_type(inv, makers, texts)
            if info is not None:
                types[str(index)] = info
        for dep in entry.get("deps") or []:
            record = dep.get("value")
            if dep.get("table") != "inv_comp" or not isinstance(record, dict):
                continue
            comp_index = (record.get("serialindex") or {}).get("index")
            if comp_index is None:
                continue
            comp = dep["key"].lower()
            rarity = rarity_of(comp)
            own = (W._quoted(record.get("rarity")) or "").lower()  # noqa: SLF001
            if "pearl" in own:
                rarity = "pearl"
            if rarity is None:
                unknown.add(comp)
                continue
            key = f"{inv}.{comp}"
            item_name = " / ".join(sorted(names[key])) if names.get(key) else None
            comps.setdefault(inv, {})[str(comp_index)] = [rarity, item_name] if item_name else rarity

    # Compositions are keyed by type index for the mod
    type_index = {info["inv"]: k for k, info in types.items()}
    by_type = {type_index[inv]: c for inv, c in comps.items() if inv in type_index}
    print(f"{len(types)} gear types, {sum(len(c) for c in by_type.values())} compositions, "
          f"{sum(1 for c in by_type.values() for v in c.values() if isinstance(v, list))} named")  # fmt: skip
    if unknown:
        print(f"  compositions with no rarity (left out): {len(unknown)}, e.g. {sorted(unknown)[:8]}")
    classes = {}
    for cls, (portrait, trait) in CLASSES.items():
        classes[cls] = {"class": texts.get(cls) or cls.title(), "hero": texts.get(f"{cls}narrativename") or "",
                        "portrait": portrait, "trait": trait}  # fmt: skip
    print("classes:", {k: (v["class"], v["hero"]) for k, v in classes.items()})
    return {
        "classes": classes,
        "types": dict(sorted(types.items(), key=lambda kv: int(kv[0]))),
        "comps": dict(sorted(by_type.items(), key=lambda kv: int(kv[0]))),
        "source_data": dict(sorted(loot.data.fingerprints.items())),
    }


def describe_type(inv: str, makers: dict[str, str], texts: dict[str, str]) -> dict[str, Any] | None:
    """A gear item type ("jak_sg", "classmod_paladin", "bor_shield"...) or None for anything else."""
    if inv.startswith("classmod_"):
        cls = inv.removeprefix("classmod_").replace("_", "")
        return {"inv": inv, "kind": "Class Mod", "icon": ICONS["classmod"], "class": texts.get(cls) or cls.title()}
    maker, _, kind = inv.partition("_")
    if maker not in W.MAKERS:
        return None
    maker_name = makers.get(W.MAKERS[maker], W.MAKERS[maker].title())
    if kind in W.WEAPON_TYPES:
        kind_name = texts.get(W.WEAPON_TYPES[kind]) or kind.upper()
        icon = ICONS[kind]
    elif kind in W.GEAR_TYPES:
        kind_name, icon = W.GEAR_TYPES[kind], ICONS[kind]
    elif kind in SHIELD_KINDS:
        kind_name, icon = SHIELD_KINDS[kind], ICONS["shield"]
    elif re.fullmatch(r"(hw|heavy)(_\w+)?", kind):
        kind_name, icon = "Heavy Weapon", ICONS["hw"]
    else:
        return None
    return {"inv": inv, "maker": maker_name, "kind": kind_name, "icon": icon}


def main() -> None:
    import gen_item_cards as G  # noqa: PLC0415  # needs the item cards mod's pak.py on the path

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=G.DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()
    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    cache: dict[str, Any] = {}

    def load(name: str) -> Any:
        if name not in cache:
            cache[name] = G._entries(paks, name, args.oodle)  # noqa: SLF001
        return cache[name]

    data = build(load)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"-> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
