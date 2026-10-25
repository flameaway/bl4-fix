"""
A roll as the game's own item card shows it: the data model of the game's item card widget
(OakWidgetData_ItemCard, `data_def_item_card_0` in the menu page), filled from a roll. The page puts
a copy of the game's card markup in the dex and binds it to this model (ui/dex.js `gameCard`).

Field formats are the game's, as recorded from its own cards (2026-10-24): entries are
{ident, idx, image ("texture2d'<path>'"), description, value, loc_text, comparison}; types,
manufacturer idents ("borg" for Ripper) and the header words come from the item type.
"""

from __future__ import annotations

import re
from typing import Any

from . import stats

TYPE_ICONS = "/Game/uiresources/_shared/assets/ico_ui_art_item_card_type/"
LEGENDARY_ICON = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/ico_misc_legendaries.ico_misc_legendaries"
RARITY_COLORS = {"legendary": "rgba(255, 58, 0, 1.00)", "pearlescent": "rgba(0, 220, 255, 1.00)", "epic": "rgba(154, 18, 238, 1.00)"}
MANUFACTURERS = {"Ripper": "borg"}  # card ident when it isn't the name, lowercased
WEAPON_WORDS = {"ps": ("Pistol", "pistol"), "sm": ("Submachine Gun", "smg"), "ar": ("Assault Rifle", "assault"),
                "sg": ("Shotgun", "shotgun"), "sr": ("Sniper Rifle", "sniper")}  # fmt: skip
ELEMENT_IDENTS = {"Fire": "fire", "Shock": "electric", "Cryo": "cryo", "Corrosive": "corrosive", "Radiation": "radiation"}
_PLACEHOLDER = re.compile(r"\s*\{([A-Za-z0-9_]+)\}")


def _entry(ident: str, idx: int, image: str, text: str, label: str = "", value: str | None = None) -> dict[str, Any]:
    return {"ident": ident, "idx": idx, "image": f"texture2d'{image}'" if image else "", "description": text,
            "value": text if value is None else value, "loc_text": label, "comparison": "item_card_compare_none"}  # fmt: skip


def _filled(text: str, fills: dict[str, str]) -> str:
    def one(match: re.Match[str]) -> str:
        found = fills.get(match.group(1))
        if found is None:
            return ""
        return found if match.group(0).startswith("{") else " " + found

    return _PLACEHOLDER.sub(one, text)


def _types(inv: str, item: dict[str, Any]) -> dict[str, str]:
    """itemtype / itembasetype / headers / type icon for an item type (`jak_sr`, `bor_shield`, ...)."""
    kind = item.get("type", "")
    maker = item.get("maker", "")
    man = MANUFACTURERS.get(maker, maker.lower())
    suffix = inv.rpartition("_")[2]
    if suffix in WEAPON_WORDS and not inv.endswith("_gadget"):
        words, icon = WEAPON_WORDS[suffix]
        return {"itemtype": f"weapon_{suffix}", "itembasetype": "weapon", "whiteheader": words, "greyheader": "Weapon",
                "icon": f"ico_art_item_card_weap_{icon}"}  # fmt: skip
    if suffix == "hw":
        return {"itemtype": "heavy_weapon_gadget", "itembasetype": "gadget", "whiteheader": "Heavy Gun Ordnance", "greyheader": "Ordnance",
                "icon": "ico_art_item_card_heavy_weapon_generic"}  # fmt: skip
    if "grenade" in inv:
        return {"itemtype": "grenade_gadget", "itembasetype": "gadget", "whiteheader": "Grenade Ordnance", "greyheader": "Ordnance",
                "icon": f"ico_art_item_card_grenade_{'borg' if man == 'borg' else man}"}  # fmt: skip
    if "turret" in inv:
        return {"itemtype": "turret_gadget", "itembasetype": "gadget", "whiteheader": "Turret Ordnance", "greyheader": "Ordnance",
                "icon": "ico_art_item_card_heavy_weapon_generic"}  # fmt: skip
    if "repair_kit" in inv:
        return {"itemtype": "repair_kit", "itembasetype": "repair_kit", "whiteheader": "Repkit", "greyheader": "Healing",
                "icon": "ico_art_item_card_rep_kit"}  # fmt: skip
    if "shield" in inv:
        armor = (stats.data().get("bases", {}).get(inv) or [""])[0] == "armor_shield"
        kind_word = "Armor Shield" if armor else "Energy Shield"
        return {"itemtype": "armor_shield" if armor else "energy_shield", "itembasetype": "shield", "whiteheader": kind_word,
                "greyheader": "Shield", "icon": "ico_art_item_card_armor_shield" if armor else "ico_art_item_card_energy_shield"}  # fmt: skip
    if inv.startswith("classmod"):
        return {"itemtype": "classmod", "itembasetype": "classmod", "whiteheader": f"{item.get('class', '')} Class Mod".strip(),
                "greyheader": "Class Mod", "icon": "ico_art_item_card_class_mod"}  # fmt: skip
    return {"itemtype": "enhancement", "itembasetype": "enhancement", "whiteheader": kind or "Enhancement", "greyheader": "Modification",
            "icon": "ico_art_item_card_enhancement"}  # fmt: skip


def model(key: str, item: dict[str, Any], roll: dict[str, Any], level: int | None, extra_fills: dict[str, str] | None = None) -> dict[str, Any]:
    """The item card model for one roll ({"lines", "fills", "variants", "skills", "elements"})."""
    inv = key.partition(".")[0]
    rarity = str(item.get("rarity", "Legendary")).lower()
    maker = item.get("maker", "")
    types = _types(inv, item)
    lines = roll.get("lines") or []
    fills = {**(extra_fills or {}), **(roll.get("fills") or {})}

    def group(name: str) -> list[dict[str, Any]]:
        return [_entry(line["key"], i, line.get("icon", ""), line["text"], line.get("label", ""))
                for i, line in enumerate(x for x in lines if x["group"] == name)]  # fmt: skip

    headline = next((line for line in lines if line["group"] == "headline"), None)
    primary = group("primary")
    secondary = []
    texts = list(item.get("effects") or [])
    texts += [v["text"] for i, v in enumerate(item.get("variants") or []) if i in (roll.get("variants") or [])]
    for text in texts:
        secondary.append(_entry("", len(secondary), LEGENDARY_ICON, _filled(text, fills)))
    shown = {e["value"] for e in secondary}
    for line in roll.get("parts") or []:
        if line["text"] not in shown:
            shown.add(line["text"])
            secondary.append(_entry(line["key"], len(secondary), line.get("icon", ""), line["text"]))
    secondary += [{**e, "idx": len(secondary) + i} for i, e in enumerate(group("secondary"))]
    tertiary = group("tertiary")
    # A class mod's card lists its skills where a weapon has its stats
    skills = roll.get("skills") or {}
    if skills:
        named = {f"{s['graph']},{s['node']}": s for s in item.get("skills") or []}
        primary = []
        for ref, points in skills.items():
            skill = named.get(ref, {})
            # As the game's class mod card: name, skill icon, tree color as the frame, "<graph>,<node>"
            # in loc_text (Item Cards finds the skill's numbers by it)
            entry = _entry(skill.get("name", ref.partition(",")[2]), len(primary), "", skill.get("desc", ""), ref, f"+{points}")
            if skill.get("icon"):
                entry["image"] = f"Texture2D'{skill['icon']}'"
            entry["comparison"] = skill.get("color", "red")
            primary.append(entry)
    element_lines = [line for line in lines if line["group"] == "element"]
    elements = roll.get("elements") or []
    damage1 = ELEMENT_IDENTS.get(elements[0], "impact") if elements else "impact"
    if element_lines and "Cryo Efficiency" in element_lines[0]["text"]:
        damage1 = "cryo"
    damage2 = ELEMENT_IDENTS.get(elements[1], "") if len(elements) > 1 else ""
    return {
        "visible": True, "mousepositioned": False, "abbreviated": False, "compare": False, "comparemode": False,
        "name": item.get("name", ""), "rarityident": rarity, "rarityloc": item.get("rarity", ""),
        "raritycolor": RARITY_COLORS.get(rarity, "rgba(255, 255, 255, 1.00)"), "showrarity": True,
        "manufacturer": MANUFACTURERS.get(maker, maker.lower()), "manufacturername": maker,
        "itemtype": types["itemtype"], "itembasetype": types["itembasetype"],
        "whiteheader": types["whiteheader"], "greyheader": types["greyheader"], "lowergreyheader": True,
        "level": f"Lvl {level}" if level else "", "showlevel": bool(level), "leveltoolow": False,
        "thumbnailicon": f"Texture2D'{TYPE_ICONS}{types['icon']}.{types['icon']}'",
        "hasheadline": headline is not None,
        "headline": _entry(headline["key"], 0, "", headline["text"], headline["label"]) if headline else _entry("", -1, "", ""),
        "primaryactive": bool(primary), "primary_stat_entries": primary,
        "secondaryactive": bool(secondary), "secondary_stat_entries": secondary,
        "tertiaryactive": bool(tertiary), "tertiary_stat_entries": tertiary,
        "textstatsactive": False, "text_stat_entries": [], "debugstatsactive": False, "debug_stat_entries": [],
        "damagetype1": damage1, "showelement1": bool(element_lines), "elementtext": element_lines[0]["text"] if element_lines else "",
        "damagetype2": damage2, "showelement2": len(element_lines) > 1, "secondelementtext": element_lines[1]["text"] if len(element_lines) > 1 else "",
        "redtext": " ".join(item.get("red") or []),
        "legendarystatvisible": False, "legendarystat": _entry("", -1, "", ""),
        "firmwarevisible": False, "firmware": _entry("", -1, "", ""), "firmwarecount": 0, "firmwarestring": "", "firmwaretransfered": False,
        "showprice": False, "price": "", "bannertype": "", "bannertext": "", "bannericon": "",
        "showglyphs": False, "glyphs": [], "horizontalsplit": False, "cardisflipped": False,
        "isequipable": True, "lootbeam": False, "scale": 1, "itemcardshowdelay": 0,
        # Seconds between the two elements of a two-element gun (the card script swaps them; unset,
        # its timer ran with no delay and the row flickered)
        "elementswapdelay": 1.8,
    }  # fmt: skip
