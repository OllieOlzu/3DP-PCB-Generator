"""
negative_stl.py
---------------
Reads border.json and shape.json, computes the boolean difference
(border MINUS shapes), triangulates the result, and writes a flat
2D binary STL file (negative.stl).

JSON format (both files):
  [
    [[x0,y0], [x1,y1], ...],   <- polygon 0 (vertices in order, auto-closed)
    [[x0,y0], [x1,y1], ...],   <- polygon 1
    ...
  ]

Dependencies:
  pip install shapely triangle numpy
"""

import json
import struct
import sys
from pathlib import Path

import numpy as np
import triangle as tr
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

# ── Configurable file paths ──────────────────────────────────────────────────
BORDER_FILE = "activefiles/border.json"
SHAPE_FILE  = "activefiles/topthickoutline.json"
OUTPUT_FILE = "activefiles/topnegthickoutline.stl"
# ─────────────────────────────────────────────────────────────────────────────


# ── I/O helpers ──────────────────────────────────────────────────────────────

def load_json_polygons(path: str) -> list[Polygon]:
    """Load a JSON polygon list and return valid Shapely Polygons."""
    with open(path) as fh:
        raw = json.load(fh)

    polys = []
    for shape in raw:
        pts = [tuple(v) for v in shape]
        # Ensure ring is closed (shapely accepts open rings too, but be safe)
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        poly = Polygon(pts)
        poly = make_valid(poly)          # repair any self-intersections
        if not poly.is_empty:
            polys.append(poly)
    return polys


def write_binary_stl(path: str, triangle_soup: list[np.ndarray]) -> None:
    """
    Write a binary STL file.

    triangle_soup : list of (3,3) float32 arrays, each row is a vertex [x,y,z]
    The flat normal [0,0,1] is used for all triangles.
    """
    n = len(triangle_soup)
    with open(path, "wb") as fh:
        # 80-byte header
        header = b"negative_stl - border minus shapes"
        fh.write(header.ljust(80, b"\x00"))
        # Triangle count (uint32)
        fh.write(struct.pack("<I", n))
        normal = struct.pack("<fff", 0.0, 0.0, 1.0)
        attr   = struct.pack("<H", 0)
        for tri in triangle_soup:
            fh.write(normal)
            for v in tri:
                fh.write(struct.pack("<fff", float(v[0]), float(v[1]), float(v[2])))
            fh.write(attr)
    print(f"  Wrote {n:,} triangles → {path}")


# ── Polygon → Constrained Delaunay Triangulation ─────────────────────────────

def _ring_verts_and_segs(ring, vert_list: list, seg_list: list) -> None:
    """
    Append the vertices of a ring to vert_list and the corresponding
    index-pair segments to seg_list.  Handles closed / open rings.
    """
    coords = list(ring.coords)
    if coords[0] == coords[-1]:          # drop duplicate closing point
        coords = coords[:-1]
    base = len(vert_list)
    n    = len(coords)
    vert_list.extend(c[:2] for c in coords)
    for i in range(n):
        seg_list.append((base + i, base + (i + 1) % n))


def triangulate_shapely_polygon(poly: Polygon) -> list[np.ndarray]:
    """
    Triangulate a (possibly holed) Shapely Polygon using the Triangle
    library (Shewchuk's CDT).  Returns a list of (3,3) float32 arrays
    (each row = one vertex in 3-D with z=0).
    """
    vertices: list[tuple] = []
    segments: list[tuple] = []
    holes:    list[tuple] = []

    # Exterior ring
    _ring_verts_and_segs(poly.exterior, vertices, segments)

    # Interior rings (holes)
    for interior in poly.interiors:
        _ring_verts_and_segs(interior, vertices, segments)
        # Place a seed point guaranteed to lie inside the hole
        hole_pt = Polygon(interior).representative_point()
        holes.append((hole_pt.x, hole_pt.y))

    tri_in: dict = {
        "vertices": np.array(vertices, dtype=np.float64),
        "segments": np.array(segments,  dtype=np.int32),
    }
    if holes:
        tri_in["holes"] = np.array(holes, dtype=np.float64)

    # 'p'  → PSLG triangulation (respects segments)
    # 'Q'  → quiet (suppress Triangle's stdout chatter)
    try:
        tri_out = tr.triangulate(tri_in, "pQ")
    except Exception as exc:
        print(f"  [warn] triangulation failed for a sub-polygon: {exc}", file=sys.stderr)
        return []

    verts2d = tri_out["vertices"]          # (N,2) float64
    faces   = tri_out["triangles"]         # (M,3) int

    tris = []
    for f in faces:
        v0 = np.array([verts2d[f[0]][0], verts2d[f[0]][1], 0.0], dtype=np.float32)
        v1 = np.array([verts2d[f[1]][0], verts2d[f[1]][1], 0.0], dtype=np.float32)
        v2 = np.array([verts2d[f[2]][0], verts2d[f[2]][1], 0.0], dtype=np.float32)
        # Ensure counter-clockwise winding (normal pointing +Z)
        edge1 = v1 - v0
        edge2 = v2 - v0
        cross_z = edge1[0] * edge2[1] - edge1[1] * edge2[0]
        if cross_z < 0:
            v1, v2 = v2, v1          # swap to flip winding
        tris.append(np.stack([v0, v1, v2]))
    return tris


def geometry_to_triangles(geom) -> list[np.ndarray]:
    """
    Accept any Shapely geometry (Polygon, MultiPolygon, GeometryCollection)
    and return a flat list of (3,3) triangle arrays.
    """
    tris = []
    geom_type = geom.geom_type

    if geom_type == "Polygon":
        if not geom.is_empty:
            tris.extend(triangulate_shapely_polygon(geom))

    elif geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            tris.extend(geometry_to_triangles(part))

    # Points / Lines from degenerate difference ops are silently skipped
    return tris


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    script_dir = Path(__file__).parent

    border_path = script_dir / BORDER_FILE
    shape_path  = script_dir / SHAPE_FILE
    output_path = script_dir / OUTPUT_FILE

    # ── Load ────────────────────────────────────────────────────────────────
    print(f"Loading border  : {border_path}")
    border_polys = load_json_polygons(border_path)
    print(f"  → {len(border_polys)} polygon(s)")

    print(f"Loading shapes  : {shape_path}")
    shape_polys  = load_json_polygons(shape_path)
    print(f"  → {len(shape_polys)} polygon(s)")

    # ── Boolean operations ──────────────────────────────────────────────────
    print("Computing border ∪ …")
    border_union = make_valid(unary_union(border_polys))

    print("Computing shapes ∪ …")
    shape_union  = make_valid(unary_union(shape_polys))

    print("Computing difference (border − shapes) …")
    result = make_valid(border_union.difference(shape_union))

    if result.is_empty:
        print("ERROR: difference is empty – shapes may fully cover the border.", file=sys.stderr)
        sys.exit(1)

    area_border = border_union.area
    area_shapes = shape_union.intersection(border_union).area
    area_result = result.area
    print(f"  Border area  : {area_border:.4f}")
    print(f"  Shapes area  : {area_shapes:.4f}  (clipped to border)")
    print(f"  Result area  : {area_result:.4f}  (should equal border − shapes)")

    # ── Triangulate ─────────────────────────────────────────────────────────
    print("Triangulating …")
    triangle_soup = geometry_to_triangles(result)

    if not triangle_soup:
        print("ERROR: triangulation produced no triangles.", file=sys.stderr)
        sys.exit(1)

    # ── Write STL ───────────────────────────────────────────────────────────
    print(f"Writing STL     : {output_path}")
    write_binary_stl(str(output_path), triangle_soup)
    print("Done.")


if __name__ == "__main__":
    main()
