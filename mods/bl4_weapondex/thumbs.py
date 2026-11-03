"""
Takes each legendary's picture exactly as the inventory shows it, once: `dex bake items` (or the
dev button). Saved as loose files (uiresources/bl4weapondex/items/<item key>.png + index.json),
made from the player's own install like the boss pictures, so nothing is drawn at play time.

The inventory's pictures come from the game's thumbnail manager (OakThumbnailManager, a game
instance subsystem with no reflected functions): it renders an item's serial in its own thumbnail
scene into a 512x256 render target (OakInventoryThumbnail_*), but only when a card asks for one.
So for each item:
- we roll one ourselves (roll.new_serial: its serial, at the player's level; nothing is spawned);
- we ask the manager for that serial's thumbnail ourselves, through the native function every card
  uses (`RequestThumbnail(manager, out ThumbnailID, const FString& serial, const FIntPoint& size)`,
  found from the cards' callers), pinned to this build and checked before use;
- when its render target turns up, we wait out the manager's pre-render frames, read it straight
  back (RGBA, 0..255 floats with alpha) and the PNG is written on an interpreter of its own
  (offload.py; our writer thread when there's none).

Several items are in flight at once (IN_FLIGHT): the manager loads their assets side by side, so a
picture costs ~0.2 s instead of ~0.9 s one at a time (1.5-4.5 s with the old 4 Hz polling; measured
2026-11-02). The manager renders one item at a time, one attempt every OakUI.Thumbnails.RenderInterval
(0.0667 s here) with 5 pre-render attempts an item (NumPreRenderItemThumbnails), and forces the
capture after 0.2 s on an item when busy (~3 attempts = ~240 ms an item: what we measured). During a
bake (_pace) the pace is 0, an attempt every frame, and there are PRERENDER (12) attempts an item:
0.17 s an item. The pace alone (5 attempts in ~5 frames, 0.09 s an item) left textures half streamed
in: the same 48 serials rendered again (others in between pushed them out of the manager) came out
~10% less sharp than at the game's pace, fine patterns blurred and decals missing, worst with cold
textures (a game just launched); with 12 attempts as sharp as at the game's pace (8: nearly; 16: no
sharper). The median over different rolls hid it (2026-11-03). Several passes a frame
(OakUI.Thumbnails.MaxConsecutiveRenders 4) was 2.7x faster and as sharp in a warm game, but on a
fresh launch ~10% blurrier at the median (the worst items lost a third), and worse still with
PrestreamTextures on the thumbnail mesh or r.Streaming.FullyLoadUsedTextures beside it: textures
need real time to come in from disk, and only the item being drawn streams (the manager has one
mesh component in its own world and swaps each item into it). Not used; same items rendered cold,
then warm, compared per item (2026-11-03). The game's values are put back when
the bake ends or stops (RE of the manager's tick 0x868D10, 2026-11-03). A fully transparent read
(nothing drawn yet: 2 of ~1,600 at slower paces) is read again, never saved. Works without a
character too (titlebake.py, at the title screen): the roll reads the game's attributes on the
controller, at NO_CHARACTER_LEVEL, in caches of its own (roll.Detached).

Runs from the mod's tick, a few small steps a frame. A picture is read back as 8-bit colour
(ReadRenderTarget through readback.py, ~2 ms: the SDK's call was ~1.3 ms + ~13 ms letting go of its
array; the float read ReadRenderTargetRaw took 26-34 ms), one a tick.
The roll pictures (Shots: roll.py, breakdown.py) are separate and untouched by the bake.
"""

from __future__ import annotations

import ctypes
import queue
import struct
import threading
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

from . import offload, picture, readback, roll, serialgen, stats

# The manager's request, pinned to the build of 2026-10-22 (RVAs and the bytes there)
REQUEST_RVA = 0x11BD35E
REQUEST_HEAD = bytes.fromhex("41574156415541545657534881ecd00000004c89cb4889d6")
# A card's caller: passes the inventory's thumbnail size (a static FIntPoint) to the request
CALLER_RVA = 0x11BCE48
CALLER_HEAD = bytes.fromhex("564883ec204889d64c8d0de177a20be802050000")
SIZE_RVA = 0xCBE4638
MANAGER_VTABLE_RVA = 0xB7FCB60
WIDTH, HEIGHT = 512, 256
WRITER_IDLE_S = 5.0  # how often an idle writer checks whether a hot reload replaced it
RENDER_WAIT_S = 15.0  # from the request (with IN_FLIGHT queued, up to ~4.4 s was seen)
IN_FLIGHT = 8  # items asked for at once (24 items: 4.6 s, 0.19 s each; one at a time 0.88 s each)
SETTLE_S = 0.1  # after the render target appears (was 0.6; 0.1 checked against the shipped pictures)
NO_CHARACTER_LEVEL = 50  # the level items are rolled at with no character (title screen)
INTERVAL_CVAR = "OakUI.Thumbnails.RenderInterval"  # the manager's pace: 0 during a bake (see above)
# Its attempts an item before the capture: 12 during a bake (the game's 5 at an attempt a frame left
# textures half streamed in; see above)
PRERENDER_CVAR = "OakUI.Thumbnails.NumPreRenderItemThumbnails"
PRERENDER = 12
RETRIES = 2
KEEP_BUFFERS = 64  # _keep
SHOT_LOOK_S = 0.05  # a roll's picture: looked for often (cheap memory reads), read as soon as it's there
SHOT_SETTLE_S = 0.1

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetModuleHandleW.restype = ctypes.c_void_p
_k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_REQUEST_TYPE = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class S:
    active = False  # from start() until _finish()
    todo: list[str] = []  # noqa: RUF012 - item keys still to do
    rolling: str | None = None  # the item being rolled (its first roll takes a few ticks)
    flight: dict[str, Flight] = {}  # noqa: RUF012 - item key -> asked for, not read back yet
    tries: dict[str, int] = {}  # noqa: RUF012
    world: WeakPointer = WeakPointer()  # the bake stops when the map changes
    out_dir: Path | None = None
    done: list[str] = []  # noqa: RUF012
    failed: dict[str, str] = {}  # noqa: RUF012
    request: Any = None
    manager: WeakPointer | None = None
    writer: threading.Thread | None = None
    pending: queue.Queue = queue.Queue()
    on_finish: Any = None
    interval: float | None = None  # the game's INTERVAL_CVAR, while a bake has it at 0
    prerender: int | None = None  # the game's PRERENDER_CVAR, while a bake has it at PRERENDER
    leaked: list[Any] = []  # noqa: RUF012 - out buffers the game filled, kept a while (tiny; capped by _keep)


class Flight:
    def __init__(self, serial: str, now: float) -> None:
        self.serial, self.requested = serial, now
        self.address = 0  # its render target, once the manager has one
        self.found = 0.0


def log(msg: str) -> None:
    logging.info(f"[Kairosdex] {msg}")


def pictures_dir(game_dir: Path) -> Path:
    return game_dir / "OakGame/Content/uiresources/bl4weapondex/items"


def baked(game_dir: Path) -> list[str]:
    """Item keys that have a whole picture (picture.whole_pngs: a damaged file counts as missing)."""
    return picture.whole_pngs(pictures_dir(game_dir))


def running() -> bool:
    return S.active


# region The manager's request


def _image_base() -> int:
    return int(_k32.GetModuleHandleW(None) or 0)


def _resolve() -> str | None:
    """Finds the manager and checks the pinned code; None when it's safe to call, else why not."""
    base = _image_base()
    if not base:
        return "no game image"
    if stats._read(base + REQUEST_RVA, len(REQUEST_HEAD)) != REQUEST_HEAD:  # noqa: SLF001
        return "the thumbnail request isn't where this build had it (game patched?)"
    if stats._read(base + CALLER_RVA, len(CALLER_HEAD)) != CALLER_HEAD:  # noqa: SLF001
        return "the card's thumbnail call isn't where this build had it (game patched?)"
    if stats._read(base + SIZE_RVA, 8) != struct.pack("<ii", WIDTH, HEIGHT):  # noqa: SLF001
        return "the inventory's thumbnail size isn't 512x256"
    # The game instance's subsystem (0.2 ms; the find_all this was took 166 ms, a hitch on the
    # first roll picture)
    try:
        manager = unrealsdk.find_class("SubsystemBlueprintLibrary").ClassDefaultObject.GetGameInstanceSubsystem(
            get_pc(possibly_loading=True), unrealsdk.find_class("OakThumbnailManager"))  # fmt: skip
    except Exception as ex:  # noqa: BLE001
        return f"no thumbnail manager ({ex})"
    if manager is None:
        return "no thumbnail manager"
    if stats._u64(manager._get_address()) != base + MANAGER_VTABLE_RVA:  # noqa: SLF001
        return "the thumbnail manager isn't the class this build had"
    S.manager = WeakPointer(manager)
    S.request = _REQUEST_TYPE(base + REQUEST_RVA)
    return None


def _keep(buffer: Any) -> None:
    """
    Out buffers are only read during the call, but are kept a while anyway (cheap insurance); the
    list used to grow by two per roll for the whole session.
    """
    S.leaked.append(buffer)
    del S.leaked[:-KEEP_BUFFERS]


def _request(serial: str) -> bool:
    manager = S.manager() if S.manager else None
    if manager is None or S.request is None:
        return False
    text = ctypes.create_unicode_buffer(serial)  # FString data: UTF-16 with its NUL
    fstring = ctypes.create_string_buffer(struct.pack("<QII", ctypes.addressof(text), len(serial) + 1, len(serial) + 1))
    size = ctypes.create_string_buffer(struct.pack("<ii", WIDTH, HEIGHT))
    out = ctypes.create_string_buffer(0x40)  # ThumbnailID {FString; FIntPoint} + padding, zeroed
    S.request(manager._get_address(), ctypes.addressof(out), ctypes.addressof(fstring), ctypes.addressof(size))  # noqa: SLF001
    _keep(out)  # the game allocated the copy of the serial in it
    return True


# endregion


RENDERED = 0x48  # the manager's TMap<ThumbnailID, render target>: element array (TSparseArray)
ELEMENT_SIZE = 0x48  # {FString serial; FIntPoint size; ...; UTextureRenderTarget2D* @+0x38; hash links}
ELEMENT_TARGET = 0x38
MAX_ELEMENTS = 4096


def _rendered(serial: str) -> int | None:
    """The address of the render target the manager made for this serial, once it has one."""
    manager = S.manager() if S.manager else None
    if manager is None:
        return None
    head = stats._read(manager._get_address() + RENDERED, 16)  # noqa: SLF001
    if head is None:
        return None
    array, count = struct.unpack_from("<QI", head)
    if not 0 < count <= MAX_ELEMENTS:
        return None
    raw = stats._read(array, count * ELEMENT_SIZE)  # noqa: SLF001
    if raw is None:
        return None
    want = (serial + "\0").encode("utf-16-le")
    for at in range(0, len(raw), ELEMENT_SIZE):
        text, length = struct.unpack_from("<Qi", raw, at)
        if length * 2 != len(want) or stats._read(text, len(want)) != want:  # noqa: SLF001 - free slots fail here
            continue
        size = raw[at + 0x10 : at + 0x18]
        target, = struct.unpack_from("<Q", raw, at + ELEMENT_TARGET)
        if size == struct.pack("<ii", WIDTH, HEIGHT) and target:
            return target
    return None


_OUTERS: dict[int, str] = {}  # outer address -> path (the manager's capture component)
OBJECT_NAME_NUMBER = 0x1C  # UObject: FName (index, number) at +0x18, outer at +0x20
OBJECT_OUTER = 0x20


def _target_at(address: int) -> Any:
    """
    The render target at this address. By its name (OakInventoryThumbnail_<number - 1>) under its
    outer: a lookup (~0.05 ms), not a walk over every object (find_all, ~20 ms: a hitch per roll).
    The outer's path is found once with find_all.
    """
    raw = stats._read(address + OBJECT_NAME_NUMBER, 12)  # noqa: SLF001
    if raw is None:
        return None
    number, outer = struct.unpack("<IQ", raw)
    if outer not in _OUTERS:
        found = next((o for o in unrealsdk.find_all("SceneCaptureComponent2D", exact=False) if o._get_address() == outer), None)  # noqa: SLF001
        if found is None:
            return None
        _OUTERS[outer] = found._path_name()
    try:
        target = unrealsdk.find_object("TextureRenderTarget2D", f"{_OUTERS[outer]}.OakInventoryThumbnail_{number - 1}")
    except Exception:  # noqa: BLE001
        return None
    return target if target is not None and target._get_address() == address else None  # noqa: SLF001


def start(game_dir: Path, keys: list[str], on_finish: Any = None, out_dir: Path | None = None) -> str:
    if running():
        return "Already taking item pictures."
    pc = get_pc()
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    if pc is None or world is None:
        return "Load into the game first."
    why = _resolve()
    if why:
        return f"Can't take item pictures: {why}."
    if not stats.resolve_defs(pc):
        return "Can't take item pictures: item stats can't be read in this game version."
    S.out_dir = out_dir or pictures_dir(game_dir)
    S.out_dir.mkdir(parents=True, exist_ok=True)
    S.todo, S.done, S.failed, S.tries, S.on_finish = list(keys), [], {}, {}, on_finish
    S.rolling, S.flight = None, {}
    S.world = WeakPointer(world)
    S.active = True
    _pace(fast=True)
    if not offload.available():
        _start_writer()
    return f"Taking {len(keys)} item pictures (about {max(1, round(len(keys) * 0.2))} s)."


def stop(reason: str = "stopped") -> None:
    if not running():
        return
    S.todo.clear()
    S.rolling = None
    S.flight.clear()
    _finish(reason)


def _finish(reason: str) -> None:
    S.active = False
    _pace(fast=False)
    summary = f"Item pictures {reason}: {len(S.done)} taken"
    if S.failed:
        summary += f", {len(S.failed)} failed (" + ", ".join(f"{k}: {v}" for k, v in list(S.failed.items())[:5]) + ")"
    log(summary)
    if S.out_dir:
        if offload.available():
            offload.dirty(S.out_dir)
        else:
            S.pending.put(("index", S.out_dir, None))
    if S.on_finish:
        S.on_finish(summary)


def _pace(*, fast: bool) -> None:
    """
    During a bake the manager renders an attempt every frame, PRERENDER of them an item; the game's
    own pace otherwise. A console write marks a variable as set by the console (later lower-priority
    writes, e.g. a hotfix arriving mid-session, would be ignored); the game's values themselves are
    put back.
    """
    try:
        ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
        if fast:
            if S.interval is None:
                S.interval = float(ksl.GetConsoleVariableFloatValue(INTERVAL_CVAR))
                if S.interval > 0:
                    ksl.ExecuteConsoleCommand(None, f"{INTERVAL_CVAR} 0", None)
            if S.prerender is None:
                S.prerender = int(ksl.GetConsoleVariableIntValue(PRERENDER_CVAR))
                if S.prerender < PRERENDER:
                    ksl.ExecuteConsoleCommand(None, f"{PRERENDER_CVAR} {PRERENDER}", None)
            return
        if S.interval is not None:
            if S.interval > 0:
                ksl.ExecuteConsoleCommand(None, f"{INTERVAL_CVAR} {S.interval:.9g}", None)
            S.interval = None
        if S.prerender is not None:
            if S.prerender < PRERENDER:
                ksl.ExecuteConsoleCommand(None, f"{PRERENDER_CVAR} {S.prerender}", None)
            S.prerender = None
    except Exception as ex:  # noqa: BLE001 - only the pace; the bake works without it
        logging.error(f"[Kairosdex] thumbnail pace: {ex!r}")


def _fail(key: str, why: str) -> None:
    S.flight.pop(key, None)
    tries = S.tries.get(key, 0)
    if tries < RETRIES and why != "no roll rules for it":
        S.tries[key] = tries + 1
        S.todo.append(key)  # again later, with a new roll
        return
    S.failed[key] = why


def tick() -> None:
    """Call every frame from the mod's tick."""
    if not running():
        return
    pc = get_pc(possibly_loading=True)
    viewport = ENGINE.GameViewport
    world = viewport.World if viewport is not None else None
    if pc is None or world is None or world != S.world():
        stop("stopped (the map changed)")
        return
    now = time.monotonic()
    _roll_next(pc, now)
    if S.flight:
        _look(now)
        _read_settled(now)
    if not S.todo and S.rolling is None and not S.flight:
        _finish("done")


def _roll_next(pc: Any, now: float) -> None:
    """Rolls the next item and asks for its picture, while fewer than IN_FLIGHT are out."""
    if len(S.flight) >= IN_FLIGHT or (S.rolling is None and not S.todo):
        return
    if S.rolling is None:
        S.rolling = S.todo.pop(0)
    key = S.rolling
    try:
        if not serialgen.has(key):
            S.rolling = None
            _fail(key, "no roll rules for it")
            return
        if pc.Pawn is not None:
            rolled = roll.new_serial(key, pc)
        else:  # the title screen
            rolled = roll.new_serial(key, pc, context=pc, level=NO_CHARACTER_LEVEL)
        if rolled is None:
            return  # its weights are still being looked up
        S.rolling = None
        if not _request(rolled[0]):
            _fail(key, "the thumbnail manager is gone")
            return
        S.flight[key] = Flight(rolled[0], now)
    except Exception as ex:  # noqa: BLE001 - one bad item mustn't stop the rest
        logging.error(f"[Kairosdex] item picture {key}: {ex!r}")
        S.rolling = None
        _fail(key, repr(ex))


def _look(now: float) -> None:
    """One pass over the manager's map for every picture still being made."""
    waiting = {f.serial: key for key, f in S.flight.items() if not f.address}
    if not waiting:
        return
    for serial, address in _rendered_all(list(waiting)).items():
        flight = S.flight[waiting[serial]]
        flight.address, flight.found = address, now
    for key, flight in list(S.flight.items()):
        if not flight.address and now - flight.requested > RENDER_WAIT_S:
            _fail(key, "no picture was rendered")


def _read_settled(now: float) -> None:
    """Reads back one settled picture (each read is ~15 ms of the game thread: one a tick)."""
    ready = [(key, f) for key, f in S.flight.items() if f.address and now - f.found >= SETTLE_S][:1]
    if not ready:
        return
    current = _rendered_all([f.serial for _, f in ready])
    for key, flight in ready:
        address = current.get(flight.serial)
        if address != flight.address:
            if address:
                flight.address, flight.found = address, now  # rendered again elsewhere: settle again
            else:
                _fail(key, "the picture was dropped before it could be read")
            continue
        target = _target_at(address)
        bgra = _read_bgra(target) if target is not None else None
        if bgra is None:
            _fail(key, "couldn't read the picture back")
            continue
        if not bgra[3::4].strip(b"\0"):
            # Nothing drawn in it yet (fully transparent): 2 of ~1,600 pictures, at slower paces
            # (2026-11-03). Look again after another settle
            if now - flight.requested < RENDER_WAIT_S:
                flight.found = now
            else:
                _fail(key, "the picture stayed empty")
            continue
        del S.flight[key]
        assert S.out_dir is not None
        _save(S.out_dir / f"{key}.png", bgra)
        S.done.append(key)


def _save(png: Path, bgra: bytes) -> None:
    """To an interpreter of its own (offload.py), else to our writer thread."""
    if offload.available():
        offload.submit("item", png.stem, png, WIDTH, HEIGHT, bgra)
    else:
        _start_writer()
        S.pending.put(("bgra", png, bgra))


def _read_bgra(target: Any) -> bytes | None:
    """
    The picture as 8-bit BGRA (ReadRenderTarget's FColor). The thumbnails are RGBA16f holding 0..255
    whole numbers: byte-identical to ReadRenderTargetRaw rounded (every pixel of 6 pictures,
    2026-11-02), and nothing left to convert. Through readback's kept block: the SDK's call took
    ~15 ms, 13 of them letting go of its array.
    """
    return readback.colors(ENGINE.GameViewport.World, target, WIDTH * HEIGHT)


def _rendered_all(serials: list[str]) -> dict[str, int]:
    """{serial: its render target's address} for these serials: one pass over the manager's map."""
    manager = S.manager() if S.manager else None
    if manager is None:
        return {}
    head = stats._read(manager._get_address() + RENDERED, 16)  # noqa: SLF001
    if head is None:
        return {}
    array, count = struct.unpack_from("<QI", head)
    if not 0 < count <= MAX_ELEMENTS:
        return {}
    raw = stats._read(array, count * ELEMENT_SIZE)  # noqa: SLF001
    if raw is None:
        return {}
    want = {(s + "\0").encode("utf-16-le"): s for s in serials}
    lengths = {len(w) // 2 for w in want}
    size = struct.pack("<ii", WIDTH, HEIGHT)
    found: dict[str, int] = {}
    for at in range(0, len(raw), ELEMENT_SIZE):
        text, length = struct.unpack_from("<Qi", raw, at)
        if length not in lengths:  # free slots and other serials' lengths: no read
            continue
        serial = want.get(stats._read(text, length * 2) or b"")  # noqa: SLF001
        target, = struct.unpack_from("<Q", raw, at + ELEMENT_TARGET)
        if serial is not None and raw[at + 0x10 : at + 0x18] == size and target:
            found[serial] = target
    return found


def _read_picture(target: Any) -> bytes | None:
    krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
    samples = krl.ReadRenderTargetRaw(ENGINE.GameViewport.World, target, [], False)[1]
    if len(samples) != WIDTH * HEIGHT:
        return None
    # FLinearColor array, copied out at once (it only lives as long as `samples`)
    return bytes((ctypes.c_float * (4 * WIDTH * HEIGHT)).from_address(samples[0]._get_address()))  # noqa: SLF001


def _start_writer() -> None:
    if S.writer is None or not S.writer.is_alive():
        S.writer = threading.Thread(target=_write_all, args=(S.pending,), name="weapondex-thumbs", daemon=True)
        S.writer.start()


# region One picture of a rolled item (roll.py)


class Shot:
    def __init__(self, serial: str, now: float) -> None:
        self.serial, self.since, self.looked = serial, now, now
        self.target: WeakPointer | None = None
        self.seen_at = 0.0


class Shots:
    pending: dict[str, Shot] = {}  # noqa: RUF012 - item key -> its latest roll's picture, being made
    unavailable: str | None = None  # why rolls can't have pictures (checked once)
    started: dict[str, float] = {}  # noqa: RUF012 - for timing checks
    took: list[int] = []  # noqa: RUF012


def shoot(key: str, serial: str) -> bool:
    """
    Asks the game for the inventory picture of a rolled item (by its serial);
    tick_shots reports it once rendered. False if that can't be done here. Game thread.
    """
    if running():
        return False  # the bake is using the manager
    manager = S.manager() if S.manager else None
    if manager is None or S.request is None:
        if Shots.unavailable is not None:
            return False
        Shots.unavailable = _resolve()
        if Shots.unavailable:
            logging.dev_warning(f"[Kairosdex] no pictures for rolls: {Shots.unavailable}")
            return False
    if not _request(serial):
        return False
    Shots.pending[key] = Shot(serial, time.monotonic())
    Shots.started[key] = time.monotonic()
    return True


def tick_shots() -> list[tuple[str, dict[str, Any]]]:
    """
    Moves the pictures along; returns [(item key, picture)] for the ones rendered since last time:
    {"serial", "address", "path"} of the game's own render target. The page shows that render
    target itself (like the inventory does), so no pixels are read back: ReadRenderTargetRaw waits
    for the GPU, 36-71 ms a picture (measured 2026-10-24).
    """
    now = time.monotonic()
    done = []
    for key, shot in list(Shots.pending.items()):
        if shot.target is None:
            if now - shot.looked < SHOT_LOOK_S:
                continue
            shot.looked = now
            address = _rendered(shot.serial)
            target = _target_at(address) if address else None
            if target is None:
                if now - shot.since > RENDER_WAIT_S:
                    del Shots.pending[key]
                continue
            shot.target, shot.seen_at = WeakPointer(target), now
            continue
        if now - shot.seen_at < SHOT_SETTLE_S:
            continue
        del Shots.pending[key]
        target = shot.target()
        if target is not None and _rendered(shot.serial) == (address := target._get_address()):  # noqa: SLF001
            done.append((key, {"serial": shot.serial, "address": address, "path": target._path_name()}))
            Shots.took = [*Shots.took, round((now - shot.since) * 1000)][-20:]
    return done


def follow(picture: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    The picture as the manager holds it now, or None once it dropped it (past 100). The manager
    sometimes renders a serial again into a new render target (seen on a class mod 2026-10-24,
    within a second): then the picture moves with it.
    """
    if not picture:
        return None
    address = _rendered(picture["serial"])
    if address is None:
        return None
    if address != picture["address"]:
        target = _target_at(address)
        if target is None:
            return None
        picture.update(address=address, path=target._path_name())  # noqa: SLF001
    return picture


# endregion


def _write_all(pending: queue.Queue) -> None:
    dirty: set[Path] = set()  # folders whose index.json is behind
    while True:
        try:
            kind, path, raw = pending.get(timeout=WRITER_IDLE_S)
        except queue.Empty:
            if pending is not S.pending:
                return  # a hot reload made a new queue (and its own writer)
            continue
        try:
            if kind == "index":
                dirty.add(path)
            else:
                rgba = _to_rgba(raw)
                path.parent.mkdir(parents=True, exist_ok=True)
                picture.write_png(path, WIDTH, HEIGHT, rgba)
                # The copy the page's thumbnails use
                w, h, small = picture.shrink(WIDTH, HEIGHT, rgba)
                small_png = picture.small_path(path)
                small_png.parent.mkdir(parents=True, exist_ok=True)
                picture.write_png(small_png, w, h, small)
                dirty.add(path.parent)
            if pending.empty():
                # The indexes list every picture (a folder scan): once the queue is drained, not per picture
                for folder in dirty:
                    _write_index(folder)
                    picture.write_small_index(folder)
                dirty.clear()
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Kairosdex] couldn't save {path.name}: {ex!r}")


def _to_rgba(bgra: bytes) -> bytes:
    """FColor order (B, G, R, A) as RGBA: four C-level slice copies, ~0.6 ms a picture."""
    rgba = bytearray(len(bgra))
    rgba[0::4] = bgra[2::4]
    rgba[1::4] = bgra[1::4]
    rgba[2::4] = bgra[0::4]
    rgba[3::4] = bgra[3::4]
    return bytes(rgba)


def _write_index(folder: Path) -> None:
    """items/index.json for the page: {item key: version (the file's date)}."""
    picture.write_index(folder)
