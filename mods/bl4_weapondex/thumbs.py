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
  found from the cards' callers, see CLAUDE.md), pinned to this build and checked before use;
- when its render target turns up, we wait out the manager's pre-render frames, read it straight
  back (RGBA, 0..255 floats with alpha) and a worker thread writes the PNG.

Runs one step at a time from the mod's tick. Dev/bake only.
"""

from __future__ import annotations

import ctypes
import json
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

from . import picture, roll, serialgen, stats

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
RENDER_WAIT_S = 15.0
LOOK_EVERY_S = 0.25  # how often to look in the manager's map for ours (memory reads)
SETTLE_S = 0.6  # after the render target appears: the manager's pre-render frames and the capture
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
    key: str | None = None
    stage = ""
    since = 0.0
    looked = 0.0
    tries = 0
    target: WeakPointer | None = None
    seen_at = 0.0
    out_dir: Path | None = None
    done: list[str] = []  # noqa: RUF012
    failed: dict[str, str] = {}  # noqa: RUF012
    serial = ""
    request: Any = None
    manager: WeakPointer | None = None
    writer: threading.Thread | None = None
    pending: queue.Queue = queue.Queue()
    on_finish: Any = None
    leaked: list[Any] = []  # noqa: RUF012 - out buffers the game filled, kept a while (tiny; capped by _keep)


def log(msg: str) -> None:
    logging.info(f"[Weapondex] {msg}")


def pictures_dir(game_dir: Path) -> Path:
    return game_dir / "OakGame/Content/uiresources/bl4weapondex/items"


def baked(game_dir: Path) -> list[str]:
    return [p.stem for p in pictures_dir(game_dir).glob("*.png")]


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


def start(game_dir: Path, keys: list[str], on_finish: Any = None) -> str:
    if running():
        return "Already taking item pictures."
    pc = get_pc()
    if pc is None or pc.Pawn is None:
        return "Load into the game first."
    why = _resolve()
    if why:
        return f"Can't take item pictures: {why}."
    if not stats.resolve_defs(pc):
        return "Can't take item pictures: item stats can't be read in this game version."
    S.out_dir = pictures_dir(game_dir)
    S.out_dir.mkdir(parents=True, exist_ok=True)
    S.todo, S.done, S.failed, S.on_finish = list(keys), [], {}, on_finish
    S.active = True
    S.key, S.stage = None, ""
    _start_writer()
    return f"Taking {len(keys)} item pictures (about {len(keys) * 4 // 60 + 1} min)."


def stop(reason: str = "stopped") -> None:
    if not running():
        return
    S.todo.clear()
    S.key = None
    _finish(reason)


def _finish(reason: str) -> None:
    S.active = False
    summary = f"Item pictures {reason}: {len(S.done)} taken"
    if S.failed:
        summary += f", {len(S.failed)} failed (" + ", ".join(f"{k}: {v}" for k, v in list(S.failed.items())[:5]) + ")"
    log(summary)
    if S.out_dir:
        S.pending.put(("index", S.out_dir, None))
    if S.on_finish:
        S.on_finish(summary)


def _fail(why: str) -> None:
    if S.tries < RETRIES and why != "no roll rules for it":
        S.tries += 1
        S.stage = "drop"
        return
    S.failed[S.key or "?"] = why
    S.key = None


def tick() -> None:
    """Call every frame from the mod's tick."""
    if not running():
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None:
        stop("stopped (left the game)")
        return
    now = time.monotonic()
    if S.key is None:
        if not S.todo:
            _finish("done")
            return
        S.key, S.stage, S.tries = S.todo.pop(0), "drop", 0
    try:
        _step(pc, now)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Weapondex] item picture {S.key}: {ex!r}")
        S.failed[S.key or "?"] = repr(ex)
        S.key = None


def _step(pc: Any, now: float) -> None:
    key = S.key
    assert key is not None
    if S.stage == "drop":
        if not serialgen.has(key):
            _fail("no roll rules for it")
            return
        rolled = roll.new_serial(key, pc)
        if rolled is None:
            return  # its weights are still being looked up
        S.serial = rolled[0]
        if not _request(S.serial):
            _fail("the thumbnail manager is gone")
            return
        S.stage, S.since, S.looked, S.target = "render", now, now, None
        return
    if S.stage == "render":
        if now - S.looked < LOOK_EVERY_S:
            return
        S.looked = now
        # The manager's own map says which render target is this serial's (renders can finish out of
        # order: watching for "the newest render target" put pictures under the wrong items)
        address = _rendered(S.serial)
        target = _target_at(address) if address else None
        if target is None:
            if now - S.since > RENDER_WAIT_S:
                _fail("no picture was rendered")
            return
        S.target, S.seen_at, S.stage = WeakPointer(target), now, "settle"
        return
    if S.stage == "settle":
        if now - S.seen_at < SETTLE_S:
            return
        target = S.target() if S.target else None
        if target is None or _rendered(S.serial) != target._get_address():  # noqa: SLF001
            _fail("the picture was dropped before it could be read")
            return
        raw = _read_picture(target)
        if raw is None:
            _fail("couldn't read the picture back")
            return
        S.pending.put(("png", S.out_dir / f"{key}.png", raw))
        S.done.append(key)
        S.key = None


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
            logging.dev_warning(f"[Weapondex] no pictures for rolls: {Shots.unavailable}")
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
    while True:
        try:
            kind, path, raw = pending.get(timeout=WRITER_IDLE_S)
        except queue.Empty:
            if pending is not S.pending:
                return  # a hot reload made a new queue (and its own writer)
            continue
        try:
            if kind == "index":
                _write_index(path)
                continue
            rgba = _to_bytes(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            picture.write_png(path, WIDTH, HEIGHT, rgba)
            picture.write_small(path, WIDTH, HEIGHT, rgba)  # the copy the page's thumbnails use
            _write_index(path.parent)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Weapondex] couldn't save {path.name}: {ex!r}")


def _to_bytes(raw: bytes) -> bytes:
    """
    ReadRenderTargetRaw's float RGBA (0..255) as 8-bit RGBA. A row at a time, letting go of the GIL
    after each: converting a whole picture at once (~85 ms of Python) made every hook of every mod
    on the game thread wait for it, 5-60 ms per call (measured 2026-10-24). The pause must really
    sleep: sleep(0) hands the GIL over and takes it straight back, and at Python's default 5 ms
    switch interval that starved the game thread (~95 ms a turn, offline).
    """
    floats = memoryview(raw).cast("f")
    out = bytearray(len(floats))
    row = 4 * WIDTH
    for start in range(0, len(floats), row):
        out[start : start + row] = bytes(max(0, min(255, round(v))) for v in floats[start : start + row])
        time.sleep(0.001)
    return bytes(out)


def _write_index(folder: Path) -> None:
    """items/index.json for the page: {item key: version (the file's date)}."""
    index = {p.stem: int(p.stat().st_mtime) for p in sorted(folder.glob("*.png"))}
    tmp = folder / f"index.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(folder / "index.json")
