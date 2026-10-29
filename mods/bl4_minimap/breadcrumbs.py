"""
The route to the objective, live: our own path over the game's breadcrumb graph.

The ECHO map's route line follows Gearbox's "breadcrumb" graph: hand-placed route nodes per level,
kept by the `BreadcrumbRuntimeGraph` world subsystem (no reflected fields). Its +0x38 is a TArray of
56-byte nodes: {8 bytes, FVector position (doubles), 8 bytes of flags, TArray<node*> links}. The
map's route runs exactly through these nodes (plus a short lead-in from the player), so a shortest
path over the graph from the node nearest you to the node nearest the objective gives the same line,
and it's there as soon as the objective moves (the map's copy only refreshes when the map opens).

Reading the graph: the nodes are one block (one read); their link lists are separate allocations,
read a few hundred per call so no frame takes more than a few ms. Everything is read with
ReadProcessMemory (a bad address gives None) and checked: the node count, the positions, and every
link must point at a node.
"""

from __future__ import annotations

import heapq
import math
import struct
import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import UObject, WeakPointer

from .objectives import _read

NODE_SIZE = 0x38
# A node's position (3 doubles at +8) and its on/off byte (+0x21), read for every node at once
NODE = struct.Struct("<8x3d1xB" + f"{NODE_SIZE - 0x22}x")
NODES_OFFSET = 0x38  # TArray<node> in BreadcrumbRuntimeGraph
MAX_NODES = 100000
LINKS_PER_CALL = 300  # link lists read per call while loading (~15 us each)
REPLAN_GOAL_MOVE = 500.0  # world units the goal may move before the route is planned again
REPLAN_OFF_ROUTE = 4000.0  # how far you can stray from the route before it's planned again from you
REPLAN_MIN_S = 1.0
# Further than this (world units) from every node, the graph doesn't cover where you are: interiors
# like the Speakeasy have no nodes (the nearest was 686 m away). The ECHO map's route is used there.
OFF_GRAPH = 10000.0
FLAGS_REFRESH_S = 5.0  # nodes switch on and off with mission progress
PRELOAD_RETRY_S = 1.0
GRID = 5000.0  # world units per cell of the nearest-node lookup grid
MAX_RINGS = 40  # cells around the start the nearest-node lookup searches (200 m)
# The way back onto a route you stepped off (`reconnect`): a search this far around you (world units)
# and over at most this many nodes. A whole plan across World_P took 5 ms typically, 31 ms at worst
# (offline, its 11966 nodes), too slow to redo as often as you leave the line.
RECONNECT_RADIUS = 30000.0
RECONNECT_MAX_NODES = 800
RECONNECT_OFF = 1000.0  # further than this from the way back, look for a new one
RECONNECT_MIN_S = 0.5
BACK_CHECK_POINTS = 60  # the part of the way back checked each update (you're near its start)
ON_NODE = 100.0  # a route point this close to a node is that node (the map's line runs through them)


class Breadcrumbs:
    graph = WeakPointer()
    graph_class: Any = None
    library: Any = None
    world = ""
    nodes_addr = 0
    count = 0
    pos: list[tuple[float, float, float]] = []  # noqa: RUF012
    enabled: bytes = b""  # per node: 0 = switched off
    links: list[list[int]] = []  # noqa: RUF012
    grid: dict[tuple[int, int], list[int]] = {}  # noqa: RUF012  # cell -> nodes
    loaded = 0  # nodes whose links are read
    broken = ""
    flags_at = 0.0
    route: list[tuple[float, float]] = []  # noqa: RUF012  # the planned route (world x, y), you -> goal
    goal: tuple[float, float, float] | None = None
    planned_at = 0.0
    planned_from: tuple[float, float, float] = (0.0, 0.0, 0.0)
    reaches_goal = False  # the route's last node is near the goal (not just the node nearest it)
    preload_at = 0.0  # `preload`: look for a missing graph again at this time
    # `reconnect`'s last answer: (the route it was for, the way, when)
    back_line: Any = None
    back_way: list[tuple[float, float]] | None = None
    back_at = 0.0

    @classmethod
    def clear(cls) -> None:
        cls.graph = WeakPointer()
        cls.world = ""
        cls.count = cls.loaded = 0
        cls.pos, cls.enabled, cls.links, cls.grid = [], b"", [], {}
        cls.route, cls.goal = [], None
        cls.flags_at = cls.planned_at = 0.0
        cls.reaches_goal = False
        cls.back_line, cls.back_way, cls.back_at = None, None, 0.0

    @classmethod
    def _fail(cls, why: str) -> None:
        if not cls.broken:
            unrealsdk.logging.warning(f"[Minimap] live route off: {why}")
        cls.broken = why
        cls.route = []

    @classmethod
    def _read_nodes(cls, *, flags_only: bool = False) -> bool:
        """
        Positions and flags of every node (one read). flags_only: just the on/off bytes, for the
        refresh every FLAGS_REFRESH_S (positions don't move). Both run in C, not per node in Python:
        a loop over World_P's 11966 nodes took ~20 ms, a hitch every 5 s (Mod Profiler, 2026-10-29).
        """
        blob = _read(cls.nodes_addr, cls.count * NODE_SIZE) if cls.count else b""
        if blob is None:
            return False
        enabled = blob[0x21::NODE_SIZE]  # the +0x21 byte of each node
        if not flags_only:
            pos = [row[:3] for row in NODE.iter_unpack(blob)]
            # NaN fails the comparison too
            if not all(map((1e7).__gt__, map(abs, (v for p in pos for v in p)))):
                cls._fail("node positions look wrong")
                return False
            cls.pos = pos
        cls.enabled = enabled
        cls.flags_at = time.monotonic()
        return True

    @classmethod
    def _load(cls, pc: UObject, world_name: str) -> bool:
        """Finds this level's graph and reads it, a part per call. True once it's all read."""
        if cls.broken:
            return False
        if world_name != cls.world or cls.graph() is None:
            cls.clear()
            cls.world = world_name
            if cls.library is None:
                cls.library = unrealsdk.find_class("SubsystemBlueprintLibrary").ClassDefaultObject
                cls.graph_class = unrealsdk.find_class("BreadcrumbRuntimeGraph")
            graph = cls.library.GetWorldSubsystem(pc, cls.graph_class)
            if graph is None:
                return False
            head = _read(graph._get_address() + NODES_OFFSET, 16)  # noqa: SLF001
            if head is None:
                return False
            cls.nodes_addr, cls.count = struct.unpack_from("<Qi", head, 0)
            if not 0 <= cls.count <= MAX_NODES:
                cls._fail(f"odd node count {cls.count}")
                return False
            if not cls._read_nodes():
                return False
            cls.graph = WeakPointer(graph)
            cls.links = []
        if cls.loaded < cls.count:
            end = min(cls.count, cls.loaded + LINKS_PER_CALL)
            for k in range(cls.loaded, end):
                head = _read(cls.nodes_addr + k * NODE_SIZE + 0x28, 12)
                if head is None:
                    cls._fail("can't read the node links")
                    return False
                address, n = struct.unpack_from("<Qi", head, 0)
                raw = _read(address, n * 8) if n > 0 else b""
                if raw is None or n < 0 or n > 64:  # noqa: PLR2004
                    cls._fail("node links look wrong")
                    return False
                links = []
                for i in range(n):
                    offset = struct.unpack_from("<Q", raw, i * 8)[0] - cls.nodes_addr
                    target, rest = divmod(offset, NODE_SIZE)
                    if rest or not 0 <= target < cls.count:
                        cls._fail("a node link points outside the graph")
                        return False
                    links.append(target)
                cls.links.append(links)
            cls.loaded = end
            if end == cls.count:
                cls._build_grid()
            return False
        if time.monotonic() - cls.flags_at > FLAGS_REFRESH_S:
            cls._read_nodes(flags_only=True)
        return True

    @classmethod
    def preload(cls, pc: UObject, world_name: str) -> None:
        """
        Reads the graph ahead of need, a part per call. It was read only once a route was asked
        for: World_P's 11966 nodes take ~40 calls, so a pin set just before closing the map had no
        line for seconds (2026-10-29). Looks for a missing graph at most once a second.
        """
        if cls.broken:
            return
        if cls.world == world_name and cls.graph() is not None and cls.loaded >= cls.count:
            return
        now = time.monotonic()
        if cls.world == world_name and cls.graph() is None and now < cls.preload_at:
            return
        cls.preload_at = now + PRELOAD_RETRY_S
        cls._load(pc, world_name)

    @classmethod
    def _build_grid(cls) -> None:
        grid: dict[tuple[int, int], list[int]] = {}
        for k, (x, y, _) in enumerate(cls.pos):
            grid.setdefault((int(x // GRID), int(y // GRID)), []).append(k)
        cls.grid = grid

    @classmethod
    def _nearest(cls, x: float, y: float, z: float) -> int | None:
        """The nearest switched-on node: grid cells in growing rings until one has a node."""
        cx, cy = int(x // GRID), int(y // GRID)
        best, best_d = None, math.inf
        for ring in range(MAX_RINGS):
            # Only the ring's own cells (the whole square each ring was ~88k cells with nothing near)
            if ring == 0:
                cells = [(cx, cy)]
            else:
                cells = [(cx + i, cy + j) for i in range(-ring, ring + 1) for j in (-ring, ring)]
                cells += [(cx + i, cy + j) for i in (-ring, ring) for j in range(1 - ring, ring)]
            for cell in cells:
                for k in cls.grid.get(cell, ()):
                    if not cls.enabled[k]:
                        continue
                    nx, ny, nz = cls.pos[k]
                    d = (nx - x) ** 2 + (ny - y) ** 2 + ((nz - z) * 2) ** 2  # a floor up/down counts double
                    if d < best_d:
                        best, best_d = k, d
            # Anything in a further ring is at least `ring` cells away
            if best is not None and math.sqrt(best_d) <= ring * GRID:
                break
        return best

    @classmethod
    def _path(cls, start: int, goal: int) -> list[int]:
        """A* over the graph (straight-line distances)."""
        pos, links, enabled = cls.pos, cls.links, cls.enabled
        gx, gy, gz = pos[goal]
        came: dict[int, int] = {}
        cost = {start: 0.0}
        frontier = [(0.0, start)]
        while frontier:
            _, k = heapq.heappop(frontier)
            if k == goal:
                path = [k]
                while k in came:
                    k = came[k]
                    path.append(k)
                return path[::-1]
            kx, ky, kz = pos[k]
            for n in links[k]:
                if not enabled[n]:
                    continue
                nx, ny, nz = pos[n]
                c = cost[k] + math.dist((kx, ky, kz), (nx, ny, nz))
                if c < cost.get(n, math.inf):
                    cost[n] = c
                    came[n] = k
                    heapq.heappush(frontier, (c + math.dist((nx, ny, nz), (gx, gy, gz)), n))
        return []

    @classmethod
    def _node_at(cls, x: float, y: float) -> int | None:
        """The node a route point sits on (within `ON_NODE`), if any."""
        for k in cls.grid.get((int(x // GRID), int(y // GRID)), ()):
            nx, ny, _ = cls.pos[k]
            if abs(nx - x) <= ON_NODE and abs(ny - y) <= ON_NODE:
                return k
        return None

    @classmethod
    def reconnect(cls, me: tuple[float, float, float],
                  line: list[tuple[float, float]]) -> list[tuple[float, float]] | None:  # fmt: skip
        """
        `line` (world x, y, ending at the goal) from where you rejoin it: the way from you over the
        graph to the node of the line that makes the whole trip shortest (the way there + the rest
        of the line), then the line on from there. A small search around you instead of a whole new
        plan; the line ahead stays the map's own. None if the graph isn't read yet, you're off it,
        or no node of the line is within reach. The answer is kept until you're `RECONNECT_OFF`
        from it (or the line changes), looked for again at most every `RECONNECT_MIN_S`.
        """
        if cls.loaded < cls.count or not cls.grid or len(line) < 2:  # noqa: PLR2004
            return None
        now = time.monotonic()
        if line is cls.back_line and (now - cls.back_at < RECONNECT_MIN_S or (
            cls.back_way is not None and _off(cls.back_way[:BACK_CHECK_POINTS], me[0], me[1]) <= RECONNECT_OFF
        )):  # fmt: skip
            return cls.back_way
        cls.back_line, cls.back_at, cls.back_way = line, now, None
        start = cls._nearest(*me)
        if start is None or math.dist(cls.pos[start], me) > OFF_GRAPH:
            return None
        # The line's nodes, with the length of the line left after each
        left = [0.0] * len(line)
        for i in range(len(line) - 2, -1, -1):
            left[i] = left[i + 1] + math.dist(line[i], line[i + 1])
        on_line: dict[int, int] = {}  # node -> index of its point on the line
        for i, (x, y) in enumerate(line):
            if (x - me[0]) ** 2 + (y - me[1]) ** 2 > RECONNECT_RADIUS**2:
                continue
            k = cls._node_at(x, y)
            if k is not None and k not in on_line:
                on_line[k] = i
        if not on_line:
            return None
        # Dijkstra from you, bounded in reach and size
        pos, links, enabled = cls.pos, cls.links, cls.enabled
        cost, came = {start: 0.0}, {}
        frontier = [(0.0, start)]
        done = 0
        best, best_total = None, math.inf
        while frontier and done < RECONNECT_MAX_NODES:
            c, k = heapq.heappop(frontier)
            if c > cost.get(k, math.inf) or c >= best_total:
                continue
            done += 1
            if k in on_line:
                total = c + left[on_line[k]]
                if total < best_total:
                    best, best_total = k, total
            kx, ky, kz = pos[k]
            for n in links[k]:
                if not enabled[n]:
                    continue
                nx, ny, nz = pos[n]
                if (nx - me[0]) ** 2 + (ny - me[1]) ** 2 > RECONNECT_RADIUS**2:
                    continue
                nc = c + math.dist((kx, ky, kz), (nx, ny, nz))
                if nc < cost.get(n, math.inf):
                    cost[n] = nc
                    came[n] = k
                    heapq.heappush(frontier, (nc, n))
        if best is None:
            return None
        way = [best]
        while way[-1] in came:
            way.append(came[way[-1]])
        cls.back_way = [(pos[k][0], pos[k][1]) for k in reversed(way)] + line[on_line[best] + 1 :]
        return cls.back_way

    @classmethod
    def _off_route(cls, x: float, y: float) -> float:
        return _off(cls.route, x, y)


    @classmethod
    def update(cls, pc: UObject, world_name: str, me: tuple[float, float, float],
               goal: tuple[float, float, float] | None) -> list[tuple[float, float]] | None:  # fmt: skip
        """
        The route from `me` to `goal` (world x, y points); [] without a goal; None when the graph
        can't give one: no graph, no nodes near you (an interior), or no way there over it (it has
        separate parts). The caller falls back to the ECHO map's copy then. `reaches_goal` says
        whether the route's last node is near the goal.
        """
        if not cls._load(pc, world_name):
            return None
        if goal is None or not cls.pos:
            cls.route, cls.goal = [], None
            return cls.route
        now = time.monotonic()
        moved = cls.goal is None or math.dist(goal, cls.goal) > REPLAN_GOAL_MOVE
        if cls.route:
            strayed = cls._off_route(me[0], me[1]) > REPLAN_OFF_ROUTE
        else:
            # No route from where we last tried: try again once you've moved on (out of an interior)
            strayed = math.dist(me, cls.planned_from) > REPLAN_OFF_ROUTE
        if moved or (strayed and now - cls.planned_at >= REPLAN_MIN_S):
            cls.goal, cls.planned_at, cls.planned_from = goal, now, me
            cls.route, cls.reaches_goal = [], False
            start, end = cls._nearest(*me), cls._nearest(*goal)
            if start is not None and end is not None and math.dist(cls.pos[start], me) <= OFF_GRAPH:
                nodes = cls._path(start, end)
                route = [(cls.pos[k][0], cls.pos[k][1]) for k in nodes]
                cls.route = [*route, (goal[0], goal[1])] if route else []
                cls.reaches_goal = math.dist(cls.pos[end], goal) <= OFF_GRAPH
        return cls.route or None


def _off(line: list[tuple[float, float]], x: float, y: float) -> float:
    """How far (x, y) is from a polyline."""
    best = math.inf
    for (ax, ay), (bx, by) in zip(line, line[1:]):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        best = min(best, (ax + dx * t - x) ** 2 + (ay + dy * t - y) ** 2)
    return math.sqrt(best)
