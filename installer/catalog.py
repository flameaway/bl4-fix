"""
Every mod the setup app can install, shared by the build script (what to pack) and core (what to
install and what to clean up).

A mod's .sdkmod is built from `<source>/<id>/`. Required mods are always installed; the rest are
the app's checkboxes. Mods that write their own files into the game folder list them, so removing
the mod removes them too: `paks` are shim paks in Paks/~mods (with their `.next` / `.remove`
staging files), `ui_dirs` are loose folders under OakGame/Content/uiresources.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModInfo:
    id: str
    name: str
    blurb: str
    source: str = "mods"  # repo folder: "mods" (ours) or "ported" (GPL-3.0 ports of Yeti's mods)
    required: bool = False
    default: bool = True  # ticked on a first install (every mod is, by the user's choice, 2026-10-25)
    paks: tuple[str, ...] = ()
    ui_dirs: tuple[str, ...] = ()
    # Loose pictures made on the dev machine (too big for the .sdkmod the in-game updater fetches):
    # folders under uiresources that build.py zips into `<id>.assets.zip` and core unpacks
    assets: tuple[str, ...] = ()
    # Runs page scripts through the BL4 UI Link (shared/uilink): LINK_PAK and LINK_UI_DIR are shared
    # by every such mod and only removed with the last of them
    link: bool = False


LINK_PAK = "BL4Fix_UILink_4251_P.pak"  # shared/uilink/__init__.py PAK_NAME
LINK_UI_DIR = "bl4link"


def _pak(number: int) -> str:
    return f"pakchunk0-Windows_{number}_P.pak"


MODS: tuple[ModInfo, ...] = (
    ModInfo("bl4_direct", "BL4 Direct Connect", "Direct IP co-op (F6 in game).", required=True,
            ui_dirs=("bl4directconnect",)),
    ModInfo(
        "bl4_mod_menu", "Mod Menu", "Settings for every mod: MODS in the pause menu or F7.",
        required=True, paks=(_pak(4245), _pak(4247)), ui_dirs=("bl4modmenu",), link=True,
    ),
    ModInfo(
        "bl4_item_cards", "Detailed Item Cards",
        "Class mod skills with numbers, firmware, drop sources and Big Encore travel on item cards.",
        default=True, paks=(_pak(4242),), ui_dirs=("bl4direct",), link=True,
    ),
    ModInfo("bl4_skip_intro", "Skip Intro", "No 2K / Gearbox logo movies at launch."),
    ModInfo(
        "bl4_instant_tabs", "Instant Menu Tabs", "The ECHO menu opens and switches pages at once.",
        paks=(_pak(4246),), ui_dirs=("bl4instanttabs",), link=True,
    ),
    ModInfo("bl4_echo_anywhere", "ECHO Anywhere", "Open the ECHO menu when the game refuses (falling, ...)."),
    ModInfo(
        "bl4_respec", "Easy Respec", "Right-click a skill to take a point back.",
        paks=(_pak(4244),), ui_dirs=("bl4respec",), link=True,
    ),
    ModInfo("bl4_builds", "Build Presets", "Five build slots in the inventory: gear, skills and specs.", ui_dirs=("bl4builds",), link=True),
    ModInfo("bl4_reroll", "Reroll", "Reroll class mod and enhancement stats, or whole legendaries and pearls, for Eridium.", ui_dirs=("bl4reroll",), link=True),
    ModInfo("bl4_weapondex", "Kairosdex", "A dex of the bosses you've beaten and their legendaries (F8).", paks=(_pak(4248), _pak(4250)), ui_dirs=("bl4weapondex",), assets=("bl4weapondex/bosses", "bl4weapondex/items"), link=True),
    ModInfo("bl4_damage_meter", "Carnage Meter", "Every player's damage and top damage sources (F9), and a pop-up when a boss dies.", paks=(_pak(4249),), ui_dirs=("bl4carnage",), link=True),
    ModInfo("bl4_uvh_teleport", "UVH Boss Teleport", "Hover a UVH boss challenge and press T to go there.", ui_dirs=("bl4uvhtp",), link=True),
    ModInfo("bl4_instant_travel", "Instant Fast Travel", "Fast travel on the same map without the portal."),
    ModInfo("bl4_unstuck", "Unstuck", "Frees a character stuck floating and puts it on the nearest respawn station."),
    ModInfo("bl4_minimap", "Minimap", "A minimap with the ECHO map's art, fog, icons and route (N)."),
    ModInfo("bl4_third_person", "Third Person", "H: over-the-shoulder camera with aiming."),
    ModInfo("bl4_sprint_toggle", "Sprint Toggle", "One press keeps you sprinting (after stops, shots, aiming), the next walks. Pick its key in the Mod Menu."),
    ModInfo("bl4_vehicles_anywhere", "Vehicles Anywhere", "Summon and drive where vehicles aren't allowed."),
    ModInfo("bl4_backpack_size", "Backpack Size", "Set the backpack, bank and Lost Loot limits."),
    ModInfo("bl4_shoot_open", "Shoot to Open", "Shoot a container or chest to open it."),
    ModInfo("bl4_less_fog", "Less Fog", "Thin out the haze over the world."),
    ModInfo("bl4_crisp_numbers", "Crisp Damage Numbers", "Sharper damage numbers with DLSS/FSR (Light: free; Full: costs GPU)."),
    ModInfo("bl4_tidy_pause", "Tidy Pause Menu", "Move the pause menu's News panel to the right, or hide it.", ui_dirs=("bl4tidypause",), link=True),
    ModInfo("bl4_perf", "Performance Fixes", "Fewer hitches: no DLSS reload after menus, cheaper mod hooks."),
    ModInfo("bl4_profiler", "Mod Profiler", "Benchmark what each mod costs per frame, other modders' mods too.", ui_dirs=("bl4profiler",)),
    ModInfo("bl4_dialog_skipper", "Dialog Skipper", "Skip voice lines by key or automatically.", source="ported"),
    ModInfo("bl4_grapple_anywhere", "Grapple Anywhere", "Grapple to any ledge you aim at.", source="ported"),
    ModInfo("bl4_ground_loot", "Ground Loot Helpers", "Bring the loot lying around to you, or clear it.", source="ported"),
    ModInfo("bl4_slam_tweaks", "Slam Tweaks", "A fast Pre-Sequel style ground slam.", source="ported"),
    ModInfo("bl4_encore_tweaks", "Encore Tweaks", "Big Encore machines refill health, ammo and skills.", source="ported"),
)

BY_ID = {m.id: m for m in MODS}
REQUIRED = tuple(m.id for m in MODS if m.required)
OPTIONAL = tuple(m.id for m in MODS if not m.required)
DEFAULTS = frozenset(m.id for m in MODS if m.required or m.default)
