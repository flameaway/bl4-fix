"""
Checks Kairosdex's picture safeguards offline (mods/bl4_weapondex): which files count as a picture
(picture.whole_pngs: a damaged file counts as missing) and what a run that stopped on a boss leaves
(bake.recover: a booth gets a second chance, a spawned boss doesn't; bake.forgive).
Stand-ins for the game (mods_base, unrealsdk) and for bake's sibling modules; temp folders.

    python dev/kairosdex_pictures_test.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "bl4_weapondex"
PKG = "kdx_pictures_test"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: object, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# region Stand-ins

logs: list[str] = []


class WeakPointer:
    def __init__(self, obj: object = None) -> None:
        self.obj = obj

    def __call__(self) -> object:
        return self.obj


mods_base = types.ModuleType("mods_base")
mods_base.ENGINE = None
mods_base.get_pc = lambda possibly_loading=False: None
unrealsdk = types.ModuleType("unrealsdk")
unrealsdk.logging = types.SimpleNamespace(info=logs.append, error=logs.append, warning=logs.append, dev_warning=logs.append)
unreal = types.ModuleType("unrealsdk.unreal")
unreal.WeakPointer = WeakPointer
unreal.FGameDataHandle = unreal.UObject = unreal.WrappedStruct = object
unrealsdk.unreal = unreal
sys.modules.update({"mods_base": mods_base, "unrealsdk": unrealsdk, "unrealsdk.unreal": unreal})

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(MOD)]
sys.modules[PKG] = pkg


def stub(name: str, **attrs: object) -> None:
    module = types.ModuleType(f"{PKG}.{name}")
    module.__dict__.update(attrs)
    sys.modules[module.__name__] = module
    setattr(pkg, name, module)


stub("booth")
stub("readback")
stub("data", boss_name=lambda boss_id: boss_id.title())


def load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", MOD / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    setattr(pkg, name, module)
    spec.loader.exec_module(module)
    return module


picture = load("picture")
bake = load("bake")

# endregion


def png(path: Path, width: int = 4, height: int = 4) -> bytes:
    picture.write_png(path, width, height, bytes(width * height * 4))
    return path.read_bytes()


def test_whole_pngs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        whole = png(folder / "good.png")
        (folder / "empty.png").write_bytes(b"")
        (folder / "cut.png").write_bytes(whole[: len(whole) - 5])  # an unpack that stopped part way
        (folder / "notpng.png").write_bytes(b"GIF89a" + bytes(40))
        (folder / "note.txt").write_bytes(whole)
        (folder / "dir.png").mkdir()
        check("whole: only the whole PNG counts", picture.whole_pngs(folder) == ["good"], str(picture.whole_pngs(folder)))
        check("whole: a missing folder is no pictures", picture.whole_pngs(folder / "nope") == [])
        # Fixed later (made again): counts from then on
        (folder / "cut.png").write_bytes(whole)
        check("whole: a repaired file counts", picture.whole_pngs(folder) == ["cut", "good"], str(picture.whole_pngs(folder)))
        # Damaged after it was found whole: its size changes, so it's opened again
        (folder / "good.png").write_bytes(whole[:20])
        check("whole: damaged later counts as missing", picture.whole_pngs(folder) == ["cut"], str(picture.whole_pngs(folder)))
        # The same size and date as a file found whole: not opened again (the speed-up)
        st = (folder / "cut.png").stat()
        (folder / "cut.png").write_bytes(bytes(len(whole)))
        os.utime(folder / "cut.png", ns=(st.st_atime_ns, st.st_mtime_ns))
        check("whole: a known file isn't opened again", picture.whole_pngs(folder) == ["cut"], str(picture.whole_pngs(folder)))


def test_whole_pngs_real() -> None:
    # This PC's game folder: the pictures its title-screen runs made (nothing ships since 2026-11-03)
    game_dir = Path(os.environ.get("BL4_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4"))
    folder = game_dir / "OakGame/Content/uiresources/bl4weapondex/items"
    if not folder.is_dir():
        return
    names = picture.whole_pngs(folder)
    every = sorted(p.stem for p in folder.glob("*.png"))
    check("whole: every item picture in the game folder counts", names == every, f"{len(names)} of {len(every)}")


def game() -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    game_dir = Path(tmp.name)
    folder = bake.pictures_dir(game_dir)
    folder.mkdir(parents=True)
    return game_dir, folder, tmp


def attempt(folder: Path, text: str) -> None:
    (folder / ".attempt").write_text(text, encoding="utf-8")


def test_recover_booth_twice() -> None:
    game_dir, folder, tmp = game()
    with tmp:
        check("recover: nothing left behind", bake.recover(game_dir) is None)
        attempt(folder, "arjay booth")
        check("recover: names the boss", bake.recover(game_dir) == "arjay")
        check("booth stopped once: not skipped", bake.crashed(game_dir) == [], str(bake.crashed(game_dir)))
        check("booth stopped once: a suspect", bake.suspects(game_dir) == ["arjay"], str(bake.suspects(game_dir)))
        check("the marker is gone", not (folder / ".attempt").exists())
        check("logged: tried once more", any("tried once more" in line for line in logs), str(logs[-1:]))
        attempt(folder, "arjay booth")
        bake.recover(game_dir)
        check("booth stopped twice: skipped", bake.crashed(game_dir) == ["arjay"], str(bake.crashed(game_dir)))
        check("and no longer a suspect", bake.suspects(game_dir) == [], str(bake.suspects(game_dir)))


def test_recover_suspect_made() -> None:
    game_dir, folder, tmp = game()
    with tmp:
        attempt(folder, "arjay booth")
        bake.recover(game_dir)
        png(folder / "arjay.png")  # made at the next run
        attempt(folder, "donk booth")
        bake.recover(game_dir)
        check("a suspect with a picture since is cleared", bake.suspects(game_dir) == ["donk"], str(bake.suspects(game_dir)))
        attempt(folder, "arjay booth")
        bake.recover(game_dir)
        check("so a later stop on it is a first again", bake.crashed(game_dir) == [], str(bake.crashed(game_dir)))


def test_recover_spawn_and_old_marker() -> None:
    game_dir, folder, tmp = game()
    with tmp:
        attempt(folder, "arjay spawn")
        bake.recover(game_dir)
        check("a spawned boss: skipped at once", bake.crashed(game_dir) == ["arjay"], str(bake.crashed(game_dir)))
        attempt(folder, "donk")  # a marker from before booth mode: just the id
        bake.recover(game_dir)
        check("an old marker: skipped at once", bake.crashed(game_dir) == ["arjay", "donk"], str(bake.crashed(game_dir)))


def test_forgive() -> None:
    game_dir, folder, tmp = game()
    with tmp:
        (folder / "crashed.json").write_text(json.dumps(["arjay"]), encoding="utf-8")
        (folder / bake.SUSPECTS).write_text(json.dumps(["donk"]), encoding="utf-8")
        bake.forgive(game_dir)
        check("forgive: nothing skipped or suspected", bake.crashed(game_dir) == [] and bake.suspects(game_dir) == [])
        bake.forgive(game_dir)
        check("forgive twice: fine", True)


def test_mark() -> None:
    game_dir, folder, tmp = game()
    with tmp:
        bake.S.out_dir = folder
        bake.S.booths = True
        bake._mark("arjay")  # noqa: SLF001
        check("mark: id and mode", (folder / ".attempt").read_text(encoding="utf-8") == "arjay booth")
        bake.S.booths = False
        bake._mark("arjay")  # noqa: SLF001
        check("mark: spawn mode", (folder / ".attempt").read_text(encoding="utf-8") == "arjay spawn")
        bake._mark(None)  # noqa: SLF001
        check("mark cleared", not (folder / ".attempt").exists())
        bake.S.out_dir = None


def test_baked_counts_whole_only() -> None:
    game_dir, folder, tmp = game()
    with tmp:
        png(folder / "arjay.png")
        (folder / "donk.png").write_bytes(b"\x89PNG")
        check("baked: a damaged boss picture counts as missing", bake.baked(game_dir) == ["arjay"], str(bake.baked(game_dir)))


# region Drawn yet (bake._drawn / the settle stage: the game still building shaders)


class _Clock:
    now = 1000.0


def _booths(n: int = 1) -> list:
    """bake.S set up for a booth run of n jobs, the game thread's helpers replaced (no game)."""
    world = object()
    pc = types.SimpleNamespace(Pawn=None, bShowMouseCursor=True)
    bake.time = types.SimpleNamespace(monotonic=lambda: _Clock.now)
    bake.get_pc = lambda *a, **k: pc
    bake._world = lambda: world
    bake._backlogged = lambda: False
    bake._destroy = lambda job: None
    bake._mark = lambda boss_id: None
    bake._build_ahead = lambda job: None
    jobs = [bake.Job({"id": f"b{i}", "name": f"Boss {i}"}, "c", stage="settle", built=types.SimpleNamespace(reposed=True), recipe={}) for i in range(n)]
    for job in jobs:
        job.actor = WeakPointer(object())
        job.found_at = _Clock.now
    bake.S.booths, bake.S.world = True, WeakPointer(world)
    bake.S.job, bake.S.ahead, bake.S.queue = jobs[0], jobs[1:], []
    bake.S.done, bake.S.failed, bake.S.on_finish, bake.S.record = [], [], None, False
    bake.S.capture = bake.S.probe_capture = bake.S.look_capture = bake.S.light = WeakPointer()
    bake.S.watch, bake.S.unsettled_at, bake.S.watch_next = False, 0.0, 0
    bake._unprotect = lambda: None
    return jobs


def _tick_for(seconds: float, step: float = 0.1) -> None:
    end = _Clock.now + seconds
    while _Clock.now < end:
        _Clock.now += step
        bake.tick()


def test_drawn_warm_cache() -> None:
    (job,) = _booths()
    check("drawn at the first probe: shoot (no wait when the shaders are built)", bake._drawn(job, 120, _Clock.now) and not bake.S.watch)


def test_drawn_waits_for_shaders() -> None:
    (job,) = _booths()
    logs.clear()
    t = _Clock.now
    check("nothing drawn: wait", not bake._drawn(job, 0, t) and bake.S.watch)
    check("says why, once", sum("building its shaders" in m for m in logs) == 1, str(logs))
    bake._drawn(job, 0, t + 0.5)
    check("said only once", sum("building its shaders" in m for m in logs) == 1)
    check("drawn, but the game was just filling booths in: wait", not bake._drawn(job, 40, t + 1.0))
    check("filling in (40 -> 120 pixels): wait", not bake._drawn(job, 120, t + 1.5) and bake.S.unsettled_at == t + 1.5)
    check("the same, but not long enough since: wait", not bake._drawn(job, 120, t + 2.0))
    check("the same, nothing filling in for DRAW_CLEAR_S: shoot", bake._drawn(job, 120, t + 1.5 + bake.DRAW_CLEAR_S))
    check("a pixel or two either way is the same", bake._drawn(job, 121, t + 2.0 + bake.DRAW_CLEAR_S))


def test_settle_retries_until_drawn() -> None:
    (job,) = _booths()
    probes: list[int] = [0, 0, 0, 80, 80]
    aims: list[float] = []

    def aim(j: object) -> bool:
        aims.append(_Clock.now)
        count = probes.pop(0) if probes else 80
        return bake._drawn(j, count, _Clock.now)

    bake._aim = aim
    bake._watch = lambda now: None
    _tick_for(bake.BOOTH_SETTLE_S + 3)
    check("probed again every DRAW_RETRY_S, not every frame", len(aims) >= 4 and all(b - a >= bake.DRAW_RETRY_S - 1e-6 for a, b in zip(aims, aims[1:])), str(aims))
    check("not shot while the game fills booths in", job.stage == "settle", job.stage)
    _tick_for(bake.DRAW_CLEAR_S + 1)
    check("then shot", job.stage != "settle", job.stage)


def test_settle_gives_up() -> None:
    (job,) = _booths()
    bake._aim = lambda j: bake._drawn(j, 0, _Clock.now)
    bake._watch = lambda now: None
    _tick_for(bake.BOOTH_SETTLE_S + bake.DRAW_TIMEOUT_S - 5, step=0.25)
    check("still waiting before DRAW_TIMEOUT_S", not bake.S.failed and job.stage == "settle", str(bake.S.failed))
    _tick_for(10, step=0.25)
    check("never drawn: fails after DRAW_TIMEOUT_S (made at the next launch)", bake.S.failed == ["Boss 0"] and not bake.running(), str(bake.S.failed))


def test_watch_probes_the_others() -> None:
    jobs = _booths(3)
    watched: list[float] = []
    bake._aim = lambda j: bake._drawn(j, 0, _Clock.now)
    bake._watch = lambda now: watched.append(now)
    _tick_for(bake.BOOTH_SETTLE_S + 2)
    check("while waiting, the other booths are probed (one a frame)", len(watched) > 5 and jobs[0].stage == "settle", str(len(watched)))
    bake.S.watch = False
    bake._aim = lambda j: False  # not drawn, but nothing says the game is building shaders
    watched.clear()
    _tick_for(0.5)
    check("not when the game isn't building shaders", not watched)


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
