"""
Instant Fast Travel for Borderlands 4: fast travel on the same map without the portal.

Pick a fast travel station on the ECHO map as usual. If it's on the map you're on, you're put
there at once, on the spot the game would have put you, instead of going through the travel
portal. Stations on another map still go through the game's own travel (the level has to load).

How: the map asks the server for the trip with OakPlayerController.RequestTeleportToStation (a
server RPC). We catch the call before it goes out, block it, and teleport the character to the
station ourselves (land.py), the same kind of jump the Big Encore travel of Item Cards and UVH Boss
Teleport makes. If anything about the trip doesn't fit (another map, a level-travel station, a
respawn, no station data, no room), the game's own fast travel runs as normal.

Co-op: a client's position belongs to the host, so a client's trip is made by the host's copy of
this mod through BL4 Direct's co-op channel; with a host that doesn't run it, a client gets the
normal portal. Friends who don't run it get the normal portal too.

Everything but this file can be hot-reloaded (dev/reload_instant_travel.py).
"""

from __future__ import annotations

import time
from typing import Any

from mods_base import ENGINE, SETTINGS_DIR, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, FGameDataHandle, UObject, WeakPointer, WrappedStruct

from . import health, land, stations

try:
    from bl4_direct import coop
except Exception:  # noqa: BLE001 - BL4 Direct missing or broken: solo / host only
    coop = None

__version__ = "0.1.1"
__version_info__ = (0, 1, 1)

GAME_DIR = SETTINGS_DIR.parent.parent
STATION_TYPE = 24576  # def handle type of travel stations
FEATURE = KIND = "instant_travel"
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0


def log(msg: str) -> None:
    logging.info(f"[Instant Travel] {msg}")


class S:
    # Trips caught in the hook, made on the next tick (never inside the RPC hook):
    # (WeakPointer to the controller, land.Target)
    queue: list[tuple[WeakPointer, land.Target]] = []  # noqa: RUF012
    # Set while we call the game's own fast travel ourselves (a fallback): the hook lets it through
    bypass = False
    errors: set[str] = set()  # noqa: RUF012
    checked = False  # stations.json's pin check started


def _current_map() -> str:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    return str(world.Name) if world else ""


def _target(handle: Any) -> land.Target | None:
    """
    The station if it's a plain fast travel station on the map we're on, else None (the game
    travels as normal). The handle the map passes is unresolved (only its name, e.g.
    `World_P.FT_GRA_West_Silo2`), so where it stands comes from stations.json, which lists only
    fast travel stations (not level travel, respawn or send-only ones).
    """
    if handle is None:
        return None
    name = str(handle._name)
    map_name, _, _ = name.partition(".")
    if not map_name or map_name.lower() != _current_map().lower():
        return None
    found = stations.find(name)
    if found is None:
        return None
    x, y, z, yaw, nested = found
    # Inside a level instance: whether the baked spot is world space wasn't checked, so only
    # while the station itself is loaded (land.place then uses its real spawn spots)
    if nested and land.station_actor(name) is None:
        return None
    return land.Target(name, x, y, z, yaw)


def _travel_normally(pc: UObject, target: land.Target) -> None:
    """The game's own fast travel to the station, portal and all (when ours couldn't happen)."""
    fn = pc.RequestTeleportToStation
    args = WrappedStruct(fn.func)
    args.station = FGameDataHandle(STATION_TYPE, target.name)
    args.bForRespawn = False
    args.bLocalOnly = False
    S.bypass = True
    try:
        fn(args)
    finally:
        S.bypass = False


@hook("/Script/OakGame.OakPlayerController:RequestTeleportToStation", Type.PRE)
def on_request(obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> Any:
    if S.bypass:
        return None
    try:
        # A call that arrived from a friend's game (they don't run this, or it would have been
        # caught on their side): theirs to have as normal
        if args.bForRespawn or not obj.IsLocalController():
            return None
        target = _target(args.station)
        if target is None:
            return None
        if coop is not None and coop.is_client() and not coop.host_has(FEATURE):
            return None  # the host can't move us: the normal portal
        S.queue.append((WeakPointer(obj), target))
        return Block
    except Exception as ex:  # noqa: BLE001 - a fast travel must never die in our hook
        logging.error(f"[Instant Travel] {ex!r}")
        return None


def _make_trip(pc: UObject, target: land.Target) -> None:
    def done(reply: dict[str, Any]) -> None:
        if reply.get("ok"):
            log(f"At {target.name}")
            return
        log(f"Couldn't land at {target.name} ({reply.get('error')}): fast travelling as normal")
        now_pc = get_pc()
        if now_pc is not None and now_pc.Pawn is not None:
            _travel_normally(now_pc, target)

    if coop is not None:
        if not coop.run(KIND, target.as_dict(), done, feature_name=FEATURE):
            _travel_normally(pc, target)  # the host stopped running it meanwhile
        return
    done(land.place(pc, target))


def _host_trip(req: Any) -> dict[str, Any]:
    """Host side: puts the asking player (any player running this) on a station of this map."""
    pc = req.controller()
    if pc is None:
        return {"ok": False, "error": "gone"}
    try:
        target = land.Target.from_dict(req.data)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "bad request"}
    if target.name.partition(".")[0].lower() != _current_map().lower():
        return {"ok": False, "error": "another map"}
    return land.place(pc, target)


if coop is not None:
    coop.handle(KIND, _host_trip)
    coop.feature(FEATURE, lambda: mod.is_enabled)


@hook(TICK_FUNC, Type.POST)
def _tick(*_: Any) -> None:
    global _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    if not S.checked:
        S.checked = True
        stations.start_check(GAME_DIR)  # on a worker thread
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
    except Exception:  # noqa: BLE001 - never let it break the game's tick
        pass
    if not S.queue and not land.S.settling:
        return
    try:
        queue, S.queue = S.queue, []
        for weak_pc, target in queue:
            pc = weak_pc()
            if pc is not None and pc.Pawn is not None:
                _make_trip(pc, target)
        land.tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        if repr(ex) not in S.errors:
            S.errors.add(repr(ex))
            logging.exception(ex)
        land.cancel()


def _disable() -> None:
    S.queue = []
    land.cancel()


mod = build_mod(hooks=[on_request, _tick], on_disable=_disable)
