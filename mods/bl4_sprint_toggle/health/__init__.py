"""
BL4 Fix health: which of our mods a game update (or another mod) has broken, shown where players
look, whichever of our mods they run.

Source of truth: shared/health/ in the repo, copied byte for byte into the mods that carry it
(`<mod>/health/`, tools/sync_shared.py). Never edit a mod's copy.

What it does
------------
- Mods report a problem: `health.report(mod, part, reason, whole=False)` / `health.clear(mod, part)`.
  Mods that don't carry this package are covered by PROBES: read-only looks at the step-aside
  flags they already keep (a game patch moved code they need, their data no longer matches the
  game's...). A probe answers None (fine, or not checked yet) or the reason.
- A mod with a problem shows "Needs update" (or "Conflict", when another mod's file is in the way)
  as its status and the reasons at the top of its description, in every mod list (the SDK's console
  menu, the BL4 Mod Menu, its basic panel): they all show `get_status()` and `description`.
  "Whole" problems (the mod does nothing at all) also turn the mod off for this session without
  touching its saved setting, and lock its switch, so it shows disabled with its reason; the next
  launch after an update turns it on again as saved.
- One of our mods that failed to import (Python dropped it) gets a stand-in entry with the same
  name, locked, "Needs update", so it shows in the mod lists instead of silently missing.
- Once per game version and set of problems, a notice on screen (notice.py, plain UMG: it works
  even when the HTML pages are what broke) names the mods and points at the mod list.

Many copies, one owner: the copies share `sys.modules[HUB]` (a plain module object); the newest
VERSION runs the checks. Shared fields are only ever added, never renamed or given a new meaning.

Cost: tick() is a clock compare most frames. Every PROBE_S the owner reads the probes (attribute
reads, no game calls) and only touches a mod object when its problems changed. The import scan
runs once, on a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import types
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unrealsdk import logging

VERSION = 3
HUB = "_bl4_health"
# Probes are read this often; the import scan and the notice wait for the game to settle first
PROBE_S = 2.0
SCAN_AFTER_S = 15.0
NOTICE_AFTER_S = 20.0
SETTINGS_NAME = "bl4fix_health.json"
STEAM_APP_ID = "1285190"

NEEDS_UPDATE = "<font color='#ffcc00'>Needs update</font>"
CONFLICT = "<font color='#ffcc00'>Conflict</font>"


def _game_dir() -> Path | None:
    try:
        from mods_base import SETTINGS_DIR  # noqa: PLC0415

        return Path(SETTINGS_DIR).parent.parent
    except Exception:  # noqa: BLE001
        return None


GAME_DIR = _game_dir()


# region Probes: read-only looks at what other mods already know about themselves


def _attr(module: str, path: str) -> Any:
    """`sys.modules[module]` then a dotted attribute path; None if any step is missing."""
    obj: Any = sys.modules.get(module)
    for name in path.split(".") if path else ():
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj


def _false(module: str, path: str, reason: str) -> Callable[[], str | None]:
    """The flag is exactly False (None means still checking)."""
    return lambda: reason if _attr(module, path) is False else None


def _set(module: str, path: str, reason: str) -> Callable[[], str | None]:
    """The value is truthy (an error text, a non-empty list)."""
    return lambda: reason if _attr(module, path) else None


def _called(module: str, path: str, reason: str) -> Callable[[], str | None]:
    def probe() -> str | None:
        fn = _attr(module, path)
        return reason if callable(fn) and fn() else None

    return probe


def _item_cards_page() -> str | None:
    status = _attr("bl4_item_cards", "_status")
    if not isinstance(status, str) or "paused" not in status:
        return None
    # "...paused: <pak> in Paks/~mods also changes item cards." is another mod's file, not a patch
    if "also changes" in status:
        return "conflict: " + status.split("paused: ", 1)[-1].rstrip(".") + "; detailed item cards are off"
    return "detailed item cards are off"


def _item_cards_data() -> str | None:
    data = _attr("bl4_item_cards.card_page", "_data_ok")
    if isinstance(data, dict) and any(v is False for v in data.values()):
        return "some card details (skill numbers, firmware, drop sources) are off"
    return None


def _instant_tabs() -> str | None:
    found = _attr("bl4_instant_tabs.patch", "_found")
    missing = [k for k, v in found.items() if v is None] if isinstance(found, dict) else []
    return f"{len(missing)} of its code patches don't fit this game version" if missing else None


def _unstuck() -> str | None:
    found = _attr("bl4_unstuck.hold", "_Found")
    if found is not None and getattr(found, "done", False) and not getattr(found, "flag", 1):
        return "the movement hold isn't found: it frees gravity only"
    return None


# package -> [(part, whole, probe)]. Keep in step with the mods' own step-aside flags;
# a probe that can't find its flag answers None, so a renamed flag fails quiet, not loud.
PROBES: dict[str, list[tuple[str, bool, Callable[[], str | None]]]] = {
    "bl4_reroll": [
        ("data", True, _false("bl4_reroll", "_data_ok", "the game's item data changed; rerolls are off")),
        ("native", False, _set("bl4_reroll.native", "_api_error", "in-place swaps are off (sell + mail instead)")),
    ],
    "bl4_item_cards": [("page", False, _item_cards_page), ("data", False, _item_cards_data)],
    "bl4_instant_tabs": [("patch", False, _instant_tabs)],
    "bl4_damage_meter": [
        ("binder", True, _set("bl4_damage_meter.tracker", "S.binder.error", "it can't count damage on this game version")),
        ("names", False, _false("bl4_damage_meter.naming", "_current", "legendary names show as plain names")),
    ],
    "bl4_weapondex": [
        ("data", False, _false("bl4_weapondex.data", "_data_ok", "drop chances may be off")),
        ("identity", False, _set("bl4_weapondex.identity", "_State.error", "rolling items is off")),
    ],
    "bl4_echo_anywhere": [("native", True, _set("bl4_echo_anywhere", "State.failed", "it can't open the ECHO menu on this game version"))],
    "bl4_builds": [("equip", False, _set("bl4_builds.game", "_equipper_error", "loading gear is off (skills still load)"))],
    "bl4_direct": [
        ("arrival", False, _set("bl4_direct.arrival", "scan_error", "friends' fast-travel arrival fix is off")),
        ("coop", False, _set("bl4_direct.coop", "_State.unreadable", "co-op messages between our mods are off")),
    ],
    "bl4_minimap": [
        ("objectives", False, _set("bl4_minimap.objectives", "Objectives.broken", "live objectives are off")),
        ("route", False, _set("bl4_minimap.breadcrumbs", "Breadcrumbs.broken", "the live route is off")),
    ],
    "bl4_third_person": [("camera", True, _called("bl4_third_person.camera_defs", "Finder.gave_up", "the camera modes moved in this game version"))],
    "bl4_backpack_size": [("lost_loot", False, _set("bl4_backpack_size.limits", "_lost_loot_bad", "the Lost Loot limit is left alone"))],
    "bl4_instant_travel": [("stations", True, _false("bl4_instant_travel.stations", "_current", "the stations changed; fast travel uses the portal"))],
    "bl4_unstuck": [("hold", False, _unstuck)],
}


def _link_probe(package: str, whole: bool) -> list[tuple[str, bool, Callable[[], str | None]]]:
    """The BL4 UI Link (if the mod carries it): paused by a game update or by another mod's pak."""

    def probe() -> str | None:
        status = _attr(f"{package}.uilink", "status")
        if not callable(status):
            return None
        state = status()
        if state.get("state") == "game_changed":
            return "its in-game pages are off: a game update changed the page they hook"
        if state.get("state") == "conflict":
            return f"conflict: {state.get('detail')} in Paks/~mods replaces the page script it needs"
        return None

    return [("link", whole, probe)]


PROBES["bl4_builds"] += _link_probe("bl4_builds", whole=False)  # the console command still works
PROBES["bl4_tidy_pause"] = _link_probe("bl4_tidy_pause", whole=True)
# The page parts only: rerolls from the Mod Menu / console, the uvhtp command, the damage
# counting (and its HUD pop-up) and Kairosdex's kills and banners keep working (v2)
PROBES["bl4_reroll"] += _link_probe("bl4_reroll", whole=False)
PROBES["bl4_uvh_teleport"] = _link_probe("bl4_uvh_teleport", whole=False)
PROBES["bl4_damage_meter"] += _link_probe("bl4_damage_meter", whole=False)
PROBES["bl4_weapondex"] += _link_probe("bl4_weapondex", whole=False)  # kills and banners keep working
# v3: the old page paks (4242 / 4244 / 4246 / 4248 / 4249) became link scripts. Their pages'
# fingerprints (vanilla.json) still say whether the script fits this game version: page_current
# is False once the game's own copy changed (None: not checked yet)
PROBES["bl4_item_cards"] += _link_probe("bl4_item_cards", whole=False)  # the Big Encore keys still work
PROBES["bl4_respec"] = [
    ("page", True, _false("bl4_respec.skills_page", "page_current", "a game update changed the Skills menu; right-click respec is off")),
    *_link_probe("bl4_respec", whole=True),
]
PROBES["bl4_instant_tabs"] += [
    ("page", False, _false("bl4_instant_tabs.pages", "page_current", "the fade skip is off (a game update changed the menu pages)")),
    *_link_probe("bl4_instant_tabs", whole=False),
]
PROBES["bl4_weapondex"].append(
    ("banners", False, _false("bl4_weapondex.celebrate_page", "page_current", "first-kill / new-legendary banners are off")))
PROBES["bl4_damage_meter"].append(
    ("popup", False, _false("bl4_damage_meter.popup_page", "page_current", "the boss fight pop-up is off")))

# endregion
# region Shared state


def _hub() -> types.ModuleType:
    hub = sys.modules.get(HUB)
    if hub is None:
        hub = types.ModuleType(HUB)
        hub.__doc__ = "BL4 Fix health state shared by every mod's copy (see its docstring)."
        hub.reports = {}  # (package, part) -> (reason, whole)
        hub.applied = {}  # package -> what's applied to its mod object (see _apply)
        hub.owner = None
        hub.owner_version = 0
        hub.started_at = 0.0
        hub.probed_at = 0.0
        hub.scan = None  # None: not started; [] / list of (package, name, version) once done
        hub.standins = {}  # package -> stand-in Mod for a failed import
        hub.noticed = False
        hub.ticked_at = 0.0
        sys.modules[HUB] = hub
    return hub


def _me() -> types.ModuleType:
    return sys.modules[__name__]


def _elect(hub: types.ModuleType) -> None:
    if hub.owner is _me():
        return
    if hub.owner is None or getattr(hub, "owner_version", 0) < VERSION:
        hub.owner, hub.owner_version = _me(), VERSION


# endregion
# region Reporting (any copy, any thread)


def _package_of(mod: Any) -> str:
    """The top-level package a mod object came from (what sys.modules and the .sdkmod are named)."""
    if isinstance(mod, str):
        return mod
    for name, module in list(sys.modules.items()):
        if "." not in name and getattr(module, "mod", None) is mod:
            return name
    return getattr(mod, "name", "")


def report(mod: Any, part: str, reason: str, *, whole: bool = False) -> None:
    """A part of `mod` (its Mod object or package name) doesn't work: `reason` says what's off."""
    hub = _hub()
    _elect(hub)
    hub.reports[(_package_of(mod), part)] = (reason, whole)
    hub.probed_at = 0.0  # apply at the next tick


def clear(mod: Any, part: str) -> None:
    hub = _hub()
    if hub.reports.pop((_package_of(mod), part), None) is not None:
        hub.probed_at = 0.0


def problems() -> dict[str, list[dict[str, Any]]]:
    """package -> [{part, reason, whole}] for every mod with a problem right now (as last applied)."""
    return {pkg: list(entry["items"]) for pkg, entry in _hub().applied.items() if entry["items"]}


# endregion
# region Game thread


def tick() -> None:
    """Call every frame from the mod's tick: a clock compare unless it's time to look."""
    hub = _hub()
    now = time.monotonic()
    if now - hub.ticked_at < 0.25:
        return
    hub.ticked_at = now
    _elect(hub)
    try:
        hub.owner._owner_tick(hub, now)  # noqa: SLF001 - the owner may be another mod's copy
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[BL4 Fix health] {ex!r}")


def _owner_tick(hub: types.ModuleType, now: float) -> None:
    if not hub.started_at:
        hub.started_at = now
    if now - hub.probed_at >= PROBE_S:
        hub.probed_at = now
        _probe(hub)
    if hub.scan is None and now - hub.started_at >= SCAN_AFTER_S and GAME_DIR is not None:
        hub.scan = False  # running
        threading.Thread(target=_scan_imports, args=(hub, GAME_DIR / "sdk_mods"), name="bl4_health.scan", daemon=True).start()
    if isinstance(hub.scan, list):
        found, hub.scan = hub.scan, True
        _add_standins(hub, found)
    # After the import scan, so the notice names mods that didn't load too
    if not hub.noticed and now - hub.started_at >= NOTICE_AFTER_S and (hub.scan is True or GAME_DIR is None):
        _maybe_notice(hub)
    if (notice := sys.modules.get(f"{__name__}.notice")) is not None:
        notice.tick()  # hides it when its time is up


def _probe(hub: types.ModuleType) -> None:
    wanted: dict[str, list[dict[str, Any]]] = {}
    for package, probes in PROBES.items():
        if package not in sys.modules:
            continue
        for part, whole, probe in probes:
            try:
                reason = probe()
            except Exception:  # noqa: BLE001 - another version of that mod: say nothing
                reason = None
            if reason:
                wanted.setdefault(package, []).append({"part": part, "reason": reason, "whole": whole})
    for (package, part), (reason, whole) in list(hub.reports.items()):
        items = wanted.setdefault(package, [])
        items[:] = [i for i in items if i["part"] != part]
        items.append({"part": part, "reason": reason, "whole": whole})
    for package in set(wanted) | set(hub.applied):
        if package in hub.standins:
            continue  # a mod that didn't load: its stand-in keeps its entry
        items = wanted.get(package, [])
        if [i["reason"] for i in items] != [i["reason"] for i in hub.applied.get(package, {}).get("items", [])]:
            _apply(hub, package, items)


PREFIX = "<font color='#ffcc00'>"


def _own_description(text: str) -> str:
    """The mod's own description, without problems we put above it (a lost entry, a reload)."""
    while text.startswith(PREFIX) and "\n\n" in text:
        text = text.split("\n\n", 1)[1]
    return text


def _mod_object(package: str) -> Any:
    return getattr(sys.modules.get(package), "mod", None)


def _status_for(items: list[dict[str, Any]]) -> str:
    return CONFLICT if items and all(i["reason"].startswith("conflict") for i in items) else NEEDS_UPDATE


def _apply(hub: types.ModuleType, package: str, items: list[dict[str, Any]]) -> None:
    """Shows (or takes back) a mod's problems on its Mod object: status, description, switch."""
    mod = _mod_object(package)
    entry = hub.applied.get(package)
    if mod is None:
        hub.applied.pop(package, None)
        return
    if entry is None or entry.get("mod") is not mod:
        entry = {"mod": mod, "description": _own_description(mod.description), "turned_off": False, "items": []}
    # Back to the mod's own state first
    if "get_status" in vars(mod):
        del mod.get_status
    mod.description = entry["description"]
    if entry["turned_off"]:
        mod.enabling_locked = False
        mod.enable()
        entry["turned_off"] = False
    entry["items"] = items
    if not items:
        hub.applied.pop(package, None)
        logging.info(f"[BL4 Fix health] {mod.name}: working again")
        return
    hub.applied[package] = entry
    status = _status_for(items)
    # One sentence per problem (mod lists collapse line breaks, so no bullets)
    reasons = [i["reason"].removeprefix("conflict: ") for i in items]
    # Capitalized, unless it starts with a file name ("pakchunk0-...pak")
    lines = " ".join(f"{r if '.' in r.split(' ', 1)[0] else r[0].upper() + r[1:]}." for r in reasons)
    head = "Conflict with another mod" if status == CONFLICT else "Needs an update for this game version"
    mod.description = f"{PREFIX}{head}:</font> {lines}\n\n{entry['description']}"
    mod.get_status = lambda: status
    if any(i["whole"] for i in items) and mod.is_enabled:
        # Off for this session only (its saved setting stays on), and its switch locked
        mod.disable(dont_update_setting=True)
        mod.enabling_locked = True
        entry["turned_off"] = True
    logging.warning(f"[BL4 Fix health] {mod.name}: {'; '.join(i['reason'] for i in items)}")
    hub.noticed = False  # a new problem may be worth a notice (the settings file stops repeats)


# endregion
# region Mods that didn't import


def _scan_imports(hub: types.ModuleType, sdk_mods: Path) -> None:
    """Worker: our .sdkmods in sdk_mods whose package isn't loaded (its import failed)."""
    found: list[tuple[str, str, str]] = []
    try:
        for path in sorted(sdk_mods.glob("bl4_*.sdkmod")):
            package = path.stem
            if package in sys.modules:
                continue
            try:
                with zipfile.ZipFile(path) as zf:
                    text = zf.read(f"{package}/pyproject.toml").decode("utf-8", "replace")
            except (OSError, KeyError, zipfile.BadZipFile):
                continue
            # Ours only (author "flame"): never speak for another modder's mod
            if not re.search(r'authors\s*=\s*\[[^\]]*"flame"', text):
                continue
            name = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
            version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            found.append((package, name.group(1) if name else package, version.group(1) if version else ""))
    except OSError:
        pass
    hub.scan = found


def _add_standins(hub: types.ModuleType, found: list[tuple[str, str, str]]) -> None:
    try:
        from mods_base import Mod  # noqa: PLC0415
        from mods_base.mod_list import mod_list, register_mod  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    for package, name, version in found:
        if package in sys.modules or package in hub.standins or any(m.name == name for m in mod_list):
            continue
        standin = Mod(
            name=name,
            author="flame",
            description=(
                "<font color='#ffcc00'>Didn't load.</font> After a game update this usually means it needs"
                " an update too; the SDK log (unrealsdk.log) says what failed."
            ),
            version=version,
            settings_file=None,  # never read or write the real mod's settings
            keybinds=[], options=[], hooks=[], commands=[],
            auto_enable=False,
        )  # fmt: skip
        standin.enabling_locked = True
        standin.get_status = lambda: NEEDS_UPDATE
        register_mod(standin)
        hub.standins[package] = standin
        hub.applied[package] = {"mod": standin, "description": standin.description, "turned_off": False,
                                "items": [{"part": "import", "reason": "it didn't load", "whole": True}]}  # fmt: skip
        logging.warning(f"[BL4 Fix health] {name} didn't load: shown as needing an update")


# endregion
# region The notice


def _build_id() -> str:
    """Steam's build id of the installed game (changes with every patch), or the exe's date."""
    if GAME_DIR is None:
        return ""
    manifest = GAME_DIR.parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    try:
        match = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        return str(int((GAME_DIR / "OakGame/Binaries/Win64/Borderlands4.exe").stat().st_mtime))
    except OSError:
        return ""


def _settings_path() -> Path | None:
    return GAME_DIR / "sdk_mods" / "settings" / SETTINGS_NAME if GAME_DIR is not None else None


def _maybe_notice(hub: types.ModuleType) -> None:
    current = problems()
    if not current:
        return
    try:
        from mods_base import get_pc  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    pc = get_pc(possibly_loading=False)
    if pc is None or getattr(pc, "Pawn", None) is None:
        return  # title screen or loading: wait for gameplay
    key = {"build": _build_id(), "problems": sorted(f"{p}:{i['part']}" for p, items in current.items() for i in items)}
    path = _settings_path()
    seen: Any = None
    if path is not None:
        try:
            seen = json.loads(path.read_text(encoding="utf-8")).get("noticed")
        except (OSError, ValueError, AttributeError):
            seen = None
    hub.noticed = True
    if seen == key:
        return
    names = [hub.applied[p]["mod"].name for p in sorted(current)]
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    menu = "bl4_mod_menu" in sys.modules
    where = "Details in the mod list (F7 or MODS in the pause menu)." if menu else "Details in the mod list (console: mods)."
    title = f"BL4 Fix: {len(names)} mod{'s' if len(names) != 1 else ''} need{'' if len(names) != 1 else 's'} an update"
    from . import notice  # noqa: PLC0415 - UMG: only when there's something to say

    notice.show(pc, title, [shown, "The rest of your mods keep working.", where])
    if path is not None:
        try:
            tmp = path.with_name(f"{SETTINGS_NAME}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"noticed": key}, indent=4), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


# endregion
