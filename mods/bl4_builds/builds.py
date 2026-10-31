"""
Saving and loading builds, and describing them for the page. Call these on the game thread.

Save and load always read the game fresh. The page's description (summaries(cached=True)) comes
from a cache that watch() keeps fresh, a little per frame and only while the page asks for it:
see "Page summary" below.
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
import zlib
from collections.abc import Callable
from importlib import resources
from typing import Any

from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

from . import game, presets

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo/host only
    coop = None

FEATURE = "builds"
SERVICE = "builds.load"

# Equipped actor class -> what to call the slot (first match wins: HeavyWeaponGadget is ordnance)
KINDS = (
    ("gadget", "Ordnance"),
    ("grenade", "Ordnance"),
    ("weapon", "Weapon"),
    ("shield", "Shield"),
    ("repair", "Repkit"),
    ("enhancement", "Enhancement"),
    ("classmod", "Class Mod"),
    ("class_mod", "Class Mod"),
)
WEAPON_SLOTS = 4
_TREE = re.compile(r"_(?:Trunk|Branch)_([A-Za-z]+?)(?:_\d+)?$", re.IGNORECASE)
_SPEC = re.compile(r"Skill_Specializations_([A-Za-z]+)_Base")


class Refused(Exception):
    """Why a save or load didn't happen (nothing was changed)."""


def blocked(*, cached: bool = False) -> str | None:
    """Why builds can't be saved or loaded right now, or None. cached: may be up to BLOCKED_S old."""
    now = time.monotonic()
    if cached and now - _view.blocked_at < BLOCKED_S:
        return _view.blocked
    why = _blocked()
    _view.blocked, _view.blocked_at = why, now
    return why


def _blocked() -> str | None:
    try:
        game._player()  # noqa: SLF001
    except game.BuildError as ex:
        return str(ex)
    if game.character() is None:
        return "Load into the game first."
    return None


# region Slot names


def slot_kinds() -> dict[int, str]:
    """Equip slot -> its kind ("Weapon", "Shield", ...), from what's equipped (or the slot's place)."""
    out: dict[int, str] = {}
    try:
        _, pawn, _ = game._player()  # noqa: SLF001
        slots = pawn.EquippedInventorySlots.items
    except (game.BuildError, AttributeError):
        return out
    for index in range(len(slots)):
        actor = slots[index].InstancedInventory
        name = actor.Class.Name.lower() if actor is not None else ""
        out[index] = next((label for key, label in KINDS if key in name), "Weapon" if index < WEAPON_SLOTS else "Gear")
    return out


def slot_name(slot: int, kinds: dict[int, str]) -> str:
    kind = kinds.get(slot) or ("Weapon" if slot < WEAPON_SLOTS else "Gear")
    return f"Weapon {slot + 1}" if kind == "Weapon" and slot < WEAPON_SLOTS else kind


# endregion
# region Skill names

_skill_names: dict[str, list[str | None]] | None = None


def _skill_names_data() -> dict[str, list[str | None]]:
    """Node names per graph, from Detailed Item Cards' skills.json when it's installed."""
    global _skill_names
    if _skill_names is None:
        _skill_names = {}
        try:
            raw = json.loads((resources.files("bl4_item_cards") / "card_page" / "skills.json").read_text(encoding="utf-8"))
            _skill_names = {graph: [(node or {}).get("name") for node in nodes] for graph, nodes in raw.items()}
        except Exception:  # noqa: BLE001 - names are a nicety
            pass
    return _skill_names


def _node_name(graph: str, index: int) -> str | None:
    names = _skill_names_data().get(graph) or []
    name = names[index] if index < len(names) else None
    if name and (match := _SPEC.match(name)):
        return match.group(1)
    if not name or name.startswith(("SkillDef_", "Skill_", "ST_")):
        return None
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name) if " " not in name else name


_trees: dict[str, dict[str, str]] | None = None


def _tree_data() -> dict[str, dict[str, str]]:
    """Graph -> {"tree": display name, "color": red/green/blue} (trees.json, tools/gen_builds_trees.py)."""
    global _trees
    if _trees is None:
        try:
            _trees = json.loads((resources.files(__package__) / "trees.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _trees = {}
    return _trees


def _tree_of(graph: str) -> tuple[str, str] | None:
    """(tree name, color) of a skill tree graph (trunk or branch), or None for other graphs."""
    info = _tree_data().get(graph)
    if info:
        return info["tree"], info["color"]
    if match := _TREE.search(graph):
        return match.group(1), ""
    return None


def describe_skills(skills: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """For the page: action skill, points per tree (with the skill screen's colors), specializations."""
    action = None
    trees: dict[str, dict[str, Any]] = {}
    specs: list[tuple[str, int]] = []
    perks = 0
    for graph, data in skills.items():
        if graph.endswith("_ActionSkills"):
            # The three action skills come first; the class trait (always picked) after them
            picked = sorted(int(index) for index in data.get("active", {}))
            action = _node_name(graph, picked[0]) if picked else None
        elif graph == "ProgressGraph_Specializations":
            for index, points in data.get("points", {}).items():
                specs.append((_node_name(graph, int(index)) or f"#{int(index) + 1}", int(points)))
        elif graph == "ProgressGraph_Specializations_Skills":
            perks = len(data.get("active", {}))
        elif tree := _tree_of(graph):
            entry = trees.setdefault(tree[0], {"name": tree[0], "color": tree[1], "points": 0})
            entry["points"] += sum(int(p) for p in data.get("points", {}).values())
    spent = sorted((t for t in trees.values() if t["points"]), key=lambda t: -t["points"])
    spec_text = " · ".join(f"{name} {points}" for name, points in sorted(specs, key=lambda t: -t[1]) if points)
    if perks:
        spec_text = (spec_text + " · " if spec_text else "") + f"{perks} perk{'s' if perks != 1 else ''}"
    return {
        "action": action,
        "trees": spent,
        "tree_text": " · ".join(f"{t['name']} {t['points']}" for t in spent),
        "specs": spec_text,
        "points": sum(t["points"] for t in spent),
    }


# endregion
# region Save and load


def save(n: int, labels: dict[str, Any] | None = None) -> str:
    """Saves what's equipped and the skills into build n. labels: slot -> what the page showed."""
    if (why := blocked()) is not None:
        raise Refused(why)
    char = game.character()
    assert char is not None
    kinds = slot_kinds()
    labels = labels or {}
    gear = {}
    for slot, serial in sorted(game.equipped().items()):
        page = labels.get(str(slot)) or {}
        gear[str(slot)] = {
            "serial": serial,
            "kind": kinds.get(slot, ""),
            "label": str(page.get("label") or ""),
            "desc": str(page.get("desc") or ""),
            "rarity": str(page.get("rarity") or ""),
        }
    build = {"saved": time.time(), "gear": gear, "skills": game.read_skills()}
    # Saving over a build keeps its name
    if (old := presets.get(char, n)) is not None and old.get("name"):
        build["name"] = old["name"]
    if not presets.put(char, n, build):
        raise Refused("Couldn't write the builds file (see the SDK log).")
    # Nothing in the game changed: the page's cache stays (presets.version tells it to redraw)
    return f"Build {n} saved."


def _apply(build: dict[str, Any], *, skills: bool, specializations: bool) -> dict[str, Any]:
    """
    Puts a build on the player game.py acts for (ours, or on the host a friend's). Raises Refused
    with nothing changed if it can't start. Returns {"missing": [slot], "failed": [slot],
    "short": {graph: points}, "unpicked": n}.
    """
    # Checked before anything changes, so a build that doesn't fit leaves everything as it is
    if skills and (bad := game.check_skills(build.get("skills", {}), specializations=specializations)):
        raise Refused(f"The build's skills don't fit this character any more ({', '.join(bad)}); nothing was changed.")
    wanted = {int(slot): entry["serial"] for slot, entry in build.get("gear", {}).items() if entry.get("serial")}
    try:
        gear = game.apply_gear(wanted)
    except game.native.NotFound as ex:
        raise Refused(f"This game version moved the equip code ({ex}); Build Presets needs an update.") from ex
    except Exception as ex:  # noqa: BLE001 - tell the player, not just the log
        logging.error("".join(traceback.format_exception(ex)))
        raise Refused(f"Loading the build failed while equipping ({ex!r}); see the SDK log.") from ex
    finally:
        invalidate()
    out: dict[str, Any] = {"missing": sorted(gear.missing), "failed": sorted(gear.failed), "short": {}, "unpicked": 0}
    if skills:
        try:
            result = game.apply_skills(build.get("skills", {}), specializations=specializations)
        except game.BuildError as ex:
            raise Refused(str(ex)) from ex
        except Exception as ex:  # noqa: BLE001
            logging.error("".join(traceback.format_exception(ex)))
            raise Refused(f"The build's gear is on, but putting the skills back failed ({ex!r}); see the SDK log.") from ex
        finally:
            invalidate()
        out["short"] = dict(result.short)
        out["unpicked"] = len(result.unpicked)
    return out


def _describe(n: int, build: dict[str, Any], raw: dict[str, Any], kinds: dict[int, str]) -> tuple[str, list[str]]:
    details = []
    missing = sorted(set(raw.get("missing", [])))
    for slot in missing:
        entry = build["gear"].get(str(slot), {})
        what = entry.get("label") or entry.get("kind") or "item"
        details.append(f"{slot_name(slot, kinds)}: {what} isn't in your backpack anymore, left as it was.")
    for slot in sorted(set(raw.get("failed", []))):
        details.append(f"{slot_name(slot, kinds)}: the game wouldn't equip it.")
    for graph, short in raw.get("short", {}).items():
        details.append(f"{short} point(s) in {graph} couldn't be put back.")
    if raw.get("unpicked"):
        details.append(f"{raw['unpicked']} skill pick(s) couldn't be made (still locked?).")
    headline = f"Build {n} loaded."
    if missing:
        headline = f"Build {n} loaded; {len(missing)} item(s) missing."
    return headline, details


def _serial_key(serial: str) -> int:
    return zlib.crc32(serial.encode("ascii", "replace"))


def load(n: int, *, skills: bool = True, specializations: bool = True,
         on_done: Callable[[str, list[str], bool], None] | None = None) -> tuple[str, list[str]]:  # fmt: skip
    """
    Loads build n. Returns (headline, details). As a co-op client the host's copy of the mod does
    it (equipping and respeccing belong to the host): this returns a "loading" note at once and
    on_done(headline, details, error) gets the outcome later.
    """
    if (why := blocked()) is not None:
        raise Refused(why)
    char = game.character()
    assert char is not None
    # Everything below reads the game itself (backpack, graphs), never the page's cache
    build = presets.get(char, n)
    if build is None:
        raise Refused(f"Build {n} is empty.")
    kinds = slot_kinds()
    if coop is None or not coop.is_client():
        return _describe(n, build, _apply(build, skills=skills, specializations=specializations), kinds)

    if not coop.host_has(FEATURE):
        raise Refused("In co-op the host needs Build Presets to load your build (saving works).")
    # The host knows our items by their serials: send a short key per slot, it finds the serial
    # in our backpack (which it holds)
    have = {item.serial for item in game.backpack()}
    gear = build.get("gear", {})
    request = {
        "gear": {slot: _serial_key(e["serial"]) for slot, e in gear.items() if e.get("serial") in have},
        "missing": [int(slot) for slot, e in gear.items() if e.get("serial") and e["serial"] not in have],
        "skills": build.get("skills", {}) if skills else None,
        "specs": specializations,
    }

    def done(reply: dict[str, Any]) -> None:
        invalidate()
        if not reply.get("ok"):
            text = f"Build {n} wasn't loaded: {reply.get('error') or 'the host refused'}."
            if on_done is not None:
                on_done(text, [], True)
            else:
                logging.info(f"[Build Presets] {text}")
            return
        reply["missing"] = list(reply.get("missing", [])) + request["missing"]
        headline, details = _describe(n, build, reply, kinds)
        if on_done is not None:
            on_done(headline, details, False)
        else:
            logging.info(f"[Build Presets] {' '.join([headline, *details])}")

    coop.request(SERVICE, request, done, timeout=15.0)
    return f"Loading build {n}...", ["Your host's game equips it and sets your skills."]


def _serve_load(req: Any) -> dict[str, Any]:
    """On the host: a friend's load (their saved build, their backpack, their skills)."""
    package = sys.modules.get("bl4_builds")
    if package is not None and not package.mod.is_enabled:
        return {"ok": False, "error": "Build Presets is off on the host"}
    data = req.data
    skills = data.get("skills")
    if skills is not None and not isinstance(skills, dict):
        return {"ok": False, "error": "bad request"}
    wanted = data.get("gear") or {}
    if not isinstance(wanted, dict):
        wanted = {}
    with game.acting_for(req.pc):
        try:
            by_key = {_serial_key(item.serial): item.serial for item in game.backpack()}
            _, pawn, _ = game._player()  # noqa: SLF001
            slot_count = len(pawn.EquippedInventorySlots.items)
        except (game.BuildError, AttributeError) as ex:
            return {"ok": False, "error": str(ex)}
        gear, missing = {}, []
        for slot_s, key in wanted.items():
            # Slot numbers reach the native equip call: only the asker's own slots, and only
            # int serial keys (a modified client could send anything)
            if not (isinstance(slot_s, str) and slot_s.isascii() and slot_s.isdigit()):
                continue
            slot = int(slot_s)
            if slot >= slot_count or isinstance(key, bool) or not isinstance(key, int):
                continue
            serial = by_key.get(key)
            if serial is None:
                missing.append(slot)
            else:
                gear[str(slot)] = {"serial": serial}
        build = {"gear": gear, "skills": skills or {}}
        try:
            raw = _apply(build, skills=skills is not None, specializations=bool(data.get("specs", True)))
        except Refused as ex:
            logging.warning(f"[Build Presets] a friend's build wasn't loaded: {ex}")
            return {"ok": False, "error": str(ex)}
    raw["missing"] = raw["missing"] + missing
    logging.info(f"[Build Presets] loaded a build for a friend ({len(gear)} item(s))")
    return {"ok": True, **raw}


MAX_NAME = 24


def rename(n: int, name: str) -> str:
    char = game.character()
    if char is None:
        raise Refused("Load into the game first.")
    build = presets.get(char, n)
    if build is None:
        raise Refused(f"Build {n} is empty.")
    name = " ".join(str(name).split())[:MAX_NAME]
    if name:
        build["name"] = name
    else:
        build.pop("name", None)
    if not presets.put(char, n, build):
        raise Refused("Couldn't write the builds file (see the SDK log).")
    return f"Build {n} is now called {name}." if name else f"Build {n}'s name was cleared."


def clear(n: int) -> str:
    char = game.character()
    if char is None:
        raise Refused("Load into the game first.")
    if not presets.clear(char, n):
        raise Refused("Couldn't write the builds file (see the SDK log).")
    return f"Build {n} cleared."


# endregion
# region Page summary

# The page asks for the squares up to 20 times a second while the inventory shows, so they come
# from a cache: summaries(cached=True) costs a few reads (the PC and character, to notice a new
# world) and returns the same list until something it's made of changes. The expensive reads
# (every backpack item's serial, every skill graph's nodes: a few ms together) are done by watch(),
# called every frame, a slice per frame (ITEMS_PER_STEP items or GRAPHS_PER_STEP graphs), and only
# while the page keeps asking (ACTIVE_S):
#   - items: when the backpack's cheap change signal moves (game.backpack_signal(), checked every
#     SIGNAL_S), and every ITEMS_VERIFY_S anyway in case something changed without it
#   - skills: every SKILLS_S (nothing cheap says they changed)
#   - both at once when the page starts asking again (the menu opened) and after our own actions
# A new PC or character drops everything; the first ask after that reads at once (one frame).
# Saves and loads never use any of this: they read the game themselves.

ACTIVE_S = 1.0
SIGNAL_S = 0.2
SKILLS_S = 1.0
ITEMS_VERIFY_S = 5.0
BLOCKED_S = 0.5
ITEMS_PER_STEP = 40
GRAPHS_PER_STEP = 3


class _view:  # noqa: N801 - module state
    pc = WeakPointer()
    char: str | None = None
    items: list[game.Item] | None = None
    items_v = 0
    items_at = 0.0  # when the last items read started; 0 = read again now
    signal: Any = None  # backpack_signal() from just before the cached items were read
    skills: dict[str, Any] | None = None
    skills_v = 0
    skills_at = 0.0
    kinds: dict[int, str] = {}  # noqa: RUF012 - slot_kinds() as of items_v
    kinds_v = -1
    summary: list[dict[str, Any]] = []  # noqa: RUF012
    summary_key: Any = None
    blocked: str | None = None
    blocked_at = 0.0
    # watch()
    wanted_at = 0.0  # the page last asked
    active = False
    checked_at = 0.0
    sweep: Any = None  # the read in progress (a generator), one at a time
    sweep_kind = ""
    sweep_signal: Any = None
    sweep_verify = False
    missed_logged = False


def _reset(pc: Any, char: str | None) -> None:
    """A new PC or character: nothing cached is theirs."""
    _view.pc = WeakPointer(pc)
    _view.char = char
    _view.items = _view.skills = _view.signal = _view.sweep = None
    _view.items_at = _view.skills_at = _view.blocked_at = 0.0
    _view.summary_key = None


def invalidate() -> None:
    """Our own save / load / clear may have changed things: re-read items and skills right away."""
    _view.sweep = None
    _view.items_at = _view.skills_at = _view.blocked_at = 0.0


def _set_items(items: list[game.Item], signal: Any) -> bool:
    _view.signal = signal
    if items == _view.items:
        return False
    _view.items = items
    _view.items_v += 1
    return True


def _set_skills(skills: dict[str, Any]) -> bool:
    if skills == _view.skills:
        return False
    _view.skills = skills
    _view.skills_v += 1
    return True


def _read_items_now() -> None:
    signal = game.backpack_signal()
    _view.items_at = time.monotonic()
    _set_items(game.backpack(), signal)


def _read_skills_now() -> None:
    _view.skills_at = time.monotonic()
    try:
        _set_skills(game.read_skills())
    except game.BuildError:
        _set_skills({})


def _items_sweep() -> Any:
    """Reads the backpack, ITEMS_PER_STEP entries per step; the list is the generator's return value."""
    out: list[game.Item] = []
    start = 0
    while True:
        # Found again every step: nothing from the game is kept across frames
        _, _, state = game._player()  # noqa: SLF001
        entries = state.BackpackItems.items
        count = len(entries)
        end = min(count, start + ITEMS_PER_STEP)
        for i in range(start, end):
            if (item := game.read_entry(entries[i])) is not None:
                out.append(item)
        del state, entries
        start = end
        if start >= count:
            return out
        yield


def _skills_sweep() -> Any:
    """Reads the skill graphs, GRAPHS_PER_STEP per step; the dict is the generator's return value."""
    refs = [(game._graph_name(g), WeakPointer(g)) for g in game.build_graphs()]  # noqa: SLF001
    yield
    out: dict[str, Any] = {}
    for start in range(0, len(refs), GRAPHS_PER_STEP):
        for name, ref in refs[start : start + GRAPHS_PER_STEP]:
            graph = ref()
            if graph is None:
                raise game.BuildError("the skill graphs went away")
            out[name] = game.read_graph(graph)
            del graph
        yield
    return out


def watch() -> bool:
    """
    Call every frame. While the page asks for the squares, keeps their cache fresh, a slice of
    reading per frame; does nothing otherwise. True when what the squares show changed.
    """
    now = time.monotonic()
    if now - _view.wanted_at >= ACTIVE_S or _view.char is None:
        _view.active = False
        _view.sweep = None
        return False
    if not _view.active:
        # The page started asking again (the inventory opened): anything may have changed since
        # (unless summaries() just read it)
        _view.active = True
        if now - _view.items_at > ACTIVE_S:
            _view.items_at = 0.0
        if now - _view.skills_at > ACTIVE_S:
            _view.skills_at = 0.0
    try:
        return _watch(now)
    except game.BuildError:
        _view.sweep = None
        return False
    except Exception:
        _view.sweep = None
        raise


def _watch(now: float) -> bool:
    if _view.sweep is not None:
        try:
            next(_view.sweep)
        except StopIteration as done:
            _view.sweep = None
            if _view.sweep_kind == "skills":
                return _set_skills(done.value)
            changed = _set_items(done.value, _view.sweep_signal)
            if changed and _view.sweep_verify and not _view.missed_logged:
                _view.missed_logged = True
                logging.warning("[Build Presets] The backpack changed without its change signal; the squares caught up late.")
            return changed
        return False
    if now - _view.skills_at >= SKILLS_S:
        _view.skills_at = now
        _view.sweep, _view.sweep_kind = _skills_sweep(), "skills"
        return False
    forced = _view.items_at == 0.0
    verify = not forced and now - _view.items_at >= ITEMS_VERIFY_S
    signal = None
    if forced or verify or now - _view.checked_at >= SIGNAL_S:
        _view.checked_at = now
        signal = game.backpack_signal()
    if forced or verify or (signal is not None and signal != _view.signal):
        _view.items_at = now
        _view.sweep, _view.sweep_kind = _items_sweep(), "items"
        # The signal from before the read: a change during it moves the signal again, read again
        _view.sweep_signal = signal
        _view.sweep_verify = verify and signal == _view.signal
    return False


def summaries(*, cached: bool = False) -> list[dict[str, Any]]:
    """
    What each of the five squares shows. cached (the page): from the cache watch() keeps fresh
    (within about a second); otherwise everything is read now.
    """
    pc = get_pc(possibly_loading=True)
    char = game.character()
    if char is None:
        return []
    if _view.pc() is not pc or _view.char != char:
        _reset(pc, char)
    try:
        if not cached or _view.items is None:
            _read_items_now()
        if not cached or _view.skills is None:
            _read_skills_now()
    except game.BuildError:
        return []
    if cached:
        _view.wanted_at = time.monotonic()
    if _view.kinds_v != _view.items_v:
        _view.kinds, _view.kinds_v = slot_kinds(), _view.items_v
    key = (char, _view.items_v, _view.skills_v, presets.version)
    if key != _view.summary_key:
        _view.summary = _build_summaries(char, _view.items or [], _view.skills or {}, _view.kinds)
        _view.summary_key = key
    return _view.summary


def _build_summaries(char: str, items: list[game.Item], current_skills: dict[str, Any],
                     kinds: dict[int, str]) -> list[dict[str, Any]]:  # fmt: skip
    serials = {item.serial for item in items}
    equipped = {item.slot: item.serial for item in items if item.slot >= 0}
    out = []
    for n, build in enumerate(presets.all_of(char), 1):
        if build is None:
            out.append({"n": n, "empty": True})
            continue
        gear = []
        for slot, entry in sorted(build.get("gear", {}).items(), key=lambda kv: int(kv[0])):
            gear.append({
                "slot": slot_name(int(slot), kinds),
                "label": entry.get("label") or entry.get("kind") or "Item",
                "desc": entry.get("desc") or "",
                "rarity": entry.get("rarity") or "",
                "have": entry.get("serial") in serials,
                "on": equipped.get(int(slot)) == entry.get("serial"),
            })  # fmt: skip
        skills = build.get("skills", {})
        wearing = all(g["on"] for g in gear) and _skills_match(skills, current_skills)
        out.append({
            "n": n,
            "empty": False,
            "name": build.get("name") or "",
            "saved": time.strftime("%b %d, %H:%M", time.localtime(build.get("saved", 0))),
            "gear": gear,
            "missing": sum(1 for g in gear if not g["have"]),
            "current": wearing,
            **describe_skills(skills),
        })  # fmt: skip
    return out


def _skills_match(saved: dict[str, Any], current: dict[str, Any]) -> bool:
    for graph, data in saved.items():
        now = current.get(graph)
        if now is None:
            continue
        key = "active" if data.get("type") == game.ACTIVATION else "points"
        if {k: int(v) for k, v in data.get(key, {}).items()} != {k: int(v) for k, v in now.get(key, {}).items()}:
            return False
    return True


# endregion


if coop is not None:
    coop.handle(SERVICE, _serve_load)
