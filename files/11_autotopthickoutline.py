import json
import math

with open("cache/config.json", "r") as file:
    data = json.load(file)

# ── Configuration ──────────────────────────────────────────────────────────────
OUTLINE_RADIUS = data["traceoutline"] + data["cutterwiggleroom"]          # Half-width of edge rectangles / radius of vertex circles
CIRCLE_VERTICES = data["circlevertices"]        # Polygon approximation resolution for each vertex cap
# ───────────────────────────────────────────────────────────────────────────────


def make_circle(cx: float, cy: float, radius: float, n: int = CIRCLE_VERTICES) -> list:
    """Return an n-vertex polygon approximating a circle centred on (cx, cy)."""
    return [
        [
            cx + radius * math.cos(2 * math.pi * i / n),
            cy + radius * math.sin(2 * math.pi * i / n),
        ]
        for i in range(n)
    ]


def make_rect(ax: float, ay: float, bx: float, by: float, half_width: float) -> list | None:
    """
    Return a 4-vertex rectangle centred on the segment A→B.

    The rectangle spans the full length of the segment and has a total
    width of 2 × half_width, laid out perpendicular to the segment direction.
    Vertices are ordered so the polygon winds consistently (CCW).
    """
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0:
        return None                         # Degenerate edge – skip silently

    # Unit perpendicular (rotated 90° CCW from the edge direction)
    nx = -dy / length
    ny =  dx / length

    ox, oy = nx * half_width, ny * half_width   # Offset vector

    return [
        [ax + ox, ay + oy],   # A-side, left
        [ax - ox, ay - oy],   # A-side, right
        [bx - ox, by - oy],   # B-side, right
        [bx + ox, by + oy],   # B-side, left
    ]


def outline_shapes(shapes: list, radius: float) -> list:
    """
    Convert a list of vertex-array shapes into an outlined equivalent.

    For every shape in *shapes* the function emits:
      • One circle polygon  per vertex   (round cap, fills corners evenly)
      • One rectangle polygon per edge   (fat line segment between two vertices)

    The combination produces the 'thick marker' effect: lines become filled
    bands of width 2 × radius and every join / endpoint is smoothly capped.
    """
    result = []

    for shape in shapes:
        n = len(shape)
        if n == 0:
            continue

        # ── 1. Vertex caps ────────────────────────────────────────────────────
        for vx, vy in shape:
            result.append(make_circle(vx, vy, radius))

        # ── 2. Edge rectangles ────────────────────────────────────────────────
        for i in range(n):
            ax, ay = shape[i]
            bx, by = shape[(i + 1) % n]    # Wraps last vertex back to first
            rect = make_rect(ax, ay, bx, by, radius)
            if rect is not None:
                result.append(rect)

    return result


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    INPUT_FILE  = "activefiles/combinedtop.json"
    OUTPUT_FILE = "activefiles/topthickoutline.json"

    with open(INPUT_FILE, "r") as f:
        shapes = json.load(f)

    print(f"Loaded {len(shapes)} shape(s) from '{INPUT_FILE}'.")

    outlined = outline_shapes(shapes, OUTLINE_RADIUS)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(outlined, f, indent=2)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_vertices = sum(len(s) for s in shapes)
    total_edges    = sum(len(s) for s in shapes)   # One edge per vertex (closed polygon)
    circles        = total_vertices
    rects          = total_edges

    print(
        f"Output  : {len(outlined)} shapes written to '{OUTPUT_FILE}'\n"
        f"  Circles (vertex caps) : {circles}\n"
        f"  Rectangles (edges)    : {rects}\n"
        f"  OUTLINE_RADIUS        : {OUTLINE_RADIUS}"
    )
