"""
Takes a picture of each boss for the dex, once: `dex bake` (or the dev button). The pictures are
saved as loose files (uiresources/bl4weapondex/bosses/<boss id>.png), so every later session just
shows them. They're made from the player's own install, like dex.json.

Most bosses aren't a model file of their own: the game builds them at spawn time from shared
bodies, unique parts and materials. So each one is spawned for real, with the game's own spawner
(SpawnBlueprintLibrary.GbxSpawnActorAtTransform), either on the ground in front of the player
(the default: best in an open arena) or high above them (`sky`, held in place every frame):
- the frame it appears it's made undamageable and its AI controller is removed, so it just stands;
- meanwhile the player is undamageable and invisible to AI (GbxTargetingFunctionLibrary.
  SetInvisibleToAI), both put back when the bake ends however it ends;
- it's left a moment so its idle animation and textures settle;
- a SceneCapture2D that renders only that actor photographs it front-on, lit by our own light
  beside the camera (where it's spawned, the scene's light often comes from behind, which left
  bosses as dark silhouettes): raw HDR scene colour into a float render target, read straight back
  (ReadRenderTargetRaw, through readback.py) and tone mapped by picture.py into a PNG on an
  interpreter of its own (offload.py; a worker thread when there's none) (scene captures ignore
  the exposure settings). The capture's alpha says which pixels the boss covers: the old
  .hdr export had none, and "black means empty" made every shadowed part see-through;
- then it's destroyed and the next boss starts.

Some bosses crash the game when spawned like this. Before each spawn the boss's id goes in
bosses/.attempt; it's removed once that boss is done (or failed normally). If a bake finds the file
still there, that boss crashed the game last time: it goes in bosses/crashed.json and later bakes
skip it (`dex bake <boss>` still tries it on purpose). A booth (nothing spawned) gets a second
chance first (`recover`): closing the game during a title-screen run leaves the same mark.

The spawner is latent: it returns at once and writes the new actor into its `Result` out param when
the spawn finishes, frames later, into the memory of the original call's params. Called the usual
way, the SDK copies our params into a temporary block and frees it on return, so that late write
landed in freed memory and crashed the game a few bosses later (checked: out params never reach a
WrappedStruct passed to a call). So we call ProcessEvent ourselves with a params block we own
(_process_event) and keep every one for the rest of the session (_KEEP). The boss is then read
from Result. A boss that spawns after we gave up on it is still removed when it turns up (_LATE),
and so is anything that came with it (Saddleback's rider is a character of its own).

Runs from the mod's tick, one step per frame, in gameplay only (spawning stalls while paused).
Everything it spawns is tracked with WeakPointers and destroyed on stop, error or disable.
"""

from __future__ import annotations

import array
import ctypes
import json
import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGameDataHandle, UObject, WeakPointer, WrappedStruct

from . import booth, data, offload, picture, readback

GBX_ACTOR_TYPE = 16384
BOOTH_HEIGHT = 20000.0  # above the player, in sky mode
GROUND_DISTANCE = 2000.0  # in front of the player, in ground mode
PROTECT_REASON = "WeapondexBake"
SPAWN_TIMEOUT_S = 30.0
EXTRAS_RADIUS = 3000.0  # characters that appear this close to the boss came with it
# UObject::ProcessEvent's vtable slot (this build: the address unrealsdk logs as "Detoured ProcessEvent")
PROCESS_EVENT_SLOT = 73
# RF_BeginDestroyed | RF_FinishDestroyed | RF_MirroredGarbage: objects on their way out
DEAD_FLAGS = 0x8000 | 0x10000 | 0x40000000
TEMPLATE_FLAGS = 0x10 | 0x20  # RF_ClassDefaultObject | RF_ArchetypeObject: never "near", never removed
SETTLE_S = 3.0  # idle animation and textures (big bosses stream in slowly)
BOOTH_SETTLE_S = 1.5  # a booth is posed at once: only its textures stream in
BOOTH_REPOSE_S = 0.3  # its pose is set again this long after it was built (booth.Booth.repose)
# Is the booth drawn yet? The game draws a mesh only once its shaders are compiled
# (r.PSOPrecache.ProxyCreationWhenPSOReady 1, docs/game-performance.md): while it's still building
# them (the first launch after a patch or a driver update), a booth shows nothing, or only the
# parts whose shaders were ready. A title-screen run then made 31 of 118 pictures empty ("the
# capture is empty") and ~8 with only weapons and small parts, all in its first ~25 s
# (2026-11-03). The framing probe tells: nothing drawn = not ready, so the shot waits. Once one
# booth was seen not drawn, the run waits for the game: every standing booth is probed again (one a
# frame, each every DRAW_RETRY_S) and a shot waits until no booth has been empty or filling in for
# DRAW_CLEAR_S, and its own probe came out the same twice. With the shaders built no probe is
# empty, so nothing changes then. Booths only: a spawned boss moves, so its probe always differs
DRAW_RETRY_S = 0.5
DRAW_CLEAR_S = 10.0
DRAW_TIMEOUT_S = 300.0  # a booth not drawn this long fails (its picture is tried at the next launch)
# Booths built ahead of the one being photographed: they settle meanwhile. With compose off the GIL
# (offload.py) this caps the bosses: BOOTH_SETTLE_S each, BOOTH_AHEAD at once (118 in ~35 s). 9 or
# 12 took ~27 s for ~70 MB more, but one 12 run lost Kraggula's wings (its picture came out at 15%
# of its size): kept at 6 (2026-11-03). Building the same bosses twice in one session also shifted
# ~18 bosses' framing by up to 11%, whatever the number: compare runs from fresh launches
BOOTH_AHEAD = 6
# Booths standing at once share one spot: each capture shows only its own booth (ShowOnlyActors),
# our light casts no shadows, and the light never moves (see _light): apart, each booth was lit
# from another angle (2026-11-03)
BOOTH_SPACING = 0.0
SIZE = 512
CAPTURE_SOURCE_HDR = 0
CAPTURE_SOURCE_FINAL_LDR = 2
RTF_RGBA8 = 2
RTF_RGBA16F = 6
PRIMITIVE_RENDER_SHOW_ONLY = 2
FOV = 30.0
# Our light's intensity per squared cm of distance to the boss: the same light on it however far
# the camera is, well above the vault's own lighting (at 4 m, 5e7 lit the player ~30x the scene)
LIGHT_PER_CM2 = 300.0
LIGHT_RAISE = 0.25  # how far above the camera, as a share of the camera's distance
# Our light above the camera (True, the user's choice 2026-11-02). Until then it was never moved (an
# early return): it stood at the world's origin, kilometres away, and the pictures were lit by the
# map's sun and sky (they changed with map and time of day). At the camera it's ~50,000x the sky,
# and compose's levelling keeps the look (12 bosses side by side). False = the shipped pictures' way
LIGHT_AT_CAMERA = True
FRAME_MARGIN = 1.08
PROBE_SIZE = 64  # the framing test shot
# The picture is drawn from passes of the frozen boss (picture.compose): after the HDR shot (its
# transparency and light), its paint colours, which way its surfaces face, and their depth. The
# game's own final image has its look (ink outlines are post-processing) but comes out blown out:
# captures ignore exposure settings.
CAPTURE_SOURCE_DEPTH = 4
CAPTURE_SOURCE_NORMAL = 6
CAPTURE_SOURCE_BASE_COLOR = 7
PASSES = (CAPTURE_SOURCE_BASE_COLOR, CAPTURE_SOURCE_NORMAL, CAPTURE_SOURCE_DEPTH)
KEEP_RAW = False  # dev: also save every pass's floats in bosses/raw/
# Pictures waiting for the worker (picture.compose, ~0.8 s each on a thread of the game's
# interpreter): the next shot waits while this many are queued. Each holds its four passes (16 MB);
# with reads at ~25 ms a shot outran the worker 2 to 1 and a 118-boss run queued ~70 (over 1 GB;
# 2026-11-03). On interpreters of their own (offload.py, ~0.22 s each) one more than there are
# workers, so none of them waits for the next shot
COMPOSE_AHEAD = 2
OFFLOAD_AHEAD = offload.WORKERS + 1
SUSPECTS = "suspects.json"  # recover: booths a run stopped on once
SHOW_ATTACHED = False  # dev: a spawned boss's picture also shows what's attached to it (weapons, crystals), like a booth
# The sky still draws into a show-only capture and washes it out; the boss alone is what we want
HIDDEN_SHOW_FLAGS = ("Fog", "Atmosphere", "VolumetricFog", "Cloud", "LocalFogVolumes", "MotionBlur", "Bloom")


def log(msg: str) -> None:
    logging.info(f"[Kairosdex] {msg}")


@dataclass
class Job:
    boss: dict[str, Any]
    char: str
    stage: str = "spawn"
    started: float = 0.0
    nearby: set[str] = field(default_factory=set)  # characters near the booth before the spawn
    actor: WeakPointer = field(default_factory=WeakPointer)
    found_at: float = 0.0
    booth: tuple[float, float, float] = (0.0, 0.0, 0.0)
    shot: Any = None  # (camera location, yaw, pitch, fov, shown actors), from the test shot
    hdr: bytes = b""  # the HDR shot's pixels (its alpha is the picture's transparency)
    passes: list[bytes] = field(default_factory=list)  # each of PASSES' pixels, in order
    args: Any = None  # the spawn call's params: the game writes Result into them later
    recipe: dict[str, Any] | None = None  # booth mode: rebuilt from this recipe, nothing spawned
    built: Any = None  # booth mode: the booth.Booth
    slot: int = 0  # booth mode: which of the BOOTH_AHEAD places in the sky
    probed: int = -1  # booth mode: pixels its last probe found drawn (-1: not probed yet)
    probe_since: float = 0.0  # its first probe
    next_probe: float = 0.0  # its next probe, while it isn't drawn yet (DRAW_RETRY_S)


# Every spawn call's params, kept alive for good (see the module docstring). Survives a hot reload.
_KEEP: list[Any] = globals().get("_KEEP") or []
# Params of spawns we gave up on: whatever turns up in their Result is removed
_LATE: list[Any] = globals().get("_LATE") or []
_PROCESS_EVENT = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class S:
    queue: list[Job] = []  # noqa: RUF012
    job: Job | None = None
    out_dir: Path | None = None
    source = CAPTURE_SOURCE_HDR
    ground = True
    protected: WeakPointer = WeakPointer()  # the player pawn we made safe
    was_damageable = True
    done: list[str] = []  # noqa: RUF012
    failed: list[str] = []  # noqa: RUF012
    capture: WeakPointer = WeakPointer()
    probe_capture: WeakPointer = WeakPointer()
    light: WeakPointer = WeakPointer()
    look_capture: WeakPointer = WeakPointer()
    look: Any = None  # the look shot's render target
    # Pictures to make, made one at a time on one worker (pure Python: each takes seconds)
    pending: queue.Queue | None = None
    worker: threading.Thread | None = None
    target: Any = None  # the render target (kept referenced so it isn't collected)
    probe: Any = None  # the framing test shot's render target
    on_finish: Callable[[str], None] | None = None
    record = False  # dev: also record each spawned boss's booth recipe (booth.record)
    recipes: dict[str, Any] = {}  # noqa: RUF012 - recorded this run, by boss id
    booths = False  # booth mode (start(recipes=...))
    ahead: list[Job] = []  # noqa: RUF012 - booth mode: built and settling, next after S.job
    slot = 0
    watch = False  # a booth was seen not drawn this run: the game is building shaders (DRAW_RETRY_S)
    unsettled_at = 0.0  # the last probe that found a booth empty or filling in
    watch_next = 0  # the next booth in S.ahead to probe again
    world: WeakPointer = WeakPointer()  # the world it started in (the controller stays the same across maps)


def running() -> bool:
    return S.job is not None or bool(S.queue) or bool(S.ahead)


def pictures_dir(game_dir: Path) -> Path:
    return game_dir / "OakGame/Content/uiresources/bl4weapondex/bosses"


def crashed(game_dir: Path) -> list[str]:
    """Bosses that crashed the game while being photographed."""
    try:
        return json.loads((pictures_dir(game_dir) / "crashed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def suspects(game_dir: Path) -> list[str]:
    """Bosses a booth run stopped on once (the game crashed or was closed): tried once more."""
    try:
        found = json.loads((pictures_dir(game_dir) / SUSPECTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in found if isinstance(i, str)] if isinstance(found, list) else []


def recover(game_dir: Path) -> str | None:
    """
    If the last bake stopped in the middle of a boss (bosses/.attempt left behind), notes which.
    A spawned boss (`dex bake`) is skipped from then on (crashed.json). A booth only the second
    time it happens: quitting the game during a title-screen run (Quit, Alt+F4, closing the window)
    leaves the same mark, and a booth is plain meshes, so the first time it's a suspect and is
    tried again. A suspect with a picture since is cleared. Returns the boss's id.
    """
    folder = pictures_dir(game_dir)
    marker = folder / ".attempt"
    try:
        boss_id, _, mode = marker.read_text(encoding="utf-8").strip().partition(" ")
    except OSError:
        return None
    marker.unlink(missing_ok=True)
    if not boss_id:
        return None
    was = set(suspects(game_dir))
    now = was - set(baked(game_dir))
    if mode == "booth" and boss_id not in was:
        now.add(boss_id)
        log(f"The last picture run stopped on {data.boss_name(boss_id)} (the game was closed or crashed): it's tried once more.")
    else:
        now.discard(boss_id)
        bad = sorted(set(crashed(game_dir)) | {boss_id})
        (folder / "crashed.json").write_text(json.dumps(bad), encoding="utf-8")
        log(f"The last picture run crashed the game on {data.boss_name(boss_id)}; it's skipped from now on.")
    if now != was:
        (folder / SUSPECTS).write_text(json.dumps(sorted(now)), encoding="utf-8")
    return boss_id


def forgive(game_dir: Path) -> None:
    """Every boss skipped after a crash, and every suspect, is tried again (the dev re-run)."""
    folder = pictures_dir(game_dir)
    for name in ("crashed.json", SUSPECTS):
        (folder / name).unlink(missing_ok=True)


def _mark(boss_id: str | None) -> None:
    """bosses/.attempt: the boss being made, and how (booth / spawn): `recover` reads it."""
    if S.out_dir is None:
        return
    marker = S.out_dir / ".attempt"
    if boss_id is None:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(f"{boss_id} {'booth' if S.booths else 'spawn'}", encoding="utf-8")  # ids have no spaces


def _step(what: str) -> None:
    """Breadcrumb: the step about to run, so a crash names the call that did it (bosses/.step)."""
    if S.out_dir is not None and S.job is not None:
        with open(S.out_dir / ".step", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {S.job.boss['id']}: {what}\n")


def baked(game_dir: Path) -> list[str]:
    """Boss ids that have a whole picture (picture.whole_pngs: a damaged file counts as missing)."""
    return picture.whole_pngs(pictures_dir(game_dir))


# region Starting and stopping


def start(game_dir: Path, boss_ids: list[str], *, source: int = CAPTURE_SOURCE_HDR, ground: bool = True,
          on_finish: Callable[[str], None] | None = None, record: bool = False,
          recipes: dict[str, Any] | None = None, out_dir: Path | None = None) -> str:  # fmt: skip
    """
    Pictures of these bosses. `recipes` (booth mode): each boss is rebuilt from its booth recipe
    instead of spawned (bosses without one are skipped). `record` (dev, spawn mode): also records
    each boss's recipe into S.recipes and `booth.json` next to the pictures.
    """
    if running():
        return "A bake is already running."
    pc = get_pc()
    # A booth needs no map or character: it runs at the title screen or with a menu open too
    if pc is None or (recipes is None and pc.Pawn is None):
        return "Load into the game first."
    bosses = {b["id"]: b for b in data.dex()["bosses"]}
    S.queue = []
    for boss_id in boss_ids:
        boss = bosses.get(boss_id)
        if boss is None:
            continue
        # The boss's own def: the one without Big Encore / replay / variant suffixes
        char = min(boss["chars"], key=len)
        if recipes is not None:
            recipe = recipes.get(boss_id)
            if recipe is None or recipe.get("v") != booth.RECIPE_VERSION:
                continue
            S.queue.append(Job(boss, char, recipe=recipe))
        else:
            S.queue.append(Job(boss, char))
    S.out_dir = out_dir or pictures_dir(game_dir)
    S.out_dir.mkdir(parents=True, exist_ok=True)
    S.source = source
    S.ground = ground if recipes is None else False  # a booth stands in the sky, where nothing is
    S.done, S.failed = [], []
    S.record = record and recipes is None
    S.recipes = {}
    if recipes is None:
        _protect(pc.Pawn)  # a booth has no AI and deals no damage: nothing to protect from
    S.on_finish = on_finish
    S.booths = recipes is not None
    S.ahead = []
    S.watch, S.unsettled_at, S.watch_next = False, 0.0, 0
    S.world = WeakPointer(_world())  # booth mode: another world = the map changed
    if S.booths:
        return f"Taking pictures of {len(S.queue)} boss(es)."
    return f"Taking pictures of {len(S.queue)} boss(es). Stay in gameplay (no menus) until it says it's done."


def stop(reason: str = "stopped") -> None:
    if S.job is not None:
        _destroy(S.job)
        _mark(None)
    S.job = None
    S.queue = []
    for job in S.ahead:
        _destroy(job)
    S.ahead = []
    for cam in (S.capture(), S.probe_capture(), S.look_capture(), S.light()):
        if cam is not None:
            cam.K2_DestroyActor()
    S.capture = WeakPointer()
    S.probe_capture = WeakPointer()
    S.light = WeakPointer()
    S.look_capture = WeakPointer()
    S.look = None
    S.target = None
    S.probe = None
    _unprotect()
    if S.record and S.recipes and S.out_dir is not None:
        _write_recipes(S.out_dir)
    summary = f"Boss pictures {reason}: {len(S.done)} taken" + (f", {len(S.failed)} failed ({', '.join(S.failed)})" if S.failed else "")
    log(summary)
    if S.on_finish is not None:
        S.on_finish(summary)


def _protect(pawn: UObject) -> None:
    """The player can't be hurt or noticed by AI while bosses stand around."""
    S.protected = WeakPointer(pawn)
    S.was_damageable = bool(pawn.bCanBeDamaged)
    pawn.bCanBeDamaged = False
    try:
        lib = unrealsdk.find_class("GbxTargetingFunctionLibrary").ClassDefaultObject
        lib.SetInvisibleToAI(pawn, PROTECT_REASON, True)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Kairosdex] Couldn't hide you from AI: {ex}")


def _unprotect() -> None:
    pawn = S.protected()
    S.protected = WeakPointer()
    if pawn is None:
        return
    pawn.bCanBeDamaged = S.was_damageable
    try:
        lib = unrealsdk.find_class("GbxTargetingFunctionLibrary").ClassDefaultObject
        lib.SetInvisibleToAI(pawn, PROTECT_REASON, False)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Kairosdex] Couldn't unhide you from AI: {ex}")


# endregion
# region Steps


def _remove(actor: UObject) -> None:
    try:
        controller = actor.Controller
        if controller is not None:
            controller.UnPossess()
            controller.K2_DestroyActor()
    except AttributeError:
        pass
    actor.K2_DestroyActor()


def _destroy(job: Job) -> None:
    """Removes the boss and whatever came with it; if it hasn't spawned yet, removes it when it does."""
    _step("destroy")
    if job.built is not None:
        job.built.destroy()
        job.built = None
        job.actor = WeakPointer()
        return
    actor = job.actor()
    job.actor = WeakPointer()
    if actor is not None:
        _remove(actor)
    elif job.args is not None:
        _LATE.append(job.args)
    for extra in _near(job.booth):
        if extra._path_name() not in job.nearby:  # noqa: SLF001
            log(f"also removed {extra.Name}, which came with {job.boss['name']}")
            _remove(extra)


def _near(where: tuple[float, float, float]) -> list[UObject]:
    """
    Live characters near a point, other than the player and things the player owns. Reads
    properties only: calling a function on every character in the world reaches ones in odd states
    (garbage, half-destroyed), and one such call crashed the game.
    """
    pc = get_pc()
    pawn = pc.Pawn if pc is not None else None
    player_controller = unrealsdk.find_class("PlayerController")
    out = []
    for o in unrealsdk.find_all("OakCharacter", exact=False):
        try:
            if o.ObjectFlags & (DEAD_FLAGS | TEMPLATE_FLAGS) or o.bActorIsBeingDestroyed or o == pawn:
                continue
            controller = o.Controller
            if controller is not None and controller.Class._inherits(player_controller):  # noqa: SLF001
                continue
            if pawn is not None and pawn in (o.Owner, o.Instigator):
                continue
            root = o.RootComponent
            if root is None:
                continue
            loc = root.RelativeLocation  # a character's root has no parent: this is its world location
        except Exception:  # noqa: BLE001 - a half-made or dying actor: not ours to touch
            continue
        if math.dist((loc.X, loc.Y, loc.Z), where) < EXTRAS_RADIUS:
            out.append(o)
    return out


def _attached_to(other: UObject) -> UObject | None:
    """The actor a character rides on (its root's attach parent's owner), from properties only."""
    root = other.RootComponent
    parent = root.AttachParent if root is not None else None
    return parent.Outer if parent is not None else None


def _sweep_late() -> None:
    """Removes bosses that finished spawning after we gave up on them."""
    for args in list(_LATE):
        actor = args.Result
        if actor is not None:
            _LATE.remove(args)
            log(f"removed {actor.Name}, which spawned late")
            _remove(actor)


def _process_event(obj: UObject, func: UObject, args: WrappedStruct) -> None:
    """Calls a UFunction on our own params block (the SDK's call would use a temporary copy)."""
    this = obj._get_address()  # noqa: SLF001
    vtable = ctypes.c_uint64.from_address(this).value
    target = ctypes.c_uint64.from_address(vtable + 8 * PROCESS_EVENT_SLOT).value
    _PROCESS_EVENT(target)(this, func._get_address(), args._get_address())  # noqa: SLF001


def _spawn(job: Job) -> None:
    _mark(job.boss["id"])
    pc = get_pc()
    loc = pc.Pawn.K2_GetActorLocation()
    face = 0.0
    if S.ground:
        # In front of where the player faces, facing them, a little up so it lands on its feet
        yaw = math.radians(pc.Pawn.K2_GetActorRotation().yaw)
        job.booth = (loc.X + math.cos(yaw) * GROUND_DISTANCE, loc.Y + math.sin(yaw) * GROUND_DISTANCE, loc.Z + 400)
        face = yaw + math.pi
    else:
        job.booth = (loc.X, loc.Y, loc.Z + BOOTH_HEIGHT)
    _step("scan before spawn")
    job.nearby = {o._path_name() for o in _near(job.booth)}  # noqa: SLF001
    lib = unrealsdk.find_class("SpawnBlueprintLibrary").ClassDefaultObject
    fn = lib.GbxSpawnActorAtTransform
    args = WrappedStruct(fn.func)
    args.Context = pc
    args.GbxActorDef = FGameDataHandle(GBX_ACTOR_TYPE, job.char)
    t = args.Transform
    t.Translation.X, t.Translation.Y, t.Translation.Z = job.booth
    t.Rotation.Z, t.Rotation.W = math.sin(face / 2), math.cos(face / 2)
    t.Scale3D.X = t.Scale3D.Y = t.Scale3D.Z = 1
    # Critical: the spawn budget doesn't hold it back. The latent callback goes nowhere (Linkage -1).
    args.bIsCritical = True
    args.SpawnDetails.bCritical = True
    args.SpawnDetails.bOverrideCritical = True
    args.LatentInfo.CallbackTarget = pc
    args.LatentInfo.UUID = random.randint(100000, 2_000_000_000)
    args.LatentInfo.Linkage = -1
    args.LatentInfo.ExecutionFunction = "None"
    job.args = args
    _KEEP.append(args)
    _step("spawn call")
    _process_event(lib, fn.func, args)
    job.started = time.monotonic()
    job.stage = "wait"


def _build_booth(job: Job) -> None:
    """
    Booth mode: the boss rebuilt from its recipe (booth.build) high above the player, or above the
    camera where there's no character (the title screen): out of sight either way.
    """
    _mark(job.boss["id"])
    pc = get_pc()
    if pc.Pawn is not None:
        loc = pc.Pawn.K2_GetActorLocation()
    else:
        loc = _ret(pc.PlayerCameraManager.GetCameraLocation())
    job.slot = S.slot
    S.slot = (S.slot + 1) % BOOTH_AHEAD
    job.booth = (loc.X + job.slot * BOOTH_SPACING, loc.Y, loc.Z + BOOTH_HEIGHT)
    _step("booth build")
    built = booth.build(job.recipe or {}, pc, job.booth, 0.0)
    job.built = built
    job.actor = built.actor
    job.found_at = time.monotonic()
    if built.missing:
        log(f"{job.boss['name']}: the booth lacks {', '.join(built.missing[:4])}")
    actor = built.actor()
    if actor is None:
        raise RuntimeError("the booth wasn't built")
    for comp in actor.K2_GetComponentsByClass(unrealsdk.find_class("MeshComponent")):
        try:
            comp.PrestreamTextures(5.0, True, 0)
        except Exception:  # noqa: BLE001
            pass


def _world() -> UObject | None:
    viewport = ENGINE.GameViewport
    return viewport.World if viewport is not None else None


def _ret(value: Any) -> Any:
    """A UFunction's return value (calls with out params return a tuple, the return value first)."""
    return value[0] if isinstance(value, tuple) else value


def _write_recipes(folder: Path) -> None:
    """Dev: this run's recipes merged into booth.json (temp + rename)."""
    path = folder / "booth.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    old.update(S.recipes)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(old, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    log(f"booth recipes: {len(S.recipes)} recorded, {len(old)} in {path}")


def _hold(actor: UObject, booth_at: tuple[float, float, float]) -> None:
    """Keeps a spawned boss harmless: no damage, no AI. In the sky, also pinned where we put it."""
    actor.bCanBeDamaged = False
    try:
        controller = actor.Controller
        if controller is not None:
            controller.UnPossess()
            controller.K2_DestroyActor()
    except AttributeError:
        pass
    if S.ground:
        return  # it stands on the ground by itself
    try:
        actor.CharacterMovement.DisableMovement()
    except AttributeError:
        pass
    here = unrealsdk.make_struct("Vector", X=booth_at[0], Y=booth_at[1], Z=booth_at[2])
    actor.K2_SetActorLocation(here, False, unrealsdk.make_struct("HitResult"), True)


def _find(job: Job) -> bool:
    actor = job.args.Result if job.args is not None else None
    if actor is None:
        return False
    _step("found, holding")
    job.args = None  # found: nothing to remove later
    job.actor = WeakPointer(actor)
    job.found_at = time.monotonic()
    _hold(actor, job.booth)
    for comp in actor.K2_GetComponentsByClass(unrealsdk.find_class("MeshComponent")):
        try:
            comp.PrestreamTextures(5.0, True, 0)
        except Exception:  # noqa: BLE001
            pass
    return True


def _camera(pc: UObject, *, probe: bool = False, look: bool = False) -> tuple[UObject, Any]:
    """
    The picture camera, the framing test shot's or the look shot's. Each has its own render
    target, set once: switching one camera between targets every boss is what crashed the game
    inside CaptureScene (twice, at the 11th and 13th boss; never in a 102-boss run with one fixed
    target).
    """
    cap = (S.probe_capture if probe else S.look_capture if look else S.capture)()
    if cap is not None:
        return cap, cap.CaptureComponent2D
    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    xf = unrealsdk.make_struct("Transform", Rotation=unrealsdk.make_struct("Quat", W=1),
                               Scale3D=unrealsdk.make_struct("Vector", X=1, Y=1, Z=1))  # fmt: skip
    spawned = gs.BeginDeferredActorSpawnFromClass(pc, unrealsdk.find_class("SceneCapture2D"), xf, 1, None, 0)
    cap = spawned[0] if isinstance(spawned, tuple) else spawned
    done = gs.FinishSpawningActor(cap, xf, 0)
    cap = done[0] if isinstance(done, tuple) else done
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    clear = unrealsdk.make_struct("LinearColor", R=0, G=0, B=0, A=0)
    if probe:
        S.probe_capture = WeakPointer(cap)
        S.probe = krl.CreateRenderTarget2D(pc, PROBE_SIZE, PROBE_SIZE, RTF_RGBA16F, clear, False, False)
        target = S.probe
    elif look:
        S.look_capture = WeakPointer(cap)
        S.look = krl.CreateRenderTarget2D(pc, SIZE, SIZE, RTF_RGBA16F, clear, False, False)
        target = S.look
    else:
        S.capture = WeakPointer(cap)
        fmt = RTF_RGBA16F if _hdr() else RTF_RGBA8
        S.target = krl.CreateRenderTarget2D(pc, SIZE, SIZE, fmt, clear, False, False)
        target = S.target
    comp = cap.CaptureComponent2D
    comp.TextureTarget = target
    comp.bCaptureEveryFrame = False
    comp.bCaptureOnMovement = False
    # The framing test shot sees depth: solid parts only (an effect cloud would frame a speck in a cloud)
    comp.CaptureSource = CAPTURE_SOURCE_FINAL_LDR if look else CAPTURE_SOURCE_DEPTH if probe else S.source
    # Through the setter: assigning ShowFlagSettings directly doesn't reach the renderer
    comp.SetShowFlagSettings([unrealsdk.make_struct("EngineShowFlagsSetting", ShowFlagName=n, Enabled=False) for n in HIDDEN_SHOW_FLAGS])
    comp.PrimitiveRenderMode = PRIMITIVE_RENDER_SHOW_ONLY
    comp.FOVAngle = FOV
    return cap, comp


def _light(pc: UObject, cam: tuple[float, float, float], target: tuple[float, float, float]) -> float:
    """Puts our light just above the camera, strong enough for the distance to the boss."""
    light = S.light()
    if light is None:
        gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
        xf = unrealsdk.make_struct("Transform", Rotation=unrealsdk.make_struct("Quat", W=1),
                                   Scale3D=unrealsdk.make_struct("Vector", X=1, Y=1, Z=1))  # fmt: skip
        spawned = gs.BeginDeferredActorSpawnFromClass(pc, unrealsdk.find_class("PointLight"), xf, 1, None, 0)
        light = spawned[0] if isinstance(spawned, tuple) else spawned
        light.LightComponent.SetMobility(2)  # movable
        done = gs.FinishSpawningActor(light, xf, 0)
        light = done[0] if isinstance(done, tuple) else done
        comp = light.LightComponent
        comp.SetCastShadows(False)
        comp.SetSourceRadius(100.0)  # soft
        comp.SetVolumetricScatteringIntensity(0.0)
        S.light = WeakPointer(light)
    dist = math.dist(cam, target)
    comp = light.LightComponent
    comp.SetIntensity(LIGHT_PER_CM2 * dist * dist)
    if not LIGHT_AT_CAMERA:
        return LIGHT_PER_CM2 * dist * dist
    comp.SetAttenuationRadius(dist * 3)
    where = unrealsdk.make_struct("Vector", X=cam[0], Y=cam[1], Z=cam[2] + dist * LIGHT_RAISE)
    light.K2_SetActorLocation(where, False, unrealsdk.make_struct("HitResult"), True)
    return LIGHT_PER_CM2 * dist * dist


def _mesh_bounds(actors: list[UObject]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Centre and half-size of the box around the actors' visible meshes (their collision is bigger)."""
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    for actor in actors:
        for comp in actor.K2_GetComponentsByClass(unrealsdk.find_class("MeshComponent")):
            if not comp.bVisible or comp.bHiddenInGame:
                continue
            found = ksl.GetComponentBounds(comp, unrealsdk.make_struct("Vector"), unrealsdk.make_struct("Vector"), 0.0)
            origin, extent = found[1], found[2]  # (return, Origin, BoxExtent, SphereRadius)
            for i, axis in enumerate("XYZ"):
                o, e = getattr(origin, axis), getattr(extent, axis)
                lo[i] = min(lo[i], o - e)
                hi[i] = max(hi[i], o + e)
    if lo[0] == math.inf:
        raise RuntimeError("no visible meshes")
    return tuple((a + b) / 2 for a, b in zip(lo, hi)), tuple((b - a) / 2 for a, b in zip(lo, hi))  # type: ignore[return-value]


def _riders(actor: UObject, job: Job) -> list[UObject]:
    """Characters that came with the boss and ride on it (turrets, riders): they belong in the picture."""
    out = []
    for other in _near(job.booth):
        if other == actor or other._path_name() in job.nearby:  # noqa: SLF001
            continue
        parent = _attached_to(other)
        for _ in range(4):
            if parent is None or parent == actor:
                break
            parent = _attached_to(parent) if hasattr(parent, "RootComponent") else None
        if parent == actor:
            out.append(other)
    return out


def _place(cap: UObject, where: Any, yaw: float, pitch: float) -> None:
    rot = unrealsdk.make_struct("Rotator", pitch=pitch, yaw=yaw, roll=0)
    cap.K2_SetActorLocationAndRotation(where, rot, False, unrealsdk.make_struct("HitResult"), True)


def _probe(pc: UObject, shown: list[UObject], where: Any, yaw: float) -> tuple[list[int], list[int]]:
    """A small test capture (its own camera) of these actors: the pixels where something is drawn."""
    cap, comp = _camera(pc, probe=True)
    _place(cap, where, yaw, 0.0)
    comp.ShowOnlyActors = shown
    _step("test capture")
    comp.CaptureScene()
    _step("test read")
    raw = readback.floats(pc, S.probe, PROBE_SIZE * PROBE_SIZE)
    comp.ShowOnlyActors = []
    xs, ys = [], []
    for i, depth in enumerate(array.array("f", raw or b"")[0::4]):
        if depth < picture.NO_DEPTH:  # something solid there
            xs.append(i % PROBE_SIZE)
            ys.append(i // PROBE_SIZE)
    return xs, ys


def _frame(pc: UObject, shown: list[UObject], where: Any, yaw: float) -> tuple[float, float, float, int]:
    """
    Aims at where the boss really shows up: the mesh bounds are often far bigger than what's drawn
    (hidden parts, effects), which left some bosses tiny. A small test capture (its own camera)
    finds the drawn box; the picture camera turns to its centre and zooms (FOV) to fit it.
    Returns (yaw, pitch, fov, pixels drawn in the test capture).
    """
    xs, ys = _probe(pc, shown, where, yaw)
    if not xs:
        return yaw, 0.0, FOV, 0
    t = math.tan(math.radians(FOV / 2))
    # The drawn box in -1..1 screen units, right and up
    u0, u1 = min(xs) / PROBE_SIZE * 2 - 1, (max(xs) + 1) / PROBE_SIZE * 2 - 1
    v0, v1 = 1 - (max(ys) + 1) / PROBE_SIZE * 2, 1 - min(ys) / PROBE_SIZE * 2
    yaw += math.degrees(math.atan((u0 + u1) / 2 * t))
    pitch = math.degrees(math.atan((v0 + v1) / 2 * t))
    half = min(max(u1 - u0, v1 - v0) / 2 * t * FRAME_MARGIN, t)  # never wider than the test shot
    return yaw, pitch, math.degrees(2 * math.atan(half)), len(xs)


def _booth_view(job: Job) -> tuple[tuple[float, float, float], tuple[float, float, float], float] | None:
    """A booth's drawn box from its recipe: (centre, half-size, yaw); None without one."""
    bounds = job.recipe.get("bounds") if job.built is not None and job.recipe else None
    if not bounds:
        return None
    # The real boss's drawn box, in its own frame (a booth stands unturned at job.booth): a posed
    # booth's meshes report their bind pose's bounds, which framed Core Observer cropped
    centre = (job.booth[0] + bounds[0], job.booth[1] + bounds[1], job.booth[2] + bounds[2])
    return centre, (bounds[3], bounds[4], bounds[5]), 0.0


def _camera_spot(centre: tuple[float, float, float], extent: tuple[float, float, float], yaw: float) -> tuple[float, float, float]:
    """Back far enough that the taller of its height and width fills the frame, with a margin."""
    (cx, cy, cz), (ex, ey, ez) = centre, extent
    half = max(ez, ex, ey) * FRAME_MARGIN
    dist = half / math.tan(math.radians(FOV / 2)) + max(ex, ey)
    return (cx + math.cos(yaw) * dist, cy + math.sin(yaw) * dist, cz)


def _drawn(job: Job, count: int, now: float) -> bool:
    """
    A booth's probe found `count` pixels drawn: whether it can be photographed now (see
    DRAW_RETRY_S). Also notes whether the game is still filling booths in (S.unsettled_at).
    """
    if not job.probe_since:
        job.probe_since = now
    before, job.probed = job.probed, count
    filling = count == 0 or (before > 0 and abs(count - before) > max(2, before // 100))
    if filling:
        if not S.watch:
            log("A boss isn't drawn yet (the game is still building its shaders?): the pictures wait for it")
        S.watch, S.unsettled_at = True, now
    if not S.watch:
        return count > 0
    # The game was building shaders: this booth the same twice, and none filling in for a while
    return count > 0 and before > 0 and not filling and now - S.unsettled_at >= DRAW_CLEAR_S


def _watch(now: float) -> None:
    """While the game builds shaders: probes the next booth that's settling (one a frame)."""
    ready = [j for j in S.ahead if now >= j.next_probe and now - j.found_at > BOOTH_SETTLE_S]
    if not ready:
        return
    S.watch_next %= len(ready)
    job = ready[S.watch_next]
    S.watch_next += 1
    actor = job.actor()
    view = _booth_view(job)
    if actor is None or view is None:
        return
    centre, extent, yaw = view
    cam = _camera_spot(centre, extent, yaw)
    where = unrealsdk.make_struct("Vector", X=cam[0], Y=cam[1], Z=cam[2])
    xs, _ = _probe(get_pc(), [actor], where, math.degrees(yaw + math.pi))
    _drawn(job, len(xs), now)
    job.next_probe = now + DRAW_RETRY_S


def waiting() -> bool:
    """The pictures are waiting for the game to draw the booths (it's building shaders)."""
    return running() and S.watch and time.monotonic() - S.unsettled_at < DRAW_CLEAR_S


def _aim(job: Job) -> bool:
    """
    Works out the shot (one frame before taking it: one capture per frame). False: a booth that
    isn't drawn yet (the game is building shaders), to be aimed at again later.
    """
    pc = get_pc()
    actor = job.actor()
    if actor is None:
        raise RuntimeError("it disappeared")
    if job.built is not None:
        shown = [actor]  # the booth already holds every piece (riders, weapons: booth.record)
    else:
        _step("aim: hold")
        _hold(actor, job.booth)
        _step("aim: riders")
        shown = [actor, *_riders(actor, job)]
        if SHOW_ATTACHED:
            for a in list(shown):
                attached = a.GetAttachedActors([], True, True)
                shown += [x for x in (attached[1] if isinstance(attached, tuple) else attached) if x not in shown]
    _step(f"aim: bounds of {len(shown)}")
    view = _booth_view(job)
    if view is not None:
        (cx, cy, cz), (ex, ey, ez), yaw = view
    else:
        (cx, cy, cz), (ex, ey, ez) = _mesh_bounds(shown)
        yaw = math.radians(actor.K2_GetActorRotation().yaw)
    if S.record and job.built is None:
        _step("aim: record")
        try:
            recipe = booth.record(shown)
            loc = actor.K2_GetActorLocation()
            dx, dy = cx - loc.X, cy - loc.Y
            cos, sin = math.cos(-yaw), math.sin(-yaw)
            # Centre and half-size in the boss's frame (extents turned with it, as an AABB)
            recipe["bounds"] = [round(dx * cos - dy * sin, 1), round(dx * sin + dy * cos, 1), round(cz - loc.Z, 1),
                                round(abs(ex * cos) + abs(ey * sin), 1), round(abs(ex * sin) + abs(ey * cos), 1), round(ez, 1)]  # fmt: skip
            recipe["build"] = booth.game_build()  # the generator pins booth.jsonl to it
            S.recipes[job.boss["id"]] = recipe
        except Exception as ex:  # noqa: BLE001 - the picture still gets taken
            logging.error(f"[Kairosdex] Couldn't record the booth of {job.boss['name']}: {ex!r}")
    cam = _camera_spot((cx, cy, cz), (ex, ey, ez), yaw)
    where = unrealsdk.make_struct("Vector", X=cam[0], Y=cam[1], Z=cam[2])
    _light(pc, cam, (cx, cy, cz))  # a frame before the picture
    look, pitch, fov, drawn = _frame(pc, shown, where, math.degrees(yaw + math.pi))
    if job.built is not None and not _drawn(job, drawn, time.monotonic()):
        return False
    job.shot = (cam, look, pitch, fov, [WeakPointer(a) for a in shown])
    # Frozen from here: the shots that make the picture are taken on different frames and must line up
    for a in shown:
        a.CustomTimeDilation.Value = 0.0
        a.CustomTimeDilation.BaseValue = 0.0
    return True


def _shoot(job: Job) -> None:
    pc = get_pc()
    cam, look, pitch, fov, refs = job.shot
    shown = [a for a in (r() for r in refs) if a is not None]
    if not shown or job.actor() is None:
        raise RuntimeError("it disappeared")
    cap, comp = _camera(pc)
    _place(cap, unrealsdk.make_struct("Vector", X=cam[0], Y=cam[1], Z=cam[2]), look, pitch)
    comp.FOVAngle = fov
    comp.ShowOnlyActors = shown
    _step("capture")
    comp.CaptureScene()
    _step("read")
    boss_id = job.boss["id"]
    if not _hdr():
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        krl.ExportRenderTarget(pc, S.target, str(S.out_dir), f"{boss_id}.png")
    else:
        raw = readback.floats(pc, S.target, SIZE * SIZE)  # ~3 ms + the GPU finishing the capture
        if raw is None:
            raise RuntimeError("the capture couldn't be read back")
        job.hdr = raw
    comp.ShowOnlyActors = []


def _pass(job: Job) -> None:
    """
    The next of PASSES (a frame after the last shot; its capture source was set a frame ahead):
    the same view of the frozen boss.
    """
    pc = get_pc()
    cam, look, pitch, fov, refs = job.shot
    shown = [a for a in (r() for r in refs) if a is not None]
    if not shown or job.actor() is None:
        raise RuntimeError("it disappeared")
    cap, comp = _camera(pc, look=True)
    _place(cap, unrealsdk.make_struct("Vector", X=cam[0], Y=cam[1], Z=cam[2]), look, pitch)
    comp.FOVAngle = fov
    comp.ShowOnlyActors = shown
    _step(f"pass {comp.CaptureSource} capture")
    comp.CaptureScene()
    _step("pass read")
    raw = readback.floats(pc, S.look, SIZE * SIZE)
    comp.ShowOnlyActors = []
    if raw is None:
        raise RuntimeError("a pass couldn't be read back")
    job.passes.append(raw)


def _pass_source(source: int) -> None:
    """The capture source for the next pass (set a frame ahead of it)."""
    _, comp = _camera(get_pc(), look=True)
    comp.CaptureSource = source


def _hdr() -> bool:
    return S.source in (CAPTURE_SOURCE_HDR, 1)


def _backlogged() -> bool:
    """Enough pictures are waiting for the workers (COMPOSE_AHEAD / OFFLOAD_AHEAD): the next shot waits for them."""
    if offload.backlog("boss") >= OFFLOAD_AHEAD:
        return True
    return S.pending is not None and S.pending.qsize() >= COMPOSE_AHEAD and S.worker is not None and S.worker.is_alive()


def _convert_soon(job: Job) -> None:
    """Queues the picture for the worker (one at a time, so a long bake doesn't pile up threads)."""
    png = S.out_dir / f"{job.boss['id']}.png"
    if KEEP_RAW:
        raw_dir = S.out_dir / "raw"
        raw_dir.mkdir(exist_ok=True)
        for name, blob in zip(("hdr", "base", "normal", "depth"), (job.hdr, *job.passes), strict=False):
            (raw_dir / f"{job.boss['id']}.{name}.raw").write_bytes(blob)
    if offload.available():
        offload.submit("boss", job.boss["name"], png, SIZE, SIZE, job.hdr, *job.passes)
        return
    if S.pending is None:
        S.pending = queue.Queue()
    S.pending.put((job.hdr, list(job.passes), png, job.boss["name"]))
    if S.worker is None or not S.worker.is_alive():
        S.worker = threading.Thread(target=_convert_all, args=(S.pending,), name="weapondex pictures", daemon=True)
        S.worker.start()


def _convert_all(pending: queue.Queue) -> None:
    """Worker thread: makes each queued picture, until the queue has been empty a few seconds."""
    while True:
        try:
            hdr, passes, png, name = pending.get(timeout=5)
        except queue.Empty:
            return
        try:
            if len(passes) == len(PASSES):
                rgba = picture.compose(SIZE, SIZE, hdr, *passes)
            else:
                rgba = picture.from_capture(SIZE, SIZE, hdr)
            picture.write_png(png, SIZE, SIZE, rgba)
            picture.write_small(png, SIZE, SIZE, rgba)  # the copy the page's thumbnails use
            _write_index(png.parent)  # keep the page's list current while the bake runs
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Kairosdex] Couldn't make the picture of {name}: {ex}")


def _build_ahead(job: Job) -> None:
    try:
        _build_booth(job)
        job.stage = "settle"
        S.ahead.append(job)
    except Exception as ex:  # noqa: BLE001 - one bad boss mustn't stop the rest
        logging.error(f"[Kairosdex] No picture of {job.boss['name']}: {ex}")
        S.failed.append(job.boss["name"])
        _destroy(job)


def tick() -> None:
    """Call every frame from the mod's tick."""
    offload.poll()  # the pictures being made on interpreters of their own (bosses and items)
    if _LATE:
        _sweep_late()
    if not running():
        return
    pc = get_pc()
    if S.booths:
        # A booth is a still, plain actor of our own: no map or character needed (the title
        # screen). A map change takes it with the old world: stop
        world = _world()
        if pc is None or world is None or world != S.world():
            stop("stopped (the map changed)")
            return
        if pc.Pawn is not None and pc.bShowMouseCursor:
            # A menu up in a game: pictures taken behind the pause menu came out in the reference
            # pose at a low LOD (the meshes were right, the renderer never got them; online session,
            # world not paused, 2026-11-02). Wait for gameplay
            return
    elif pc is None or pc.Pawn is None:
        stop("stopped (left the game)")
        return
    elif pc.bShowMouseCursor:
        return  # a menu is up: the game is paused, so wait
    if S.booths:
        # Batched: the next booths are built (one a tick, not while a shot is being taken) and
        # settle while this one is photographed, so a boss costs its shots, not its settle time
        if (S.job is None or S.job.stage == "settle") and S.queue and len(S.ahead) + (S.job is not None) < BOOTH_AHEAD:
            _build_ahead(S.queue.pop(0))
        if S.job is None and S.ahead:
            S.job = S.ahead.pop(0)
        if S.job is None:
            if not S.queue:
                stop("done")
            return
    elif S.job is None:
        S.job = S.queue.pop(0)
    job = S.job
    now = time.monotonic()
    try:
        if job.stage == "spawn" and job.recipe is not None:
            _build_booth(job)
            job.stage = "settle"
        elif job.stage == "spawn":
            _spawn(job)
        elif job.stage == "wait":
            if _find(job):
                job.stage = "settle"
            elif now - job.started > SPAWN_TIMEOUT_S:
                raise RuntimeError("didn't spawn")
        elif job.stage == "settle":
            actor = job.actor()
            if actor is None:
                raise RuntimeError("it disappeared")
            if job.built is None:
                _step("settle: hold")
                _hold(actor, job.booth)
            elif not job.built.reposed and now - job.found_at > BOOTH_REPOSE_S:
                # The pose again, now that the forced LOD is in use; the aim waits a frame for it
                _step("settle: repose")
                job.built.repose()
                return
            settled = now - job.found_at > (BOOTH_SETTLE_S if job.built is not None else SETTLE_S)
            if settled and now >= job.next_probe and not _backlogged():
                if _aim(job):
                    job.stage = "shoot"
                elif now - job.probe_since > DRAW_TIMEOUT_S:
                    raise RuntimeError(f"not drawn in {DRAW_TIMEOUT_S:.0f} s (the game still building its shaders?)")
                else:
                    job.next_probe = now + DRAW_RETRY_S
            elif S.watch and S.booths:
                _watch(now)  # the frame's one capture: another booth, to see if the game is done
        elif job.stage == "shoot":
            _shoot(job)
            if _hdr():
                _pass_source(PASSES[0])
                job.stage = "passes"
            else:
                S.done.append(job.boss["id"])
                log(f"Picture taken: {job.boss['name']}")
                job.stage = "remove"
        elif job.stage == "passes":
            # One pass a frame
            _pass(job)
            if len(job.passes) < len(PASSES):
                _pass_source(PASSES[len(job.passes)])
            else:
                _convert_soon(job)
                S.done.append(job.boss["id"])
                log(f"Picture taken: {job.boss['name']}")
                job.stage = "remove"
        elif job.stage == "remove":
            # A frame after the capture, not in the same one
            _destroy(job)
            _mark(None)
            S.job = None
    except Exception as ex:  # noqa: BLE001 - one bad boss mustn't stop the rest
        logging.error(f"[Kairosdex] No picture of {job.boss['name']}: {ex}")
        S.failed.append(job.boss["name"])
        _destroy(job)
        _mark(None)
        S.job = None
    if S.job is None and not S.queue and not S.ahead:
        stop("done")


# endregion


def write_index(game_dir: Path) -> None:
    folder = pictures_dir(game_dir)
    folder.mkdir(parents=True, exist_ok=True)
    _write_index(folder)


def _write_index(folder: Path) -> None:
    """
    bosses/index.json, which the page reads: {boss id: version} for each picture, the version
    being its file's date, which the page puts in the picture's address so a picture taken again
    shows at once instead of the copy it already loaded.
    """
    picture.write_index(folder)  # the worker and the game thread may both write it: each its own temp file
