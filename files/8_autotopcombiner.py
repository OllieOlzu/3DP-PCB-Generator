#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║              Shape Combiner  –  Polygon Union            ║
║                                                          ║
║  Algorithm overview:                                     ║
║   1. Find all edge-edge intersection points              ║
║   2. Augment every polygon's vertex list with those pts  ║
║   3. Keep only edges whose midpoint is NOT strictly      ║
║      inside any other polygon  (= union boundary edges)  ║
║   4. Walk the directed boundary graph, choosing the      ║
║      most-CCW turn at every junction, to recover         ║
║      all closed output rings                             ║
╚══════════════════════════════════════════════════════════╝

Usage:
  python shape_combiner.py                   # interactive (prompts for files)
  python shape_combiner.py -i in.json        # explicit input
  python shape_combiner.py -i in.json -o out.json
  python shape_combiner.py --demo            # built-in square + triangle demo
"""

import json
import os
import math
from typing import List, Tuple, Optional, Dict, Set
from collections import defaultdict

# ─── Type aliases ────────────────────────────────────────────────────────────
Point   = Tuple[float, float]
Polygon = List[Point]
Edge    = Tuple[Point, Point]

# ─── Numerical tolerances ────────────────────────────────────────────────────
EPSILON     = 1e-9     # geometric comparisons
SNAP_DIGITS = 8        # decimal places when snapping coordinates


# ═════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL GEOMETRY
# ═════════════════════════════════════════════════════════════════════════════

def snap(p: Point) -> Point:
    """Round a point to SNAP_DIGITS decimals to absorb float drift."""
    return (round(p[0], SNAP_DIGITS), round(p[1], SNAP_DIGITS))


def dist_sq(a: Point, b: Point) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def midpoint(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def segment_intersection(
    a: Point, b: Point,
    c: Point, d: Point
) -> Optional[Tuple[Point, float, float]]:
    """
    Parametric intersection of segment AB and segment CD.

    Returns (intersection_point, t_ab, t_cd)  where t_ab ∈ [0,1] and
    t_cd ∈ [0,1], or None when the segments do not intersect.

    Endpoints touching (t = 0 or 1) are included so that T-junctions
    and corner touches are captured.
    """
    ax, ay = a;  bx, by = b
    cx, cy = c;  dx, dy = d

    d1x, d1y = bx - ax, by - ay   # direction AB
    d2x, d2y = dx - cx, dy - cy   # direction CD

    denom = d1x * d2y - d1y * d2x

    if abs(denom) < EPSILON:           # parallel / collinear – skip
        return None

    ex, ey = cx - ax, cy - ay         # vector A→C
    t = (ex * d2y - ey * d2x) / denom
    s = (ex * d1y - ey * d1x) / denom

    if -EPSILON <= t <= 1.0 + EPSILON and -EPSILON <= s <= 1.0 + EPSILON:
        t = max(0.0, min(1.0, t))
        s = max(0.0, min(1.0, s))
        ix = ax + t * d1x
        iy = ay + t * d1y
        return snap((ix, iy)), t, s

    return None


def winding_number(pt: Point, poly: Polygon) -> int:
    """
    Winding-number point-in-polygon test (handles non-convex shapes).
    Returns 0 when outside, ≠ 0 when inside, ±½ on an edge (rare float case).
    """
    x, y   = pt
    winding = 0
    n       = len(poly)

    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]

        if y1 <= y:
            if y2 > y:                              # upward crossing
                cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
                if cross > EPSILON:
                    winding += 1
        else:
            if y2 <= y:                             # downward crossing
                cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
                if cross < -EPSILON:
                    winding -= 1

    return winding


def point_on_segment(pt: Point, a: Point, b: Point, eps: float = 1e-7) -> bool:
    """True iff pt lies on the closed segment [a, b]."""
    ax, ay = a;  bx, by = b;  x, y = pt
    # 1. Collinearity check via cross product
    cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
    if abs(cross) > eps * max(1.0, abs(bx - ax), abs(by - ay)):
        return False
    # 2. Point must lie within the bounding box of the segment
    if not (min(ax, bx) - eps <= x <= max(ax, bx) + eps):
        return False
    if not (min(ay, by) - eps <= y <= max(ay, by) + eps):
        return False
    return True


def on_polygon_boundary(pt: Point, poly: Polygon) -> bool:
    """True iff pt lies on any edge of poly."""
    n = len(poly)
    for i in range(n):
        if point_on_segment(pt, poly[i], poly[(i + 1) % n]):
            return True
    return False


def strictly_inside(pt: Point, poly: Polygon) -> bool:
    """True iff pt is strictly inside poly (not on its boundary)."""
    if on_polygon_boundary(pt, poly):
        return False
    return winding_number(pt, poly) != 0


# ═════════════════════════════════════════════════════════════════════════════
#  STEP 1 – AUGMENT POLYGONS WITH INTERSECTION VERTICES
# ═════════════════════════════════════════════════════════════════════════════

def augment_polygon(poly_idx: int, polygons: List[Polygon]) -> Polygon:
    """
    Walk every edge of polygons[poly_idx].  For each edge A→B, collect all
    intersection points with edges of every *other* polygon, sort them by
    their parameter t along A→B, and splice them in.

    This ensures that every crossing becomes a proper shared vertex in at
    least one polygon's vertex list.
    """
    poly   = polygons[poly_idx]
    n      = len(poly)
    result: List[Point] = []

    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]

        crossings: List[Tuple[float, Point]] = []

        for j, other in enumerate(polygons):
            if j == poly_idx:
                continue
            m = len(other)
            for k in range(m):
                c = other[k]
                d = other[(k + 1) % m]
                hit = segment_intersection(a, b, c, d)
                if hit is not None:
                    pt, t, _s = hit
                    # Ignore endpoint touches – they're already vertices
                    if EPSILON < t < 1.0 - EPSILON:
                        crossings.append((t, pt))

        # Sort along AB, deduplicate near-coincident points
        crossings.sort(key=lambda x: x[0])

        result.append(snap(a))
        prev_pt = snap(a)

        for _t, pt in crossings:
            if dist_sq(pt, prev_pt) > EPSILON ** 2:
                result.append(pt)
                prev_pt = pt

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  STEP 2 – CLASSIFY EDGES (BOUNDARY vs INTERIOR)
# ═════════════════════════════════════════════════════════════════════════════

def is_boundary_edge(
    a: Point, b: Point,
    own_idx: int,
    augmented: List[Polygon]
) -> bool:
    """
    An edge A→B from polygon own_idx lies on the union boundary iff its
    midpoint is NOT strictly inside any other polygon.
    """
    mid = midpoint(a, b)
    for j, poly in enumerate(augmented):
        if j == own_idx:
            continue
        if strictly_inside(mid, poly):
            return False
    return True


def collect_boundary_edges(augmented: List[Polygon]) -> List[Edge]:
    """
    Return all directed edges that form the outer boundary of the union.

    Deduplication rules applied after classification:
    - Same-direction duplicate edges → keep exactly one copy.
    - Opposite-direction edge pairs (A→B and B→A both present) → cancel both
      (they represent an interior shared segment between two polygons).
    """
    raw: List[Edge] = []
    for i, poly in enumerate(augmented):
        n = len(poly)
        for k in range(n):
            a = poly[k]
            b = poly[(k + 1) % n]
            if a != b and is_boundary_edge(a, b, i, augmented):
                raw.append((a, b))

    # Deduplicate same-direction edges
    seen_forward: dict = {}
    for a, b in raw:
        key = (snap(a), snap(b))
        seen_forward[key] = (a, b)          # last write wins – coords are the same

    # Cancel opposite-direction pairs
    result: List[Edge] = []
    for (sa, sb), (a, b) in seen_forward.items():
        if (sb, sa) not in seen_forward:    # no reverse edge → keep
            result.append((a, b))

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  STEP 3 – ASSEMBLE BOUNDARY EDGES INTO CLOSED RINGS
# ═════════════════════════════════════════════════════════════════════════════

def _signed_turn(prev: Point, curr: Point, nxt: Point) -> float:
    """
    Signed angle from direction (prev→curr) to direction (curr→nxt).
    Positive = CCW (left turn), Negative = CW (right turn).
    """
    in_a  = math.atan2(curr[1] - prev[1], curr[0] - prev[0])
    out_a = math.atan2(nxt[1]  - curr[1], nxt[0]  - curr[0])
    diff  = out_a - in_a
    # Normalise to (−π, π]
    while diff >  math.pi: diff -= 2.0 * math.pi
    while diff <= -math.pi: diff += 2.0 * math.pi
    return diff


def edges_to_polygons(edges: List[Edge]) -> List[List[List[float]]]:
    """
    Walk the directed graph of boundary edges to recover closed rings.

    At every junction (vertex with > 1 outgoing edge), we choose the
    *most-CCW* (most-left) turn relative to the incoming direction.
    This reliably traces outer boundaries in CCW order.

    Edges used by one ring are marked so subsequent walks only pick up
    remaining (disjoint-component) edges.
    """
    if not edges:
        return []

    # Build adjacency: snapped-vertex → [snapped destinations]
    adj: Dict[Point, List[Point]]  = defaultdict(list)
    coord: Dict[Point, Point]      = {}   # key → actual float coords

    for a, b in edges:
        sa, sb = snap(a), snap(b)
        adj[sa].append(sb)
        coord[sa] = a
        coord[sb] = b

    used:     Set[Tuple[Point, Point]] = set()
    polygons: List[List[List[float]]]  = []

    def walk(start: Point, first_next: Point) -> Optional[List[Point]]:
        """Try to build one closed ring starting with edge start→first_next."""
        if (start, first_next) in used:
            return None

        path   = [start]
        prev   = start
        cur    = start
        nxt    = first_next

        for _ in range(len(edges) * 2 + 4):
            edge_key = (cur, nxt)
            if edge_key in used:
                return None          # edge already claimed

            used.add(edge_key)
            path.append(nxt)
            prev, cur = cur, nxt

            if cur == start:
                # Closed ring found – path[-1] == start, so strip it
                ring = path[:-1]
                return ring if len(ring) >= 3 else None

            # Choose next step
            candidates = [v for v in adj[cur] if (cur, v) not in used]
            if not candidates:
                return None          # dead end

            if len(candidates) == 1:
                nxt = candidates[0]
            else:
                # Most-CCW turn = largest signed angle
                prev_coord = coord[prev]
                cur_coord  = coord[cur]
                candidates.sort(
                    key=lambda v: _signed_turn(prev_coord, cur_coord, coord[v]),
                    reverse=True
                )
                nxt = candidates[0]

        return None   # exceeded max steps

    # Attempt a ring from every unvisited outgoing edge
    for a, b in list(edges):
        sa, sb = snap(a), snap(b)
        ring_keys = walk(sa, sb)
        if ring_keys and len(ring_keys) >= 3:
            # Deduplicate consecutive equal vertices
            clean: List[Point] = [ring_keys[0]]
            for p in ring_keys[1:]:
                if p != clean[-1]:
                    clean.append(p)
            if clean[0] == clean[-1]:
                clean = clean[:-1]
            if len(clean) >= 3:
                polygons.append([[coord[k][0], coord[k][1]] for k in clean])

    return polygons


# ═════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def polygon_area(poly) -> float:
    """Shoelace formula.  Positive ⟹ CCW, Negative ⟹ CW."""
    n    = len(poly)
    area = 0.0
    for i in range(n):
        j     = (i + 1) % n
        xi, yi = (poly[i][0], poly[i][1])
        xj, yj = (poly[j][0], poly[j][1])
        area  += xi * yj - xj * yi
    return area / 2.0


def ensure_ccw(poly: List[List[float]]) -> List[List[float]]:
    """Return the polygon with vertices in CCW order."""
    if polygon_area(poly) < 0:
        return poly[::-1]
    return poly


def clean_polygon(poly: List[List[float]]) -> List[List[float]]:
    """Remove consecutive duplicate vertices and near-collinear spurs."""
    n      = len(poly)
    result = []
    for i in range(n):
        a = poly[(i - 1) % n]
        b = poly[i]
        c = poly[(i + 1) % n]
        # Skip if b ≈ a
        if dist_sq(tuple(a), tuple(b)) < EPSILON ** 2:
            continue
        # Skip if b is collinear with a and c (area of micro-triangle ~ 0)
        cross = (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
        if abs(cross) < 1e-10:
            continue
        result.append(b)
    return result


def fallback_largest(polygons: List[Polygon]) -> List[List[List[float]]]:
    """Return the polygon with the greatest absolute area (used as fallback)."""
    areas   = [abs(polygon_area(p)) for p in polygons]
    largest = polygons[areas.index(max(areas))]
    return [[[p[0], p[1]] for p in largest]]


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def combine_polygons(raw_polygons: List[List[List[float]]]) -> List[List[List[float]]]:
    """
    Compute the union of all input polygons.

    Parameters
    ----------
    raw_polygons : list of polygons, each a list of [x, y] pairs.

    Returns
    -------
    list of polygons representing the union, in the same [[x,y],…] format.
    """
    if not raw_polygons:
        return []
    if len(raw_polygons) == 1:
        return [raw_polygons[0]]

    # Convert to tuples and discard degenerate inputs
    polygons: List[Polygon] = [
        [(v[0], v[1]) for v in p]
        for p in raw_polygons
        if len(p) >= 3
    ]
    if not polygons:
        return []
    if len(polygons) == 1:
        return [raw_polygons[0]]

    # ── Step 1: augment each polygon with intersection vertices ──────────────
    augmented: List[Polygon] = [
        augment_polygon(i, polygons) for i in range(len(polygons))
    ]

    # ── Step 2: collect union-boundary edges ─────────────────────────────────
    boundary_edges = collect_boundary_edges(augmented)

    if not boundary_edges:
        # All shapes are mutually contained – return the largest
        return fallback_largest(polygons)

    # ── Step 3: assemble edges → rings ───────────────────────────────────────
    rings = edges_to_polygons(boundary_edges)

    if not rings:
        return fallback_largest(polygons)

    # ── Step 4: clean up & normalise winding ─────────────────────────────────
    result = []
    for ring in rings:
        cleaned = clean_polygon(ring)
        if len(cleaned) >= 3:
            result.append(ensure_ccw(cleaned))

    return result if result else fallback_largest(polygons)


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    script_dir   = os.path.dirname(os.path.abspath(__file__))
    input_path   = os.path.join(script_dir, "activefiles/topcopper.json")
    output_path  = os.path.join(script_dir, "activefiles/combinedtop.json")

    with open(input_path, 'r', encoding='utf-8') as fh:
        shapes = json.load(fh)

    result = combine_polygons(shapes)

    def fmt(v):
        return round(v, 8) if isinstance(v, float) else v

    cleaned = [[[fmt(c) for c in pt] for pt in poly] for poly in result]

    with open(output_path, 'w', encoding='utf-8') as fh:
        json.dump(cleaned, fh, indent=2)

    print(f"Combined {len(shapes)} polygon(s) → {len(result)} polygon(s)  →  {output_path}")
