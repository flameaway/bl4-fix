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
RINGS = 12  # rings of places tried around an icon's true spot before it's left there
NEAR_RINGS = 3  # tried first, against only the icons near enough to reach them
_TOUCH = 0.999  # (two icons exactly touching don't overlap: rounding)


def layout(spots: list[tuple[float, float, float, str]]) -> tuple[list[tuple[float, float]], list[list[int]]]:
    """
    Spots (x, y, r, key): world point, radius no other icon may come into (world units), and a key
    that orders exact stacks the same way each time (the texture). Returns each spot's offset (world
    units; (0, 0): left where it is) and the groups of spots that overlapped (two or more each).

    Icons that overlap nothing stay put. Each group of icons that overlap (directly or through each
    other) is placed one by one, the one nearest the group's middle first: at its true spot if it's
    free, else at the nearest free place on rings around it, starting with the direction away from
    the group's middle. Neighbours are found on a grid: O(n) for the usual 5-40 icons.
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

    # Placed so far: the icons that overlap nothing first (they stay), then each group's
    placed = [spots[g[0]][:3] for g in by_root.values() if len(g) == 1]
    # (Orders that only depend on the spots, not the list's order)
    groups.sort(key=lambda g: min(spots[i][:2] for i in g))
    for g in groups:
        mx = sum(spots[i][0] for i in g) / len(g)
        my = sum(spots[i][1] for i in g) / len(g)
        g.sort(key=lambda i: ((spots[i][0] - mx) ** 2 + (spots[i][1] - my) ** 2, spots[i][0], spots[i][1], spots[i][3]))
        for i in g:
            x, y, r, _ = spots[i]
            near = _near(placed, x, y, r, NEAR_RINGS)
            if all(d2 >= q2 for d2, _, _, q2 in near):
                placed.append((x, y, r))
                continue
            ax, ay = x - mx, y - my
            # Away from the middle; from the middle itself, world -Y (about north)
            a0 = math.atan2(ay, ax) if ax * ax + ay * ay > (r * 0.01) ** 2 else -math.pi / 2
            # The near rings first, with only the icons that reach them (most find a place there)
            spot = _ring_spot(x, y, r, a0, near, 1, NEAR_RINGS) or _ring_spot(
                x, y, r, a0, _near(placed, x, y, r, RINGS), NEAR_RINGS + 1, RINGS)  # fmt: skip
            if spot is None:
                placed.append((x, y, r))  # (nowhere free near: it stays, overlapping)
                continue
            placed.append((spot[0], spot[1], r))
            offsets[i] = (spot[0] - x, spot[1] - y)
    return offsets, groups


# Ring k's directions (6 k of them), in the order they're tried: straight on, then out to both sides
_DIRS = [
    [(math.cos(a), math.sin(a)) for a in ((t + 1) // 2 * 2 * math.pi / (6 * k) * (1 if t % 2 else -1) for t in range(6 * k))]
    for k in range(1, RINGS + 1)
]  # fmt: skip


def _near(placed: list[tuple[float, float, float]], x: float, y: float, r: float,
          rings: int) -> list[tuple[float, float, float, float]]:  # fmt: skip
    """
    The placed icons that could touch a place on rings 1..`rings` around (x, y), nearest first
    (they block most places): (distance^2, x, y, (r + their r)^2).
    """
    reach = (rings + 1) * r
    return sorted(
        ((px - x) ** 2 + (py - y) ** 2, px, py, (r + pr) ** 2 * _TOUCH)
        for px, py, pr in placed
        if abs(px - x) < reach + pr and abs(py - y) < reach + pr
    )  # fmt: skip


def _ring_spot(x: float, y: float, r: float, a0: float, near: list[tuple[float, float, float, float]],
               first: int, last: int) -> tuple[float, float] | None:  # fmt: skip
    """The first free place on rings first..last (k r around (x, y)), each tried from the direction a0 out."""
    ca, sa = math.cos(a0), math.sin(a0)
    for k in range(first, last + 1):
        dist = k * r
        for ux, uy in _DIRS[k - 1]:
            cx, cy = x + dist * (ux * ca - uy * sa), y + dist * (ux * sa + uy * ca)
            for _, px, py, q2 in near:
                if (cx - px) ** 2 + (cy - py) ** 2 < q2:
                    break
            else:
                return cx, cy
    return None


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
