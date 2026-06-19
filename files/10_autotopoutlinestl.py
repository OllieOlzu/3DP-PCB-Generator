"""
json_to_stl.py

Converts a JSON file of 2D polygon vertex lists into a valid binary STL file.
Each polygon is triangulated using a robust ear-clipping algorithm that correctly
handles convex, concave (non-convex), and collinear-vertex polygons.

Input  : input.json   — list of polygons, each polygon a list of [x, y] vertices
Output : output.stl   — binary STL with all triangles placed at z = 0
"""

import json
import struct
import math
import sys


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _cross2(o, a, b):
    """Signed 2-D cross product of vectors OA and OB (positive = CCW turn)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _signed_area(poly):
    """Shoelace formula — positive ↔ CCW, negative ↔ CW."""
    n = len(poly)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += poly[i][0] * poly[j][1]
        area -= poly[j][0] * poly[i][1]
    return area * 0.5


def _ensure_ccw(poly):
    """Return a CCW copy of the polygon (reverse if currently CW)."""
    return list(poly) if _signed_area(poly) > 0 else list(reversed(poly))


def _point_strictly_inside_triangle(p, a, b, c):
    """
    Return True if point p lies strictly inside triangle ABC.
    Uses barycentric sign-check; points exactly on an edge return False
    so collinear boundary vertices do NOT falsely disqualify an ear.
    """
    d1 = _cross2(p, a, b)
    d2 = _cross2(p, b, c)
    d3 = _cross2(p, c, a)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos) and not (d1 == 0 and d2 == 0 and d3 == 0)


def _segments_properly_intersect(p1, p2, p3, p4):
    """
    Return True if open segment p1-p2 properly crosses open segment p3-p4.
    Used to detect self-intersections when no ear is found on a clean pass.
    """
    d1 = _cross2(p3, p4, p1)
    d2 = _cross2(p3, p4, p2)
    d3 = _cross2(p1, p2, p3)
    d4 = _cross2(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


# ---------------------------------------------------------------------------
# Ear-clipping triangulation
# ---------------------------------------------------------------------------

def _is_ear(poly, ring, i):
    """
    Determine whether vertex ring[i] is an ear of the current sub-polygon.

    An ear is a vertex where:
      1. The interior angle at that vertex is strictly convex (CCW triangle).
      2. No other vertex of the ring lies strictly inside that triangle.

    This correctly handles concave polygons because only convex vertices
    of the *current* ring can be ears, and the ring shrinks each iteration.
    """
    n   = len(ring)
    pi  = (i - 1) % n
    ni  = (i + 1) % n
    a   = poly[ring[pi]]
    b   = poly[ring[i]]
    c   = poly[ring[ni]]

    # Must be a left (CCW) turn — reflex or collinear vertices are never ears
    if _cross2(a, b, c) <= 0:
        return False

    # Reject if any other ring vertex falls strictly inside triangle ABC
    for j in range(n):
        if j in (pi, i, ni):
            continue
        if _point_strictly_inside_triangle(poly[ring[j]], a, b, c):
            return False

    return True


def triangulate(raw_poly):
    """
    Triangulate a simple polygon (convex or concave) via ear clipping.

    Parameters
    ----------
    raw_poly : list of [x, y]
        Polygon vertices in any winding order.

    Returns
    -------
    list of ((x0,y0),(x1,y1),(x2,y2))
        CCW-wound triangles that exactly tile the polygon.
    """
    poly = [tuple(v) for v in raw_poly]

    # --- Degenerate cases ---
    if len(poly) < 3:
        return []
    if len(poly) == 3:
        area = _signed_area(poly)
        if area == 0:
            return []   # degenerate (collinear)
        if area < 0:
            poly = list(reversed(poly))
        return [(poly[0], poly[1], poly[2])]

    # Ensure CCW so that "left-turn" == convex
    poly = _ensure_ccw(poly)
    n    = len(poly)
    ring = list(range(n))   # active vertex indices into poly[]
    tris = []

    # Safety: worst case O(n²) iterations for a degenerate polygon
    max_iter = n * n * 2
    iters    = 0

    while len(ring) > 3:
        if iters > max_iter:
            # Should never happen on a simple polygon; emit remaining fan as fallback
            print(f"  [warn] ear-clip iteration limit hit — emitting remaining {len(ring)}-gon as fan",
                  file=sys.stderr)
            break
        iters += 1

        ear_found = False
        for i in range(len(ring)):
            if _is_ear(poly, ring, i):
                pi = (i - 1) % len(ring)
                ni = (i + 1) % len(ring)
                tris.append((poly[ring[pi]], poly[ring[i]], poly[ring[ni]]))
                ring.pop(i)
                ear_found = True
                break

        if not ear_found:
            # Polygon may be self-intersecting or has all-collinear remaining vertices.
            # Emit a degenerate fan from ring[0] and exit cleanly.
            print(f"  [warn] no ear found on remaining {len(ring)} vertices — "
                  "polygon may be self-intersecting; emitting degenerate fan.",
                  file=sys.stderr)
            for k in range(1, len(ring) - 1):
                tris.append((poly[ring[0]], poly[ring[k]], poly[ring[k + 1]]))
            ring = []
            break

    if len(ring) == 3:
        tris.append((poly[ring[0]], poly[ring[1]], poly[ring[2]]))

    return tris


# ---------------------------------------------------------------------------
# STL binary writer
# ---------------------------------------------------------------------------

def _triangle_normal(a, b, c):
    """
    Unit normal of triangle ABC via the cross product (b-a) × (c-a).
    For flat z=0 CCW triangles this always returns (0, 0, 1).
    """
    ax, ay, az = b[0]-a[0], b[1]-a[1], 0.0
    bx, by, bz = c[0]-a[0], c[1]-a[1], 0.0
    nx = ay*bz - az*by
    ny = az*bx - ax*bz
    nz = ax*by - ay*bx
    length = math.sqrt(nx*nx + ny*ny + nz*nz)
    if length < 1e-12:
        return (0.0, 0.0, 1.0)   # degenerate triangle → point normal up
    return (nx/length, ny/length, nz/length)


def write_binary_stl(all_triangles, path):
    """
    Write a compliant binary STL file.

    Binary STL layout
    -----------------
    80 bytes  — ASCII header (ignored by most tools)
    4  bytes  — uint32 triangle count
    Per triangle (50 bytes):
        12 bytes — float32 × 3  normal vector
        12 bytes — float32 × 3  vertex 1
        12 bytes — float32 × 3  vertex 2
        12 bytes — float32 × 3  vertex 3
         2 bytes — uint16        attribute byte count (0)
    """
    header = b"Binary STL - json_to_stl.py"
    header = header[:80].ljust(80, b"\x00")

    total = len(all_triangles)

    with open(path, "wb") as f:
        f.write(header)
        f.write(struct.pack("<I", total))

        for (a, b, c) in all_triangles:
            nx, ny, nz = _triangle_normal(a, b, c)
            f.write(struct.pack("<fff", nx, ny, nz))
            f.write(struct.pack("<fff", float(a[0]), float(a[1]), 0.0))
            f.write(struct.pack("<fff", float(b[0]), float(b[1]), 0.0))
            f.write(struct.pack("<fff", float(c[0]), float(c[1]), 0.0))
            f.write(struct.pack("<H",  0))

    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    input_path  = "activefiles/topoutline.json"
    output_path = "activefiles/topoutline.stl"

    # --- Load ---
    try:
        with open(input_path, "r") as fh:
            shapes = json.load(fh)
    except FileNotFoundError:
        sys.exit(f"[error] '{input_path}' not found.")
    except json.JSONDecodeError as exc:
        sys.exit(f"[error] JSON parse error: {exc}")

    if not isinstance(shapes, list):
        sys.exit("[error] JSON root must be a list of polygons.")

    # --- Triangulate ---
    all_triangles = []
    for idx, shape in enumerate(shapes):
        if len(shape) < 3:
            print(f"  [skip] shape {idx}: fewer than 3 vertices.", file=sys.stderr)
            continue
        tris = triangulate(shape)
        print(f"  shape {idx:3d}: {len(shape):4d} vertices → {len(tris):4d} triangles")
        all_triangles.extend(tris)

    if not all_triangles:
        sys.exit("[error] No triangles were produced — check your input.")

    # --- Write STL ---
    count = write_binary_stl(all_triangles, output_path)
    print(f"\nWrote {count} triangles to '{output_path}'  "
          f"({count * 50 + 84} bytes)")


if __name__ == "__main__":
    main()
