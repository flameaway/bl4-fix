"""
Checks Kairosdex's pictures at the title screen (mods/bl4_weapondex/titlebake.py) offline: when
it starts, what it asks the boss bake and the item bake for, and what it does when a run ends or is
cut short.
Stand-ins for the game (mods_base, unrealsdk) and for its sibling modules; a fake clock.

    python dev/kairosdex_titlebake_test.py
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_weapondex"
GAME = Path("C:/game")
BOSSES = ["a", "b", "c", "d"]
ITEMS = ["x", "y", "z"]  # z has no roll rules


class Clock:
    now = 1000.0

    @classmethod
    def monotonic(cls) -> float:
        return cls.now


class Obj(types.SimpleNamespace):
    pass


class WeakPointer:
    def __init__(self, obj: object = None) -> None:
        self.obj = obj

    def __call__(self) -> object:
        return self.obj


# region Stand-ins

logs: list[str] = []
world = Obj(name="World_P")
pc = Obj(Pawn=None, bShowMouseCursor=True)
engine = Obj(GameViewport=Obj(World=world))

mods_base = types.ModuleType("mods_base")
mods_base.ENGINE = engine
mods_base.get_pc = lambda possibly_loading=False: pc
unrealsdk = types.ModuleType("unrealsdk")
unrealsdk.logging = Obj(info=logs.append, error=logs.append, warning=logs.append)
unreal = types.ModuleType("unrealsdk.unreal")
unreal.WeakPointer = WeakPointer
unrealsdk.unreal = unreal
sys.modules.update({"mods_base": mods_base, "unrealsdk": unrealsdk, "unrealsdk.unreal": unreal})

PKG = "kdx_titlebake_test"
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(MOD)]
sys.modules[PKG] = pkg


class Bake:
    """bake.py: start / stop / running, S.done / S.failed, the folder's pictures."""

    def __init__(self) -> None:
        self.have: set[str] = set()
        self.crash: list[str] = []
        self.started: list[tuple[list[str], dict]] = []
        self.stops: list[str] = []
        self.on_finish = None
        self.run = False
        self.S = Obj(done=[], failed=[])
        self.recovered = 0
        self.forgiven = 0
        self.wait = False  # booths not drawn yet (the game building shaders)

    def running(self) -> bool:
        return self.run

    def waiting(self) -> bool:
        return self.run and self.wait

    def recover(self, game_dir: Path) -> None:
        self.recovered += 1

    def baked(self, game_dir: Path) -> list[str]:
        return sorted(self.have)

    def crashed(self, game_dir: Path) -> list[str]:
        return list(self.crash)

    def forgive(self, game_dir: Path) -> None:
        self.forgiven += 1
        self.crash = []

    def start(self, game_dir: Path, ids: list[str], *, recipes: dict, on_finish) -> str:  # noqa: ANN001
        self.started.append((ids, recipes))
        self.on_finish = on_finish
        self.S.done, self.S.failed = [], []
        self.run = True
        return f"Taking pictures of {len(ids)} boss(es)."

    def finish(self, done: list[str], reason: str = "done", failed: list[str] | None = None) -> None:
        self.S.done, self.S.failed = done, failed or []
        self.run = False
        self.on_finish(f"Boss pictures {reason}: {len(done)} taken")

    def stop(self, reason: str) -> None:
        self.stops.append(reason)
        self.finish([], reason)


class Thumbs:
    """thumbs.py: the item bake."""

    def __init__(self) -> None:
        self.have: set[str] = set(ITEMS)  # every item has a picture unless a test says otherwise
        self.started: list[list[str]] = []
        self.stops: list[str] = []
        self.on_finish = None
        self.run = False
        self.S = Obj(done=[], failed={})

    def running(self) -> bool:
        return self.run

    def baked(self, game_dir: Path) -> list[str]:
        return sorted(self.have)

    def start(self, game_dir: Path, keys: list[str], on_finish=None) -> str:  # noqa: ANN001
        self.started.append(list(keys))
        self.on_finish = on_finish
        self.S.done, self.S.failed = [], {}
        self.run = True
        return f"Taking {len(keys)} item pictures."

    def finish(self, done: list[str], reason: str = "done", failed: dict[str, str] | None = None) -> None:
        self.S.done, self.S.failed = done, failed or {}
        self.run = False
        self.on_finish(f"Item pictures {reason}: {len(done)} taken")

    def stop(self, reason: str) -> None:
        self.stops.append(reason)
        self.finish([], reason)


TH = Thumbs()


class Booth:
    def __init__(self) -> None:
        self.why = ""
        self.asked: list[list[str]] = []
        self.without: set[str] = set()  # bosses with no recipe

    def read_recipes(self, ids: list[str]) -> tuple[dict, str]:
        self.asked.append(list(ids))
        if self.why:
            return {}, self.why
        return {i: {"id": i, "v": 1} for i in ids if i not in self.without}, ""


class Notice:
    hide_at = 0.0
    shown: list[tuple[str, list[str] | None]] = []  # noqa: RUF012
    how: list[dict] = []  # noqa: RUF012 - each show's hold / progress
    hidden = 0

    @classmethod
    def show(cls, pc: object, title: str, details: list[str] | None = None, **how: object) -> None:
        cls.shown.append((title, details))
        cls.how.append(how)

    @classmethod
    def hide(cls) -> None:
        cls.hidden += 1

    @classmethod
    def tick(cls) -> None:
        pass

    @classmethod
    def remove(cls) -> None:
        pass


def stub(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(f"{PKG}.{name}")
    module.__dict__.update(attrs)
    sys.modules[module.__name__] = module
    setattr(pkg, name, module)
    return module


# endregion


def fresh() -> tuple[types.ModuleType, Bake, Booth]:
    global TH
    bake, booth, TH = Bake(), Booth(), Thumbs()
    stub("bake")  # titlebake's `from . import bake` needs one; the instance replaces it below
    stub("thumbs")
    stub("booth", read_recipes=booth.read_recipes)
    stub("data", dex=lambda: {"bosses": [{"id": i} for i in BOSSES], "items": {k: {} for k in ITEMS}})
    stub("serialgen", has=lambda key: key != "z")
    stub("screen", SMALL_THREAD="weapondex-small")
    Notice.shown, Notice.how, Notice.hidden = [], [], 0
    stub("notice", Notice=Notice)
    spec = importlib.util.spec_from_file_location(f"{PKG}.titlebake", MOD / "titlebake.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.time = Clock
    module.bake = bake
    module.thumbs = TH
    pc.Pawn, pc.bShowMouseCursor = None, True
    engine.GameViewport.World = world
    logs.clear()
    return module, bake, booth


def run(tb: types.ModuleType, seconds: float, enabled: bool = True, step: float = 0.25) -> None:
    end = Clock.now + seconds
    while Clock.now < end:
        Clock.now += step
        tb.tick(GAME, enabled)
        for t in threading.enumerate():
            if t.name in (tb.THREAD, tb.COUNT_THREAD):
                t.join()


def counted(tb: types.ModuleType) -> None:
    for t in threading.enumerate():
        if t.name == tb.COUNT_THREAD:
            t.join()


results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# region Cases


def test_starts_after_hold() -> None:
    tb, bake, booth = fresh()
    bake.have = {"a"}
    run(tb, 2.5)
    check("waits out the hold", not bake.started and tb.S.phase == "idle", tb.S.phase)
    run(tb, 1.5)
    check("starts after 3 s at the title", len(bake.started) == 1, str(bake.started))
    ids, recipes = bake.started[0]
    check("only bosses without a picture", ids == ["b", "c", "d"] and set(recipes) == {"b", "c", "d"}, str(ids))
    check("recipes asked only for those", booth.asked == [["b", "c", "d"]], str(booth.asked))
    check("recover ran first", bake.recovered == 1)
    check("progress notice at the start", Notice.shown and Notice.shown[0][0] == "Kairosdex: making pictures"
          and Notice.shown[0][1][0] == "Bosses 0 of 3" and Notice.how[0].get("hold") and Notice.how[0].get("progress") == 0.0,
          f"{Notice.shown} {Notice.how}")
    bake.finish(["b", "c", "d"])
    check("done after a full run", tb.S.phase == "done", tb.S.phase)
    check("end notice", Notice.shown[-1][0] == "Kairosdex: 3 boss pictures made", str(Notice.shown[-1]))
    run(tb, 10)
    check("once a session", len(bake.started) == 1)


def test_not_in_play() -> None:
    tb, bake, _ = fresh()
    pc.Pawn = Obj()
    run(tb, 10)
    check("never with a character", not bake.started)
    pc.Pawn, pc.bShowMouseCursor = None, False  # dead / loading: no cursor
    run(tb, 10)
    check("never without the cursor", not bake.started)
    pc.bShowMouseCursor = True
    run(tb, 2)
    pc.Pawn = Obj()  # loaded in before the hold ran out
    run(tb, 0.25)
    pc.Pawn = None
    run(tb, 2)
    check("the hold starts over", not bake.started, tb.S.phase)
    run(tb, 2)
    check("then starts", len(bake.started) == 1)


def test_world_change_restarts_hold() -> None:
    tb, bake, _ = fresh()
    run(tb, 2.5)
    engine.GameViewport.World = Obj(name="other")
    run(tb, 2.5)
    check("a new world restarts the hold", not bake.started)
    run(tb, 1.5)  # the hold, then the read, then the start: a tick each
    check("then starts", len(bake.started) == 1)


def test_nothing_missing() -> None:
    tb, bake, booth = fresh()
    bake.have = {"a", "b"}
    bake.crash = ["c", "d"]
    run(tb, 5)
    check("nothing to do: done, no bake, no read", tb.S.phase == "done" and not bake.started and not booth.asked,
          f"{tb.S.phase} {bake.started} {booth.asked}")
    check("no notice", not Notice.shown, str(Notice.shown))


def test_other_build() -> None:
    tb, bake, booth = fresh()
    booth.why = "the booth recipes are for game build x, this is y: they need recording again"
    run(tb, 5)
    check("another build: done, no bake", tb.S.phase == "done" and not bake.started)
    check("another build: logged", any("game build x" in line for line in logs), str(logs))
    check("another build: no notice", not Notice.shown)


def test_no_recipe_for_some() -> None:
    tb, bake, booth = fresh()
    booth.without = {"b"}
    run(tb, 5)
    check("bosses without a recipe left out", bake.started and bake.started[0][0] == ["a", "c", "d"], str(bake.started))


def test_waits_for_download_thread() -> None:
    tb, bake, _ = fresh()
    gate = threading.Event()
    thread = threading.Thread(target=gate.wait, name="weapondex-small", daemon=True)
    thread.start()
    run(tb, 5)
    check("waits for the picture download thread", not bake.started)
    gate.set()
    thread.join()
    run(tb, 1)
    check("then starts", len(bake.started) == 1)


def test_waits_for_dev_bake() -> None:
    tb, bake, _ = fresh()
    bake.run = True
    run(tb, 5)
    check("waits for a dev bake", not bake.started)
    bake.run = False
    run(tb, 1)
    check("then starts", len(bake.started) == 1)


def test_cut_short() -> None:
    tb, bake, _ = fresh()
    run(tb, 5)
    shown = len(Notice.shown)
    bake.finish(["a"], "stopped (the map changed)")
    check("cut short: idle again", tb.S.phase == "idle", tb.S.phase)
    check("cut short: no notice, the progress hidden", len(Notice.shown) == shown and Notice.hidden == 1, str(Notice.shown))
    bake.have = {"a"}
    pc.Pawn = Obj()
    run(tb, 10)
    check("not in play", len(bake.started) == 1)
    pc.Pawn = None
    run(tb, 5)
    check("the rest at the next title screen", len(bake.started) == 2 and bake.started[1][0] == ["b", "c", "d"],
          str(bake.started))


def test_failures_count_as_done() -> None:
    tb, bake, _ = fresh()
    run(tb, 5)
    bake.finish(["a", "b", "c"], failed=["D"])
    check("some failed: done", tb.S.phase == "done")
    check("failed in the notice", Notice.shown[-1][1] == ["1 couldn't be made"], str(Notice.shown[-1]))


def test_option_off() -> None:
    tb, bake, _ = fresh()
    run(tb, 5, enabled=False)
    check("off: never starts", not bake.started)
    run(tb, 5)
    check("on: starts", len(bake.started) == 1)
    run(tb, 0.25, enabled=False)
    check("off mid-run: our bake stops", bake.stops == ["stopped (option off)"] and tb.S.phase == "idle",
          f"{bake.stops} {tb.S.phase}")


def test_dev_bake_not_stopped() -> None:
    tb, bake, _ = fresh()
    bake.run = True  # a dev bake (dex bake all), not ours
    run(tb, 1, enabled=False)
    check("off: a dev bake isn't stopped", not bake.stops)


def test_left_while_reading() -> None:
    tb, bake, booth = fresh()
    run(tb, 3.25)  # the read starts on this tick, the thread is joined
    check("reading", tb.S.phase in ("reading", "ready"), tb.S.phase)
    pc.Pawn = Obj()
    run(tb, 1)
    check("left meanwhile: no bake", not bake.started and tb.S.phase == "idle", tb.S.phase)


def test_both_at_once() -> None:
    tb, bake, _ = fresh()
    bake.have = {"a"}
    TH.have = set()
    run(tb, 5)
    check("both counted in the progress notice", Notice.shown[0][1][0] == "Bosses 0 of 3, items 0 of 2", str(Notice.shown))
    check("bosses and items start together (no roll rules: left out)",
          len(bake.started) == 1 and TH.started == [["x", "y"]], f"{bake.started} {TH.started}")
    bake.finish(["b", "c", "d"])
    check("one done: still running, still the progress notice", tb.S.phase == "running"
          and Notice.shown[-1][0] == "Kairosdex: making pictures", f"{tb.S.phase} {Notice.shown[-1]}")
    TH.finish(["x", "y"])
    check("done after both", tb.S.phase == "done", tb.S.phase)
    check("one end notice for both", Notice.shown[-1][0] == "Kairosdex: 3 boss and 2 item pictures made", str(Notice.shown[-1]))
    run(tb, 10)
    check("once a session (both)", len(bake.started) == 1 and len(TH.started) == 1)


def test_items_first_then_bosses_finish() -> None:
    tb, bake, _ = fresh()
    TH.have = set()
    run(tb, 5)
    TH.finish(["x", "y"])
    check("items done first: still running", tb.S.phase == "running", tb.S.phase)
    bake.finish(["a", "b", "c", "d"])
    check("then done", tb.S.phase == "done" and Notice.shown[-1][0] == "Kairosdex: 4 boss and 2 item pictures made",
          f"{tb.S.phase} {Notice.shown[-1]}")
def test_items_only() -> None:
    tb, bake, _ = fresh()
    bake.have = set(BOSSES)
    TH.have = {"x"}
    run(tb, 5)
    check("items only: no boss bake", not bake.started)
    check("items only: started", TH.started == [["y"]], str(TH.started))
    check("items only: notice", Notice.shown[0][1][0] == "Items 0 of 1", str(Notice.shown))
    TH.finish(["y"])
    check("items only: end notice", Notice.shown[-1][0] == "Kairosdex: 1 item picture made", str(Notice.shown[-1]))


def test_items_cut_short() -> None:
    tb, bake, _ = fresh()
    bake.have = set(BOSSES)
    TH.have = set()
    run(tb, 5)
    shown = len(Notice.shown)
    TH.finish(["x"], "stopped (the map changed)")
    check("items cut short: idle", tb.S.phase == "idle", tb.S.phase)
    check("items cut short: no notice", len(Notice.shown) == shown)
    TH.have = {"x"}
    pc.Pawn = Obj()
    run(tb, 5)
    pc.Pawn = None
    run(tb, 5)
    check("the rest of the items next time", TH.started == [["x", "y"], ["y"]], str(TH.started))


def test_continue_stops_both() -> None:
    tb, bake, _ = fresh()
    TH.have = set()
    run(tb, 5)
    shown = len(Notice.shown)
    bake.finish(["a"], "stopped (the map changed)")
    TH.finish(["x"], "stopped (the map changed)")
    check("Continue: idle again, no notice", tb.S.phase == "idle" and len(Notice.shown) == shown, f"{tb.S.phase} {Notice.shown}")
    bake.have, TH.have = {"a"}, {"x"}
    run(tb, 5)
    check("the rest of both next time", bake.started[-1][0] == ["b", "c", "d"] and TH.started[-1] == ["y"],
          f"{bake.started} {TH.started}")
def test_one_cut_short() -> None:
    tb, bake, _ = fresh()
    TH.have = set()
    run(tb, 5)
    TH.finish(["x", "y"])  # the items finished before Continue
    bake.finish(["a", "b"], "stopped (the map changed)")
    check("bosses cut short, items whole: idle", tb.S.phase == "idle", tb.S.phase)
    bake.have, TH.have = {"a", "b"}, {"x", "y"}
    run(tb, 5)
    check("only the bosses next time", bake.started[-1][0] == ["c", "d"] and len(TH.started) == 1,
          f"{bake.started} {TH.started}")
def test_option_off_items() -> None:
    tb, bake, _ = fresh()
    bake.have = set(BOSSES)
    TH.have = set()
    run(tb, 5)
    run(tb, 0.25, enabled=False)
    check("off during items: the item bake stops", TH.stops == ["stopped (option off)"] and tb.S.phase == "idle",
          f"{TH.stops} {tb.S.phase}")


def test_option_off_both() -> None:
    tb, bake, _ = fresh()
    TH.have = set()
    run(tb, 5)
    run(tb, 0.25, enabled=False)
    check("off: both bakes stop", bake.stops == ["stopped (option off)"] and TH.stops == ["stopped (option off)"]
          and tb.S.phase == "idle", f"{bake.stops} {TH.stops} {tb.S.phase}")


def test_waits_for_dev_item_bake() -> None:
    tb, bake, _ = fresh()
    TH.run = True  # dex bake items
    run(tb, 5)
    check("waits for a dev item bake", not bake.started and not TH.started)


def test_counts_from_the_read() -> None:
    tb, bake, _ = fresh()
    bake.have = {"a", "zz"}  # zz: not a dex boss
    bake.crash = ["c"]
    TH.have = {"x", "other"}
    check("not counted before", tb.S.counts is None)
    run(tb, 5)
    check("counts: dex bosses and items only, crashed counted apart",
          tb.S.counts == {"bosses": (1, 4), "items": (1, 3), "skipped": 1}, str(tb.S.counts))


def test_recount() -> None:
    tb, bake, _ = fresh()
    bake.have, TH.have = {"a", "b"}, {"x", "y"}
    version = tb.S.counts_version
    tb.recount(GAME)
    counted(tb)
    check("recount on a worker", tb.S.counts == {"bosses": (2, 4), "items": (2, 3), "skipped": 0}
          and tb.S.counts_version == version + 1, str(tb.S.counts))
    tb.recount(GAME)
    counted(tb)
    check("same counts: version unchanged", tb.S.counts_version == version + 1)


def test_recount_after_run() -> None:
    tb, bake, _ = fresh()
    run(tb, 5)
    bake.have = set(BOSSES)
    bake.finish(list(BOSSES))
    check("done", tb.S.phase == "done")
    before = tb.S.counts
    run(tb, 2)
    check("not recounted at once (pictures still being composed)", tb.S.counts == before, str(tb.S.counts))
    run(tb, 4)
    check("recounted a few seconds after", tb.S.counts and tb.S.counts["bosses"] == (4, 4), str(tb.S.counts))


def test_rerun_after_done() -> None:
    tb, bake, _ = fresh()
    bake.have = set(BOSSES)
    run(tb, 5)
    check("nothing to do: done", tb.S.phase == "done" and not bake.started)
    bake.have = {"a"}  # pictures removed meanwhile
    run(tb, 5)
    check("once a session without the button", not bake.started)
    msg = tb.rerun()
    check("rerun at the title: soon", "few seconds" in msg, msg)
    check("status at the title: starting", tb.status() == "starting", tb.status())
    run(tb, 5)
    check("rerun: the missing ones made", bake.started and bake.started[-1][0] == ["b", "c", "d"], str(bake.started))
    check("status: making", tb.status() == "making them", tb.status())
    check("rerun while running: refused", "already" in tb.rerun())
    bake.finish(["b", "c", "d"])
    check("rerun done: status clear, not forced", tb.status() == "" and not tb.S.forced)


def test_rerun_option_off() -> None:
    tb, bake, _ = fresh()
    run(tb, 5, enabled=False)
    check("off: nothing", not bake.started)
    tb.rerun()
    run(tb, 5, enabled=False)
    check("rerun runs with the option off", len(bake.started) == 1, str(bake.started))
    run(tb, 1, enabled=False)
    check("and isn't stopped by it", not bake.stops and tb.S.phase == "running", f"{bake.stops} {tb.S.phase}")
    bake.finish(list(BOSSES))
    run(tb, 10, enabled=False)
    check("then off again", len(bake.started) == 1 and tb.S.phase == "idle", tb.S.phase)


def test_rerun_retries_crashed() -> None:
    tb, bake, _ = fresh()
    bake.have = {"a"}
    bake.crash = ["b"]
    run(tb, 5)
    check("crashed skipped", bake.started[0][0] == ["c", "d"], str(bake.started))
    bake.have = {"a", "c", "d"}
    bake.finish(["c", "d"])
    tb.rerun()
    run(tb, 5)
    check("rerun: crashed forgiven and tried", bake.forgiven == 1 and bake.started[-1][0] == ["b"], str(bake.started))


def test_rerun_in_game() -> None:
    tb, bake, _ = fresh()
    bake.have = set(BOSSES)
    run(tb, 5)
    pc.Pawn = Obj()
    bake.have = {"a"}
    msg = tb.rerun()
    check("rerun in game: at the next title screen", "next time" in msg, msg)
    check("status in game: next title screen", tb.status() == "next title screen", tb.status())
    run(tb, 10)
    check("not in game", len(bake.started) == 0)
    pc.Pawn = None
    run(tb, 5)
    check("at the title screen", len(bake.started) == 1 and bake.started[0][0] == ["b", "c", "d"], str(bake.started))


def test_rerun_cut_short_keeps_going() -> None:
    tb, bake, _ = fresh()
    tb.rerun()
    run(tb, 5, enabled=False)
    bake.finish(["a"], "stopped (the map changed)")
    check("cut short: still forced", tb.S.forced and tb.S.phase == "idle", f"{tb.S.forced} {tb.S.phase}")
    bake.have = {"a"}
    run(tb, 5, enabled=False)
    check("the rest at the next title screen, option off", len(bake.started) == 2 and bake.started[1][0] == ["b", "c", "d"],
          str(bake.started))


def test_rerun_nothing_missing() -> None:
    tb, bake, _ = fresh()
    bake.have = set(BOSSES)
    run(tb, 5)
    tb.rerun()
    run(tb, 5)
    check("rerun, nothing missing: a notice says so", Notice.shown and Notice.shown[-1][0] == "Kairosdex: every picture is there",
          str(Notice.shown))
    check("not forced afterwards", not tb.S.forced and tb.S.phase == "done")


def test_rerun_some_cant_be_made() -> None:
    tb, bake, booth = fresh()
    bake.have = {"a", "b", "c"}
    booth.why = "the booth recipes are for game build x, this is y: they need recording again"
    tb.rerun()
    run(tb, 5)
    check("rerun, recipes unusable: says how many and why",
          Notice.shown and Notice.shown[-1][0] == "Kairosdex: 1 picture can't be made" and "game build x" in Notice.shown[-1][1][0],
          str(Notice.shown))


def test_progress() -> None:
    tb, bake, _ = fresh()
    TH.have = set()
    run(tb, 5)
    first = len(Notice.shown)
    bake.S.done = ["a", "b"]
    TH.S.done = ["x"]
    run(tb, 0.75)
    check("progress updated in place", len(Notice.shown) > first and Notice.shown[-1][1][0] == "Bosses 2 of 4, items 1 of 2",
          str(Notice.shown[-1]))
    check("held, with a bar part way", Notice.how[-1].get("hold") and 0 < Notice.how[-1]["progress"] < 1, str(Notice.how[-1]))
    check("a time left", Notice.shown[-1][1][1].startswith("about ") or Notice.shown[-1][1][1] == "almost done", str(Notice.shown[-1]))
    TH.finish(["x", "y"])
    run(tb, 0.75)
    check("items over: counted whole", Notice.shown[-1][1][0] == "Bosses 2 of 4, items 2 of 2", str(Notice.shown[-1]))
    bars = [h["progress"] for h in Notice.how if "progress" in h]
    check("the bar never goes back", all(b2 >= b1 - 1e-9 for b1, b2 in zip(bars, bars[1:])), str(bars))
    shows = len(Notice.shown)
    run(tb, 0.25)
    check("not every tick", len(Notice.shown) <= shows + 1)
    bake.finish(["a", "b", "c", "d"])
    check("then the end notice (not held)", Notice.shown[-1][0] == "Kairosdex: 4 boss and 2 item pictures made"
          and not Notice.how[-1].get("hold"), f"{Notice.shown[-1]} {Notice.how[-1]}")
    run(tb, 2)
    check("no progress after the end", Notice.shown[-1][0] != "Kairosdex: making pictures")


def test_progress_measured_pace() -> None:
    tb, bake, _ = fresh()
    run(tb, 5)
    elapsed = Clock.now - tb.S.started_at
    Clock.now += 42.0 - elapsed  # 42 s for 3 bosses: 14 s each, far slower than BOSS_S
    bake.S.done = ["a", "b", "c"]
    run(tb, 0.5)
    check("time left from the pace so far (1 boss at ~14 s)", Notice.shown[-1][1][1] == "about 15 seconds left", str(Notice.shown[-1]))


def test_progress_waiting_for_shaders() -> None:
    tb, bake, _ = fresh()
    run(tb, 5)
    bake.wait = True
    run(tb, 0.75)
    check("says it waits for the game's shaders", Notice.shown[-1][1][1] == "waiting for the game to build its shaders", str(Notice.shown[-1]))
    bake.wait = False
    run(tb, 0.75)
    check("then the time left again", Notice.shown[-1][1][1] != "waiting for the game to build its shaders", str(Notice.shown[-1]))


def test_forget_clears_rerun() -> None:
    tb, _, _ = fresh()
    tb.rerun()
    tb.forget()
    check("disabled: no rerun pending", not tb.S.forced and tb.status() == "")


# endregion

if __name__ == "__main__":
    started = time.perf_counter()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as ex:  # noqa: BLE001
                results.append((name, False, repr(ex)))
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if not ok and detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed ({(time.perf_counter() - started) * 1000:.0f} ms)")
    sys.exit(1 if failed else 0)
