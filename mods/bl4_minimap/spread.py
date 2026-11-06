"""
"Spread out stacked icons": icons that would overlap are moved apart, each moved one with a thin
line back to its true spot, like the ECHO map does when you hover a stack. ui/minimap.js mirrors
this (spreadLayout, spreadFit): both modes lay out the same way.

The layout is in world units, so it doesn't change as you turn or walk: overlap only depends on
the distances between icons, which neither changes. An icon's moved place is its world point plus
an offset, projected like any other point, so a spread stack turns with the map as one piece
instead of being laid out again (and jumping) every frame. It's made again only when the icons or
the zoom change. Pure Python (no game calls).
"""

from __future__ import annotations

import math

# The part of an icon's size another icon may not come into (the art has a clear margin)
FOOTPRINT = 0.85
PASSES = 40  # push-apart passes at most (a pair is apart after one; a crowded stack takes more)
_TOUCH = 0.999  # (two icons exactly touching don't overlap: rounding)
_GOLDEN = math.pi * (3 - math.sqrt(5))  # (exact stacks open up in directions this far apart)


def layout(spots: list[tuple[float, float, float, str]]) -> tuple[list[tuple[float, float]], list[list[int]]]:
    """
    Spots (x, y, r, key): world point, radius no other icon may come into (world units), and a key
    that orders exact stacks the same way each time (the texture). Returns each spot's offset (world
    units; (0, 0): left where it is) and the groups of spots that overlapped (two or more each).

    Icons that overlap nothing stay put. The icons of the groups that overlap (directly or through
    each other) are pushed apart: both of an overlapping pair along the line between them, by half
    the overlap each (one that stays pushes the other the whole way), pass after pass until none
    overlap. Each ends up just clear of its neighbours, on the same side of them as in the world:
    two vending machines side by side stay side by side, each moved half the way (until 2026-11-05
    one stayed and the other went out on rings an icon apart, ending up behind it). Exact stacks
    start a hair apart in fixed directions, so they open up the same way each time. Neighbours are
    found on a grid: O(n) a pass for the usual 5-40 icons.
    """
    n = len(spots)
    offsets = [(0.0, 0.0)] * n
    rmax = max((s[2] for s in spots), default=0.0)
    if n < 2 or rmax <= 0:  # noqa: PLR2004
        return offsets, []
    cell = 2 * rmax  # two icons that touch are at most a cell apart
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (x, y, _, _) in enumerate(spots):
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(i)
    parent = list(range(n))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (gx, gy), members in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    xj, yj, rj, _ = spots[j]
                    for i in members:
                        if j > i:
                            xi, yi, ri, _ = spots[i]
                            if (xi - xj) ** 2 + (yi - yj) ** 2 < (ri + rj) ** 2 * _TOUCH:
                                parent[root(j)] = root(i)
    by_root: dict[int, list[int]] = {}
    for i in range(n):
        by_root.setdefault(root(i), []).append(i)
    groups = [g for g in by_root.values() if len(g) > 1]
    if not groups:
        return offsets, []

    # The icons that overlap nothing stay; the others move, in an order that only depends on the
    # spots, not the list's order. The pairs that can meet are found once, on a grid twice as
    # coarse (all pairs in neighbouring cells: pushed apart, an icon moves an icon or two)
    moving = sorted((i for g in groups for i in g), key=lambda i: (spots[i][0], spots[i][1], spots[i][3]))
    is_moving = [False] * n
    for i in moving:
        is_moving[i] = True
    pos = [[x, y] for x, y, _, _ in spots]
    stacked: dict[tuple[float, float], int] = {}
    for i in moving:
        x, y, r, _ = spots[i]
        k = stacked.get((x, y), 0)
        stacked[(x, y)] = k + 1
        if k:  # (the first of an exact stack opens up north-ish, the next ones around it)
            a = -math.pi / 2 + (k - 1) * _GOLDEN
            pos[i][0] += math.cos(a) * r * 0.01
            pos[i][1] += math.sin(a) * r * 0.01
    wide = 2 * cell
    coarse: dict[tuple[int, int], list[int]] = {}
    for i, (x, y, _, _) in enumerate(spots):
        coarse.setdefault((math.floor(x / wide), math.floor(y / wide)), []).append(i)
    pairs = []  # (p, q, need, need^2 less rounding, p's share): a moving icon and one it can meet (a still one: share 1)
    rank = {i: k for k, i in enumerate(moving)}
    for i in moving:
        x, y, ri, _ = spots[i]
        gx, gy = math.floor(x / wide), math.floor(y / wide)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in coarse.get((gx + dx, gy + dy), ()):
                    if j == i or (is_moving[j] and rank[j] < rank[i]):
                        continue
                    need = ri + spots[j][2]
                    pairs.append((pos[i], pos[j], need, need * need * _TOUCH, 0.5 if is_moving[j] else 1.0))
    # Each pass: p and q of an overlapping pair apart, p by its share of the overlap, q by the rest
    for _ in range(PASSES):
        clear = True
        for p, q, need, near2, share in pairs:
            ex, ey = p[0] - q[0], p[1] - q[1]
            d2 = ex * ex + ey * ey
            if d2 >= near2:
                continue
            clear = False
            d = math.sqrt(d2)
            if d <= need * 1e-6:  # (on the same spot: apart north-south)
                ex, ey, d = 0.0, -1.0, 1.0
            over = (need - d) / d
            p[0] += ex * over * share
            p[1] += ey * over * share
            q[0] -= ex * over * (1 - share)
            q[1] -= ey * over * (1 - share)
        if clear:
            break
    for i in moving:
        offsets[i] = (pos[i][0] - spots[i][0], pos[i][1] - spots[i][1])
    return offsets, groups


def fit(points: list[tuple[float, float, float]], *, square: bool) -> tuple[float, float]:
    """
    The shift (x, y) that brings a spread group's icons (x, y, edge: how far from the middle each
    may be) inside the minimap all together, so they stay apart (pulled in one by one, those at the
    rim landed on each other). For a single icon it's the usual pull-in.
    """
    if square:
        shift = []
        for axis in (0, 1):
            lo = max(-p[2] - p[axis] for p in points)
            hi = min(p[2] - p[axis] for p in points)
            shift.append(min(max(0.0, lo), hi) if lo <= hi else (lo + hi) / 2)
        return shift[0], shift[1]
    tx = ty = 0.0
    for _ in range(4):
        worst, wx, wy = 0.0, 0.0, 0.0
        for px, py, edge in points:
            x, y = px + tx, py + ty
            d = math.hypot(x, y)
            if d - edge > worst:
                worst, wx, wy = d - edge, x / d, y / d
        if worst <= 0:
            break
        tx, ty = tx - worst * wx, ty - worst * wy
    return tx, ty
