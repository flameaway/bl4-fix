"""
The route to the goal from the game's own planner: Echoloc, ECHO-4's guidance, the one the ECHO
map draws its route line with. Called directly, so the minimap's line is the map's line without
opening the map, and it follows a new goal at once.

What the map does (OakMapViewerWaypointPath's builder, code at 0x8d71162): a request struct
(0xE8 bytes, set up as at 0x4196530) goes to the planner's wrapper (_WRAPPER: rcx unused, rdx the
request, r8 the goal, r9 the start, then the view rotation and the world) and comes back with a
status (+0x48: 2 is a path) and the points (+0x70: TArray of FVector, doubles), you first. The
request owns what the planner allocated: it's freed as the map frees it (+0xB0, +0xA0, +0x50).
The line matched the map's to 0.1 m (World_P, 2026-11-05: 21 points to an objective 38 m off).

On a thread of ours, not the game thread: a call takes 0.05 ms for a short way but 18-60 ms for a
long winding one (it searches the level's graphs), a hitch each time. The game runs this same
planner off the game thread itself (gbx.player.waypoint_path_enable_async_build_path_to, on by
default: the task at 0x3d42b44 calls the planner with copies of its inputs, like `_run`). ctypes
lets go of the GIL for the call. The game thread only hands over a request (`ask`) and picks up
the answer (`answer`) at its next update.

The code is checked once before the first call (a patch moves it: then this stays off and the
minimap only has the ECHO map's route copied while the map was open).
"""

from __future__ import annotations

import ctypes
import struct
import threading

import unrealsdk

from .objectives import _k32, _read

_WRAPPER = 0x3D3E936  # the map's call into the planner (0x8d71288)
_FREE_B0 = 0x5C5B6E4  # the request's +0xB0 part (as 0x41966a0)
_FREE_50 = 0x88493EA  # its +0x50 part, the points among it (as 0x41966b6)
_FREE = 0x20A8  # FMemory::Free (its +0xA0 buffer, as 0x41966ae)
_CODE = {
    _WRAPPER: "56574881ec580100004889d60f28842490010000",
    _FREE_B0: "564883ec204889ce8b5130488b4928e8",
    _FREE_50: "564883ec204889ce31d2e85fffffff48",
    _FREE: "564883ec204885c974214889ce488b0d",
}
REQUEST_SIZE = 0xE8
GOAL, STATUS, POINTS, BUFFER = 0x28, 0x48, 0x70, 0xA0
FOUND = 2  # status: a path
MAX_POINTS = 10000

_P = ctypes.c_void_p
_D3 = ctypes.c_double * 3


class Echoloc:
    broken = ""
    ready = False
    # The worker: one request waiting at most (a newer one replaces it), its answer a whole-object swap
    asked: tuple[int, int, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]] | None = None
    answer: tuple[int, list[tuple[float, float]] | None] | None = None  # (ticket, path)
    ticket = 0
    wake = threading.Event()
    generation = 0  # a worker of an older generation stops (mod disabled, reloaded)
    thread: threading.Thread | None = None
    wrapper: ctypes._CFuncPtr | None = None
    free_b0: ctypes._CFuncPtr | None = None
    free_50: ctypes._CFuncPtr | None = None
    free: ctypes._CFuncPtr | None = None

    @classmethod
    def _setup(cls) -> bool:
        if cls.ready or cls.broken:
            return cls.ready
        base = _k32.GetModuleHandleW(None)
        for rva, code in _CODE.items():
            if _read(base + rva, len(code) // 2) != bytes.fromhex(code):
                cls.broken = "game code changed (a patch?)"
                unrealsdk.logging.warning(f"[Minimap] the game's route planner is off: {cls.broken}")
                return False
        cls.wrapper = ctypes.CFUNCTYPE(None, _P, _P, _P, _P, _P, _P)(base + _WRAPPER)
        cls.free_b0 = ctypes.CFUNCTYPE(None, _P)(base + _FREE_B0)
        cls.free_50 = ctypes.CFUNCTYPE(None, _P)(base + _FREE_50)
        cls.free = ctypes.CFUNCTYPE(None, _P)(base + _FREE)
        cls.ready = True
        return True

    @classmethod
    def ask(cls, world: int, start: tuple[float, float, float], goal: tuple[float, float, float],
            view: tuple[float, float, float]) -> int:  # fmt: skip
        """Asks the worker for `path` (game thread). Its ticket, for `answer`; 0 when the planner is off."""
        if not cls._setup():
            return 0
        cls.ticket += 1
        cls.asked = (cls.ticket, world, start, goal, view)
        if cls.thread is None:
            cls.wake = threading.Event()  # (each worker its own: an old one can't take a new one's wake-up)
            cls.thread = threading.Thread(target=cls._run, args=(cls.generation, cls.wake), name="bl4_minimap echoloc", daemon=True)
            cls.thread.start()
        cls.wake.set()
        return cls.ticket

    @classmethod
    def _run(cls, generation: int, wake: threading.Event) -> None:
        while True:
            wake.wait()
            wake.clear()
            if generation != cls.generation:
                return
            job, cls.asked = cls.asked, None
            if job is not None:
                cls.answer = (job[0], cls.path(*job[1:]))

    @classmethod
    def clear(cls) -> None:
        """Stops the worker (it ends after a call in progress) and drops what was asked."""
        cls.generation += 1
        cls.asked = cls.answer = None
        cls.thread = None
        cls.wake.set()

    @classmethod
    def path(cls, world: int, start: tuple[float, float, float], goal: tuple[float, float, float],
             view: tuple[float, float, float]) -> list[tuple[float, float]] | None:  # fmt: skip
        """
        The game's route from `start` to `goal` (world units, x, y of each point, you first), or None
        when it found none (off its graphs). `view`: your view rotation (pitch, yaw, roll); `world`:
        the UWorld's address. Blocks for the search: called on the worker (`ask`), after `_setup`.
        """
        req = ctypes.create_string_buffer(REQUEST_SIZE)
        # As the map sets it up (0x4196530); the rest is zero
        struct.pack_into("<B", req, 0x00, 1)
        struct.pack_into("<3d", req, GOAL, *goal)
        struct.pack_into("<f", req, 0x98, -1.0)
        struct.pack_into("<f", req, 0xB0, -1.0)
        struct.pack_into("<B", req, 0xBA, 2)
        at = ctypes.addressof(req)
        to, frm, rot = _D3(*goal), _D3(*start), _D3(*view)
        cls.wrapper(at, at, ctypes.addressof(to), ctypes.addressof(frm), ctypes.addressof(rot), world)
        out = None
        data, num = struct.unpack_from("<Qi", req, POINTS)
        if req.raw[STATUS] == FOUND and data and 1 < num <= MAX_POINTS:
            raw = _read(data, num * 24)
            if raw is not None:
                out = [struct.unpack_from("<2d", raw, i * 24) for i in range(num)]
        buffer = struct.unpack_from("<Q", req, BUFFER)[0]
        cls.free_b0(at + 0xB0)
        if buffer:
            cls.free(buffer)
        cls.free_50(at + 0x50)
        return out
