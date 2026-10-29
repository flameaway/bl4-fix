"""
Live skill numbers for the detailed item cards.

Class mod cards only get each skill's name, "+N" and description from the game. This works out
what the skill does at your current points and with the class mod's points, the way the Skills
menu tooltip would, and leaves it in a JSON file the card's script reads (the UI can read loose
files under uiresources/, no restart needed).

- card_page/skills.json (made by tools/gen_item_cards.py from the game's data) has every skill
  tree node: name, max rank, and its tooltip lines like "Splash Damage: {0} {1}" with the
  attribute behind each number.
- The game evaluates those attributes for the player (GbxAttributeBlueprintLibrary), giving the
  current value and the per-point step (the tooltip's "next level" number).
- Your points come from the skill tree graphs on the character.

The file maps "<graph>,<node>" (the same text the card's skill entries carry in loc_text) to the
skill's max rank, points spent, bonus points, and its tooltip lines. Each line is the tooltip text
with "{0}"-style placeholders and, per placeholder, the number formatted for every rank from 0 to
max + 6, so the card can show "your points -> with this class mod" without any maths of its own.

Firmware works the same way: card_page/firmware.json has each firmware's three tiers (how many
equipped pieces unlock it, its text, the attribute behind each number), and the live file gets
them under "$firmware" with the numbers filled in for your character.

Cost: a full pass is a few hundred attribute
evaluations, too much for one frame. So a pass is a job done in slices of under a millisecond, at
most one slice per frame: read the skill graphs, evaluate the attributes, and only if any input
differs from the last pass, render the changed skills, serialize, and hand the text to a writer
thread. Passes start every second while the mouse cursor is up (menus), every 5 s otherwise, and
at once when a menu opens or the character changes; firmware numbers are re-evaluated every 10 s.
"""

from __future__ import annotations

import json
import math
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

from . import card_page

LIVE_PATH = "bl4direct/skills_live.json"
# Keys in the live file for player info and firmware rather than a skill
PLAYER_KEY = "$player"
FIRMWARE_KEY = "$firmware"
UVH_ATTRIBUTE = "vault_hunter_level"
# Class mods add up to a handful of points; covers ranks past max for any of them
EXTRA_RANKS = 6
MISSING = math.nan

# Game-thread budget. The camera hook fires several times a frame: calls closer together than
# STEP_GAP_S count as one frame and get at most one slice.
STEP_BUDGET_S = 0.0005  # a slice starts no new unit after this (a unit is ~0.03-0.3 ms)
STEP_GAP_S = 0.005
CHECK_S = 0.25  # how often to look at whether a pass is due, while none runs
MENU_PASS_S = 1.0  # cursor up (menus): cards show there, keep them fresh
GAMEPLAY_PASS_S = 5.0  # cursor hidden: only ground-loot cards on the HUD
FIRMWARE_PASS_S = 10.0
PUBLISH_S = 1.0  # dev only: republish our loose script/stylesheet from source

_skills: dict[str, list[dict | None]] | None = None
_firmware: dict[str, dict] | None = None
_last_written: str | None = None


class _S:
    library: Any = WeakPointer()  # GbxAttributeBlueprintLibrary's CDO
    job: _Pass | None = None
    last_check = 0.0
    last_step = 0.0
    last_publish = 0.0
    last_pass = -math.inf  # when the last pass started
    last_pawn = 0  # address of the character the last pass was for
    menu = False  # cursor up at the last check
    # Raw inputs of the last pass that rendered; equal inputs mean the same file
    last_key: Any = None
    # Firmware numbers, re-evaluated every FIRMWARE_PASS_S: {attribute: value}
    firmware_values: dict[str, float] | None = None
    firmware_at = -math.inf
    # Rendered and serialized entries, kept while their inputs stay the same (bounded by the
    # static data): firmware -> (inputs, '"key":{...}'), (graph, node index) -> (inputs, '"key":{...}')
    firmware_rendered: dict[str, tuple[Any, str]] = {}
    nodes: dict[tuple[str, int], tuple[Any, str]] = {}
    plans: dict[tuple[str, ...], list[str]] = {}  # graph names -> attributes to evaluate


def _static_skills() -> dict[str, list[dict | None]]:
    global _skills
    if _skills is None:
        _skills = json.loads((resources.files(card_page) / "skills.json").read_text(encoding="utf-8"))
    return _skills


def _static_firmware() -> dict[str, dict]:
    global _firmware
    if _firmware is None:
        _firmware = json.loads((resources.files(card_page) / "firmware.json").read_text(encoding="utf-8"))
    return _firmware


def _library() -> Any:
    """The attribute library's class default object, found once (it lives as long as the game)."""
    library = _S.library()
    if library is None:
        library = unrealsdk.find_class("GbxAttributeBlueprintLibrary").ClassDefaultObject
        _S.library = WeakPointer(library)
    return library


def format_number(value: float, flags: dict[str, str]) -> str:
    if flags.get("bdisplayasoneminus") == "true":
        value = 1 - value
    # "Repkit Cooldown is reduced by +20%": the text says which way, the number is a size
    if flags.get("signstyle") == "Positive":
        value = abs(value)
    percent = flags.get("bdisplayaspercentage") == "true"
    if percent:
        value *= 100
    if flags.get("roundingmode") == "RoundToInt":
        value = round(value)
    digits = int(flags.get("floatprecision", "2"))
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    if flags.get("bdisplayplussign") == "true" and value >= 0:
        text = "+" + text
    return text + ("%" if percent else "")


def _line_values(line: dict, evaluate: Any) -> list[tuple[str, float, float, dict]] | None:
    """(placeholder, value now, change per point, flags) for each shown number, or None."""
    args = line["args"]
    shown = []
    for key, arg in args.items():
        if arg["preview"]:
            continue
        value = evaluate(arg["attribute"])
        if math.isnan(value):
            return None
        # The tooltip's "next level" number for the same stat is the per-point step
        step = 0.0
        for other in args.values():
            if other["preview"] and other["attribute"] == arg["attribute"] + "_NextLevel":
                step = evaluate(other["attribute"])
                step = 0.0 if math.isnan(step) else step
        shown.append(("{" + key + "}", value, step, arg))
    return shown


def _line_attributes(line: dict) -> list[str]:
    """Every attribute _line_values may evaluate for this line."""
    args = line["args"]
    names = []
    for arg in args.values():
        if arg["preview"]:
            continue
        names.append(arg["attribute"])
        names += [
            other["attribute"]
            for other in args.values()
            if other["preview"] and other["attribute"] == arg["attribute"] + "_NextLevel"
        ]
    return names


def _render(line: dict, values: list[tuple[str, float, float, dict]], grade: int, ranks: int) -> dict:
    text = line["text"]
    for key, arg in line["args"].items():
        if arg["preview"]:
            text = text.replace("{" + key + "}", "")
    return {
        "text": " ".join(text.split()),
        "values": {
            placeholder: [format_number(value + (rank - grade) * step, flags) for rank in range(ranks)]
            for placeholder, value, step, flags in values
        },
    }


def _render_node(node: dict, spent: int | None, bonus: int | None, evaluate: Any) -> dict:
    grade = (spent or 0) + (bonus or 0)
    lines = []
    for line in node["stats"]:
        values = _line_values(line, evaluate)
        if values is None:
            continue
        lines.append(_render(line, values, grade, node["max"] + EXTRA_RANKS + 1))
    return {"name": node["name"], "max": node["max"], "spent": spent, "bonus": bonus, "lines": lines}


def _render_one_firmware(firmware: dict, evaluate: Any) -> dict:
    tiers = []
    for tier in firmware["tiers"]:
        lines = []
        for line in tier["lines"]:
            values = _line_values(line, evaluate)
            if values is not None:
                lines.append(_render(line, values, 0, 1))
        tiers.append({"count": tier["count"], "lines": lines})
    return {"name": firmware["name"], "tiers": tiers}


def _render_firmware(evaluate: Any) -> dict[str, Any]:
    return {key: _render_one_firmware(firmware, evaluate) for key, firmware in _static_firmware().items()}


def _evaluator(pawn: Any) -> Any:
    library = _library()
    cache: dict[str, float] = {}

    def evaluate(name: str) -> float:
        if name not in cache:
            cache[name] = float(library.GetValueOfAttribute(name, pawn, MISSING))
        return cache[name]

    return evaluate


# One-shot versions (all in one call): for the bridge and for checking the sliced job against.
def collect_firmware(pawn: Any) -> dict[str, Any]:
    """{firmware: {"name", "tiers": [{"count", "lines": [{"text", "values"}]}]}} for this character."""
    return _render_firmware(_evaluator(pawn))


def collect(pawn: Any) -> dict[str, Any]:
    evaluate = _evaluator(pawn)
    skills = _static_skills()
    out: dict[str, Any] = {}
    for graph in pawn.GbxProgressionManager.ProgressGraphs:
        graph_name = graph.ProgressGraphDef._name  # noqa: SLF001
        nodes = skills.get(graph_name)
        if not nodes:
            continue
        live = list(graph.nodes)
        # Our node order must match the game's; if not (a patch?), show no points rather than wrong ones
        aligned = len(live) == len(nodes)
        for index, node in enumerate(nodes):
            if node is None:
                continue
            spent = live[index].ProgressPointsSpent if aligned else None
            bonus = live[index].BonusPoints if aligned else None
            out[f"{graph_name},{node['id']}"] = _render_node(node, spent, bonus, evaluate)
    return out


def _uvh_from(value: float) -> int | None:
    return None if math.isnan(value) else int(round(value))


def _piece(key: str, entry: Any) -> str:
    """One '"key":value' member of the live file, exactly as json.dumps of the whole dict writes it."""
    return json.dumps(key) + ":" + json.dumps(entry, separators=(",", ":"))


def _same(value: float) -> float | None:
    """A value for comparing passes: NaN never equals itself, so missing values become None."""
    return None if value != value else value  # noqa: PLR0124


# region The sliced pass


class _Pass:
    """
    One refresh, done a unit at a time across frames. Units that touch the game (graph reads,
    attribute evaluations) re-check every frame that it's still the same PC and character, and
    the job is dropped otherwise; the character is only held as a WeakPointer between frames.
    The rest (render, serialize) is plain Python.
    """

    def __init__(self, pc: Any, pawn: Any, skills: bool, firmware: bool, firmware_fresh: bool) -> None:
        self.pc = WeakPointer(pc)
        self.pc_address = pc._get_address()  # noqa: SLF001
        self.pawn = WeakPointer(pawn)
        self.pawn_address = pawn._get_address()  # noqa: SLF001
        self.skills = skills  # skill data matches the game (else no skills, as before)
        self.firmware = firmware  # firmware data matches the game
        self.firmware_fresh = firmware_fresh  # evaluate firmware this pass (else reuse the last)
        self.phase = "graphs"
        self.graph_index = 0
        # (graph name, spent per node or None, bonus per node or None), in the game's order
        self.graphs: list[tuple[str, tuple | None, tuple | None]] = []
        self.skill_names: list[str] = []  # skill attributes and the UVH level
        self.names: list[str] = []  # those plus, on a firmware pass, the firmware attributes
        self.name_index = 0
        self.values: dict[str, float] = {}
        self.firmware_values: dict[str, float] = {}
        self.build: list[tuple[int, int]] = []  # (index in graphs, node index)
        self.build_index = 0
        self.firmware_keys: list[str] = []
        self.firmware_index = 0
        self.firmware_out: list[str] = []
        self.out: dict[str, str] = {}  # key -> its serialized '"key":value' member
        self.key: Any = None

    def live_pawn(self) -> Any:
        """The character, if this pass still belongs to it; None means drop the pass."""
        pawn = self.pawn()
        pc = self.pc()
        if pawn is None or pc is None:
            return None
        current = get_pc(possibly_loading=True)
        if current is None or current._get_address() != self.pc_address:  # noqa: SLF001
            return None
        controlled = pc.Pawn
        if controlled is None or controlled._get_address() != self.pawn_address:  # noqa: SLF001
            return None
        return pawn

    def needs_game(self) -> bool:
        return self.phase in ("graphs", "attributes")

    def unit(self, pawn: Any) -> bool:
        """Does one unit of work; True when the pass is finished."""
        if self.phase == "graphs":
            self._graph_unit(pawn)
        elif self.phase == "attributes":
            self._attribute_unit(pawn)
        elif self.phase == "build":
            self._build_unit()
        elif self.phase == "dump":
            self._dump()
            return True
        return self.phase == "done"

    def _graph_unit(self, pawn: Any) -> None:
        if not self.skills:
            self._plan()
            return
        graphs = pawn.GbxProgressionManager.ProgressGraphs
        if self.graph_index >= len(graphs):
            self._plan()
            return
        graph = graphs[self.graph_index]
        self.graph_index += 1
        graph_def = graph.ProgressGraphDef
        if graph_def is None:
            return
        name = graph_def._name  # noqa: SLF001
        nodes = _static_skills().get(name)
        if not nodes:
            return
        live = list(graph.nodes)
        # Our node order must match the game's; if not (a patch?), show no points rather than wrong ones
        if len(live) != len(nodes):
            self.graphs.append((name, None, None))
            return
        spent = tuple(None if node is None else live[i].ProgressPointsSpent for i, node in enumerate(nodes))
        bonus = tuple(None if node is None else live[i].BonusPoints for i, node in enumerate(nodes))
        self.graphs.append((name, spent, bonus))

    def _plan(self) -> None:
        names = tuple(name for name, _, _ in self.graphs)
        plan = _S.plans.get(names)
        if plan is None:
            skills = _static_skills()
            plan = list(dict.fromkeys(
                attribute
                for name in names
                for node in skills[name]
                if node is not None
                for line in node["stats"]
                for attribute in _line_attributes(line)
            ))
            if len(_S.plans) >= 16:  # a few per class at most; bounded anyway
                _S.plans.clear()
            _S.plans[names] = plan
        self.skill_names = [*plan, UVH_ATTRIBUTE]
        self.names = self.skill_names
        if self.firmware and self.firmware_fresh:
            self.names = self.skill_names + _firmware_plan()
        self.phase = "attributes"

    def _attribute_unit(self, pawn: Any) -> None:
        if self.name_index >= len(self.names):
            self._compare()
            return
        name = self.names[self.name_index]
        self.name_index += 1
        self.values[name] = float(_library().GetValueOfAttribute(name, pawn, MISSING))

    def _compare(self) -> None:
        """All inputs are in: stop here if they're the same as the last pass's."""
        if self.firmware:
            if self.firmware_fresh:
                _S.firmware_values = {name: self.values[name] for name in _firmware_plan()}
            self.firmware_values = _S.firmware_values or {}
        firmware_key = (
            tuple(_same(self.firmware_values.get(name, MISSING)) for name in _firmware_plan())
            if self.firmware
            else None
        )
        self.key = (
            self.skills,
            tuple(self.graphs),
            tuple(_same(self.values[name]) for name in self.skill_names),
            firmware_key,
        )
        if self.key == _S.last_key:
            self.phase = "done"
            return
        skills = _static_skills()
        self.build = [
            (at, index)
            for at, (name, _, _) in enumerate(self.graphs)
            for index, node in enumerate(skills[name])
            if node is not None
        ]
        self.firmware_keys = list(_static_firmware()) if self.firmware else []
        self.phase = "build"

    def _evaluate(self, name: str) -> float:
        value = self.values.get(name)
        if value is None:
            value = self.firmware_values.get(name, MISSING)
        return value

    def _build_unit(self) -> None:
        """One skill, or one firmware, per unit; unchanged ones come from the cache."""
        if self.build_index >= len(self.build):
            if self.firmware and self.firmware_index < len(self.firmware_keys):
                self._firmware_unit()
                return
            self.out[PLAYER_KEY] = _piece(PLAYER_KEY, {"uvh": _uvh_from(self.values[UVH_ATTRIBUTE])})
            if self.firmware:
                self.out[FIRMWARE_KEY] = json.dumps(FIRMWARE_KEY) + ":{" + ",".join(self.firmware_out) + "}"
            self.phase = "dump"
            return
        at, index = self.build[self.build_index]
        self.build_index += 1
        graph_name, spent_all, bonus_all = self.graphs[at]
        node = _static_skills()[graph_name][index]
        spent = spent_all[index] if spent_all is not None else None
        bonus = bonus_all[index] if bonus_all is not None else None
        inputs = (spent, bonus, tuple(_same(self._evaluate(a)) for a in _node_attributes(graph_name, index)))
        key = f"{graph_name},{node['id']}"
        cached = _S.nodes.get((graph_name, index))
        if cached is None or cached[0] != inputs:
            cached = (inputs, _piece(key, _render_node(node, spent, bonus, self._evaluate)))
            _S.nodes[(graph_name, index)] = cached
        # A dict, like collect(): a graph listed twice keeps its first place and its last numbers
        self.out[key] = cached[1]

    def _firmware_unit(self) -> None:
        key = self.firmware_keys[self.firmware_index]
        self.firmware_index += 1
        firmware = _static_firmware()[key]
        inputs = tuple(_same(self.firmware_values.get(name, MISSING)) for name in _firmware_attributes(key))
        cached = _S.firmware_rendered.get(key)
        if cached is None or cached[0] != inputs:
            entry = _render_one_firmware(firmware, lambda name: self.firmware_values.get(name, MISSING))
            cached = (inputs, _piece(key, entry))
            _S.firmware_rendered[key] = cached
        self.firmware_out.append(cached[1])

    def _dump(self) -> None:
        global _last_written
        _S.last_key = self.key
        # Members were serialized as they were rendered (and cached), so this is only a join
        text = "{" + ",".join(self.out.values()) + "}"
        if text == _last_written:
            return
        _last_written = text
        _Writer.write(text)


_node_attribute_cache: dict[tuple[str, int], tuple[str, ...]] = {}
_firmware_plan_cache: list[str] | None = None
_firmware_attribute_cache: dict[str, tuple[str, ...]] = {}


def _node_attributes(graph_name: str, index: int) -> tuple[str, ...]:
    key = (graph_name, index)
    names = _node_attribute_cache.get(key)
    if names is None:
        node = _static_skills()[graph_name][index]
        names = tuple(a for line in node["stats"] for a in _line_attributes(line))
        _node_attribute_cache[key] = names
    return names


def _firmware_attributes(key: str) -> tuple[str, ...]:
    names = _firmware_attribute_cache.get(key)
    if names is None:
        tiers = _static_firmware()[key]["tiers"]
        names = tuple(a for tier in tiers for line in tier["lines"] for a in _line_attributes(line))
        _firmware_attribute_cache[key] = names
    return names


def _firmware_plan() -> list[str]:
    global _firmware_plan_cache
    if _firmware_plan_cache is None:
        _firmware_plan_cache = list(dict.fromkeys(
            attribute
            for firmware in _static_firmware().values()
            for tier in firmware["tiers"]
            for line in tier["lines"]
            for attribute in _line_attributes(line)
        ))
    return _firmware_plan_cache


class _Writer:
    """Writes the live file on a worker thread (file I/O stays off the game thread); latest wins."""

    lock = threading.Lock()
    pending: str | None = None
    busy = False
    target: Path | None = None
    failed: str | None = None  # logged from the game thread

    @classmethod
    def write(cls, text: str) -> None:
        with cls.lock:
            cls.pending = text
            if cls.busy:
                return
            cls.busy = True
        threading.Thread(target=cls._run, name="bl4_item_cards live file", daemon=True).start()

    @classmethod
    def _run(cls) -> None:
        global _last_written
        while True:
            with cls.lock:
                text, cls.pending = cls.pending, None
                if text is None or cls.target is None:
                    cls.busy = False
                    return
                target = cls.target
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(target)
            except OSError as ex:
                # Try again on the next pass that renders
                _last_written = None
                _S.last_key = None
                cls.failed = f"[Item Cards] couldn't write {target.name}: {ex}"


def _start_pass(game_dir: Path, now: float) -> None:
    """Starts a pass if one is due. Cheap: a few property reads, at most every CHECK_S."""
    if (failed := _Writer.failed) is not None:
        _Writer.failed = None
        logging.warning(failed)
    pc = get_pc(possibly_loading=True)
    if pc is None:
        _S.menu = False
        return
    pawn = pc.Pawn
    if pawn is None or not hasattr(pawn, "GbxProgressionManager") or pawn.GbxProgressionManager is None:
        return
    menu = bool(pc.bShowMouseCursor)
    opened = menu and not _S.menu
    _S.menu = menu
    address = pawn._get_address()  # noqa: SLF001
    new_pawn = address != _S.last_pawn
    interval = MENU_PASS_S if menu else GAMEPLAY_PASS_S
    if not (new_pawn or opened or now - _S.last_pass >= interval):
        return
    if new_pawn:
        _S.firmware_values = None
    firmware = card_page.data_current(game_dir, "firmware_data", check=False)
    fresh = firmware and (_S.firmware_values is None or now - _S.firmware_at >= FIRMWARE_PASS_S)
    if fresh:
        _S.firmware_at = now
    _Writer.target = game_dir / card_page.UI_PREFIX / LIVE_PATH
    # After a game patch our skill list may no longer line up with the game's: show no numbers.
    # Checked when the mod is enabled (see card_page.check_data), never here.
    skills = card_page.skill_data_current(game_dir, check=False)
    _S.job = _Pass(pc, pawn, skills, firmware, fresh)
    _S.last_pass = now
    _S.last_step = now  # its first slice goes in a later frame
    _S.last_pawn = address


def _step(job: _Pass) -> None:
    """One slice of the pass: units until the budget is spent."""
    started = time.perf_counter()
    pawn = job.live_pawn() if job.needs_game() else None
    if job.needs_game() and pawn is None:
        _S.job = None  # another world or character: never mix two
        return
    ran = False
    while True:
        # Joining the file and starting the writer: a slice of its own
        if job.phase == "dump" and ran:
            return
        if job.unit(pawn):
            _S.job = None
            return
        ran = True
        if time.perf_counter() - started >= STEP_BUDGET_S:
            return


def tick(game_dir: Path) -> None:
    """
    Called on every camera hook call (several per frame); throttles itself. Advances the current
    pass by one slice, or starts one when it's due. Game thread.
    """
    now = time.monotonic()
    if card_page.FROM_SOURCE and now - _S.last_publish >= PUBLISH_S:
        _S.last_publish = now
        # Dev: edits to the script and styles go live within a second (the page reloads them)
        card_page.publish_files(game_dir)
    job = _S.job
    if job is not None:
        if now - _S.last_step < STEP_GAP_S:
            return
        _S.last_step = now
        try:
            _step(job)
        except Exception:
            _S.job = None
            raise
        return
    if now - _S.last_check < CHECK_S:
        return
    _S.last_check = now
    _start_pass(game_dir, now)


def refresh_now(game_dir: Path) -> None:
    """Does a whole pass in one call (bridge/debug only: this is the hitch the slicing avoids)."""
    _S.last_pass = -math.inf
    _S.last_key = None
    _S.job = None
    _start_pass(game_dir, time.monotonic())
    while (job := _S.job) is not None:
        _step(job)
