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
GRID = 5000.0  # world units per cell of the nearest-node lookup grid
MAX_RINGS = 40  # cells around the start the nearest-node lookup searches (200 m)


class Breadcrumbs:
    graph = WeakPointer()
    graph_class: Any = None
    library: Any = None
    world = ""
    nodes_addr = 0
    count = 0
    pos: list[tuple[float, float, float]] = []  # noqa: RUF012
    enabled: list[bool] = []  # noqa: RUF012
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

    @classmethod
    def clear(cls) -> None:
        cls.graph = WeakPointer()
        cls.world = ""
        cls.count = cls.loaded = 0
        cls.pos, cls.enabled, cls.links, cls.grid = [], [], [], {}
        cls.route, cls.goal = [], None
        cls.flags_at = cls.planned_at = 0.0
        cls.reaches_goal = False

    @classmethod
    def _fail(cls, why: str) -> None:
        if not cls.broken:
            unrealsdk.logging.warning(f"[Minimap] live route off: {why}")
        cls.broken = why
        cls.route = []

    @classmethod
    def _read_nodes(cls) -> bool:
        """Positions and flags of every node (one read)."""
        blob = _read(cls.nodes_addr, cls.count * NODE_SIZE) if cls.count else b""
        if blob is None:
            return False
        pos, enabled = [], []
        for k in range(cls.count):
            x, y, z = struct.unpack_from("<3d", blob, k * NODE_SIZE + 8)
            if not all(v == v and abs(v) < 1e7 for v in (x, y, z)):  # noqa: PLR0124 - NaN check
                cls._fail("node positions look wrong")
                return False
            pos.append((x, y, z))
            enabled.append(blob[k * NODE_SIZE + 0x21] != 0)
        cls.pos, cls.enabled = pos, enabled
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
            cls._read_nodes()
        return True

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
    def _off_route(cls, x: float, y: float) -> float:
        best = math.inf
        for (ax, ay), (bx, by) in zip(cls.route, cls.route[1:]):
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
            best = min(best, (ax + dx * t - x) ** 2 + (ay + dy * t - y) ** 2)
        return math.sqrt(best)

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
