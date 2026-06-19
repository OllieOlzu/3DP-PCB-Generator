#!/usr/bin/env python3
"""
border2json.py
──────────────
Reads a PCB board-outline Gerber file (Edge.Cuts / .gko / .gm1 / etc.)
and outputs border.json – a single closed polygon with vertices only at
true corners (collinear intermediate points removed; arcs are tessellated
so their curvature is preserved).

Output:  [ [[x0,y0], [x1,y1], ...] ]   (one shape, one polygon)

Writes error.txt (beside this script) if:
  • Multiple disconnected shapes are detected
  • No geometry is found
  • The resulting polygon is degenerate

Usage:
    python border2json.py           # opens file-picker dialog
    python border2json.py --test    # runs built-in self-tests
"""

import json
import math
import os
import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox


# ── Configuration ─────────────────────────────────────────────────────────────

SNAP_TOL      = 0.005   # endpoints within this distance are "touching"
MAX_BRIDGE    = 1.0     # auto-bridge gaps up to this; larger → separate shape → error
COLLINEAR_DEG = 0.5     # direction-change below this (°) is treated as straight
ARC_SEGS      = 32      # tessellation samples per arc segment

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()


# ── Geometry helpers ───────────────────────────────────────────────────────────

def dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def arc_tessellate(cx, cy, r, a_start, a_end, clockwise, n=ARC_SEGS):
    """
    Return n+1 points along an arc (inclusive of both endpoints).
    Handles full-circle and near-zero-span arcs gracefully.
    """
    if clockwise:
        while a_end > a_start:              a_end -= 2 * math.pi
        while a_end < a_start - 2*math.pi: a_end += 2 * math.pi
    else:
        while a_end < a_start:              a_end += 2 * math.pi
        while a_end > a_start + 2*math.pi: a_end -= 2 * math.pi

    span = a_end - a_start
    if abs(span) < 1e-12:
        return [(cx + r * math.cos(a_start), cy + r * math.sin(a_start))]
    return [
        (cx + r * math.cos(a_start + span * t / n),
         cy + r * math.sin(a_start + span * t / n))
        for t in range(n + 1)
    ]


# ── Segment ────────────────────────────────────────────────────────────────────

class Seg:
    """A directed sequence of ≥ 2 (x, y) points (a line or tessellated arc)."""
    __slots__ = ('pts',)

    def __init__(self, pts):
        self.pts = list(pts)

    @property
    def start(self):
        return self.pts[0]

    @property
    def end(self):
        return self.pts[-1]

    def is_degenerate(self):
        """True for a segment with no real length (single point or two identical points).
        Multi-point arcs where start == end (full circles) are NOT degenerate."""
        if len(self.pts) < 2:
            return True
        if len(self.pts) == 2 and dist(self.pts[0], self.pts[1]) < 1e-9:
            return True
        return False

    def reversed(self):
        return Seg(list(reversed(self.pts)))


# ── Gerber border parser ───────────────────────────────────────────────────────

class BorderParser:
    """
    Minimal RS-274X parser that extracts only draw / arc / region geometry
    as a list of Seg objects.  Aperture flashes (D03) are ignored – they
    are pads, not outline traces.
    """

    def __init__(self):
        self._segs      = []
        self.coord_fmt  = None          # (int_digits, frac_digits, leading_omit)
        self.cur_x      = 0.0
        self.cur_y      = 0.0
        self.cur_ap     = None
        self.apertures  = {}
        self.interp     = 'linear'      # 'linear' | 'cw_arc' | 'ccw_arc'
        self.in_region  = False
        self._reg_pts   = []            # points collected for current region contour
        self._reg_segs  = []            # finished sub-contours in current G36/G37

    # ── coordinate decode ────────────────────────────────────────────

    def _decode(self, s):
        if not s:
            return 0.0
        s = s.strip()
        if '.' in s:
            return float(s)
        sign = 1
        if s[0] == '-':
            sign = -1; s = s[1:]
        elif s[0] == '+':
            s = s[1:]
        if not s:
            return 0.0
        _, frac, leading_omit = self.coord_fmt
        if leading_omit:
            return sign * int(s) / 10 ** frac
        total = self.coord_fmt[0] + frac
        return sign * int(s.ljust(total, '0')) / 10 ** frac

    # ── tokeniser ────────────────────────────────────────────────────

    def _tokenize(self, src):
        out, i, n = [], 0, len(src)
        while i < n:
            c = src[i]
            if c == '%':
                j = src.find('%', i + 1)
                if j == -1: break
                out.append(('E', src[i+1:j]))
                i = j + 1
            elif c in ' \t\r\n*':
                i += 1
            else:
                j = i
                while j < n and src[j] not in ('*', '%'):
                    j += 1
                w = src[i:j].strip()
                if w:
                    out.append(('W', w))
                i = j + 1 if j < n else j
        return out

    # ── main parse entry ─────────────────────────────────────────────

    def parse(self, filename):
        with open(filename, 'r', errors='replace') as fh:
            src = fh.read()
        for kind, val in self._tokenize(src):
            try:
                (self._ext if kind == 'E' else self._word)(val)
            except Exception:
                pass   # be lenient with malformed commands
        return self._segs

    # ── extended command handler ──────────────────────────────────────

    def _ext(self, raw):
        words = [w.strip() for w in raw.split('*') if w.strip()]
        if not words:
            return
        f = words[0]

        if f.startswith('FS'):
            m = re.match(r'FS([LT])[AI]X(\d)(\d)Y\d\d', f)
            if m:
                self.coord_fmt = (int(m.group(2)), int(m.group(3)), m.group(1) == 'L')
            else:
                m2 = re.search(r'X(\d)(\d)', f)
                if m2:
                    self.coord_fmt = (int(m2.group(1)), int(m2.group(2)), 'L' in f)
        elif f.startswith('ADD'):
            self._parse_add(f)
        # MO, LP, LM, LR, LS, SR, AM – not needed for outline-only parsing

    def _parse_add(self, w):
        m = re.match(r'ADD(\d+)([A-Za-z_][A-Za-z0-9_]*),?(.*)', w)
        if not m:
            return
        dcode, tname, pstr = int(m.group(1)), m.group(2).upper(), m.group(3)
        params = []
        for p in re.split(r'[Xx]', pstr):
            try:
                params.append(float(p.strip()))
            except ValueError:
                pass
        if tname == 'C':
            self.apertures[dcode] = {'type': 'C',
                                     'diameter': params[0] if params else 0.0}

    # ── word command handler ──────────────────────────────────────────

    def _word(self, cmd):
        if not cmd: return
        if cmd.startswith(('G04', 'G4')): return
        if cmd in ('M00', 'M02', 'M30', 'M0', 'M2'): return

        # ── G codes in this command ──────────────────────────────────
        for g in re.findall(r'G0*(\d+)', cmd):
            gn = int(g)
            if gn == 1:    self.interp = 'linear'
            elif gn == 2:  self.interp = 'cw_arc'
            elif gn == 3:  self.interp = 'ccw_arc'
            elif gn == 70: pass   # deprecated INCH unit
            elif gn == 71: pass   # deprecated MM unit
            elif gn == 36:
                self.in_region  = True
                self._reg_pts   = []
                self._reg_segs  = []
            elif gn == 37:
                self._close_region()

        # ── aperture select (bare D10+) ───────────────────────────────
        dm = re.match(r'^D(\d+)$', cmd)
        if dm:
            dn = int(dm.group(1))
            if dn >= 10:
                self.cur_ap = dn
            return

        # ── inline aperture select ────────────────────────────────────
        for ds in re.findall(r'D(\d+)', cmd):
            dn = int(ds)
            if dn >= 10:
                self.cur_ap = dn

        if self.coord_fmt is None: return

        # ── coordinates ───────────────────────────────────────────────
        xm = re.search(r'X([+-]?\d+)', cmd)
        ym = re.search(r'Y([+-]?\d+)', cmd)
        im = re.search(r'I([+-]?\d+)', cmd)
        jm = re.search(r'J([+-]?\d+)', cmd)

        nx = self._decode(xm.group(1)) if xm else self.cur_x
        ny = self._decode(ym.group(1)) if ym else self.cur_y

        # ── D operation (1–9) ─────────────────────────────────────────
        d_op = None
        for ds in re.findall(r'D0*(\d+)', cmd):
            dn = int(ds)
            if 1 <= dn <= 9:
                d_op = dn
                break

        if xm is None and ym is None and d_op is None:
            return   # only G codes; nothing to draw

        # ── dispatch ──────────────────────────────────────────────────
        i_off = self._decode(im.group(1)) if im else 0.0
        j_off = self._decode(jm.group(1)) if jm else 0.0

        if d_op == 1:
            self._draw(nx, ny, i_off, j_off)
            self.cur_x, self.cur_y = nx, ny

        elif d_op == 2:
            # Move – close in-progress region sub-contour
            if self.in_region and self._reg_pts:
                self._reg_segs.append(list(self._reg_pts))
                self._reg_pts = []
            self.cur_x, self.cur_y = nx, ny

        elif d_op == 3:
            # Flash (pad) – not outline geometry, just update position
            self.cur_x, self.cur_y = nx, ny

        elif d_op is None and (xm or ym):
            # Implicit D01 (deprecated but common in older files)
            self._draw(nx, ny, i_off, j_off)
            self.cur_x, self.cur_y = nx, ny

    # ── draw / arc operation ──────────────────────────────────────────

    def _draw(self, nx, ny, i_off, j_off):
        if self.interp == 'linear':
            pts = [(self.cur_x, self.cur_y), (nx, ny)]
        else:
            ccx = self.cur_x + i_off
            ccy = self.cur_y + j_off
            r_s = math.hypot(self.cur_x - ccx, self.cur_y - ccy)
            r_e = math.hypot(nx - ccx, ny - ccy)
            r   = (r_s + r_e) / 2 if r_s + r_e > 0 else 1e-6
            a_s = math.atan2(self.cur_y - ccy, self.cur_x - ccx)
            a_e = math.atan2(ny - ccy, nx - ccx)
            cw  = (self.interp == 'cw_arc')
            pts = arc_tessellate(ccx, ccy, r, a_s, a_e, cw, ARC_SEGS)

        if self.in_region:
            if not self._reg_pts:
                self._reg_pts.extend(pts)
            else:
                self._reg_pts.extend(pts[1:])  # avoid duplicating the join point
        else:
            if len(pts) >= 2:
                self._segs.append(Seg(pts))

    # ── region close ─────────────────────────────────────────────────

    def _close_region(self):
        self.in_region = False
        if self._reg_pts:
            self._reg_segs.append(list(self._reg_pts))
            self._reg_pts = []
        for contour in self._reg_segs:
            if len(contour) >= 2:
                self._segs.append(Seg(contour))
        self._reg_segs = []


# ── Connected component detection ─────────────────────────────────────────────

def find_components(segs, max_bridge=MAX_BRIDGE):
    """
    Count connected components in the segment graph using union-find.

    Two segments belong to the same component if any endpoint of one is
    within max_bridge of any endpoint of the other.

    Returns: (n_components, max_gap_found)
    where max_gap_found is the largest endpoint-to-endpoint distance that
    was treated as a connection.
    """
    if not segs:
        return 0, 0.0

    n = len(segs)
    parent = list(range(n))

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        parent[find(a)] = find(b)

    max_gap = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            endpoints_i = [segs[i].start, segs[i].end]
            endpoints_j = [segs[j].start, segs[j].end]
            for pi in endpoints_i:
                for pj in endpoints_j:
                    d = dist(pi, pj)
                    if d < max_bridge:
                        if find(i) != find(j):
                            union(i, j)
                        if d > max_gap:
                            max_gap = d

    n_components = len(set(find(i) for i in range(n)))
    return n_components, max_gap


# ── Segment chaining ──────────────────────────────────────────────────────────

def chain_segments(segs, snap=SNAP_TOL):
    """
    Greedily chain all segments into a single ordered point list.

    At each step the nearest unused segment endpoint is chosen.
    Duplicated junction points (within snap) are not repeated.
    The returned list represents a closed polygon (the last vertex
    implicitly connects back to the first).

    Returns list of (x, y) tuples.
    """
    if not segs:
        return []

    # Skip degenerate segments (zero-length lines; full-circle arcs are kept)
    valid = [s for s in segs if not s.is_degenerate()]
    if not valid:
        return []

    remaining  = list(valid)
    chain_pts  = list(remaining.pop(0).pts)

    while remaining:
        cur = chain_pts[-1]
        best_i, best_d, best_flip = 0, float('inf'), False

        for i, seg in enumerate(remaining):
            d0 = dist(cur, seg.start)
            d1 = dist(cur, seg.end)
            if d0 < best_d:
                best_d, best_i, best_flip = d0, i, False
            if d1 < best_d:
                best_d, best_i, best_flip = d1, i, True

        seg = remaining.pop(best_i)
        pts = list(reversed(seg.pts)) if best_flip else list(seg.pts)

        # Skip the first point of the incoming segment if it coincides with
        # the current chain end (avoids duplicate vertices at junctions)
        skip = 1 if dist(cur, pts[0]) < snap else 0
        chain_pts.extend(pts[skip:])

    # If the polygon is already closed (last pt ≈ first pt), remove the duplicate
    if len(chain_pts) > 1 and dist(chain_pts[-1], chain_pts[0]) < snap:
        chain_pts.pop()

    return chain_pts


# ── Corner simplification ─────────────────────────────────────────────────────

def remove_collinear(pts, tol_deg=COLLINEAR_DEG):
    """
    Remove points that are collinear with their neighbours
    (direction change at that vertex < tol_deg).

    Also removes exact duplicate consecutive points.
    The input is treated as a CLOSED polygon (last vertex connects to first).
    """
    if len(pts) < 3:
        return list(pts)

    # Remove exact/near-duplicate consecutive points first
    unique = [pts[0]]
    for p in pts[1:]:
        if dist(unique[-1], p) > 1e-9:
            unique.append(p)
    # Close-pair check (last ↔ first)
    if len(unique) > 1 and dist(unique[-1], unique[0]) < 1e-9:
        unique.pop()
    if len(unique) < 3:
        return unique

    pts = unique
    n   = len(pts)
    tol = math.radians(tol_deg)
    result = []

    for i in range(n):
        prev = pts[(i - 1) % n]
        cur  = pts[i]
        nxt  = pts[(i + 1) % n]

        v1 = (cur[0] - prev[0], cur[1] - prev[1])
        v2 = (nxt[0] - cur[0],  nxt[1] - cur[1])
        l1, l2 = math.hypot(*v1), math.hypot(*v2)

        if l1 < 1e-9 or l2 < 1e-9:
            continue   # degenerate zero-length edge – drop the point

        cos_a = (v1[0]*v2[0] + v1[1]*v2[1]) / (l1 * l2)
        cos_a = max(-1.0, min(1.0, cos_a))
        angle = math.acos(cos_a)   # 0 = collinear, π/2 = 90° corner

        if angle > tol:            # direction changes enough → keep
            result.append(cur)

    # Safety: never return fewer than 3 points
    return result if len(result) >= 3 else list(pts)


# ── Error helper ──────────────────────────────────────────────────────────────

def write_error(msg):
    path = os.path.join(SCRIPT_DIR, 'error.txt')
    with open(path, 'w') as fh:
        fh.write(msg + '\n')
    print(f'[ERROR] {msg}')
    print(f'        → error.txt written to {path}')


# ── Self-tests ────────────────────────────────────────────────────────────────

def _rect_segs(x0=0.0, y0=0.0, x1=100.0, y1=80.0):
    """Four segments forming a closed rectangle (in drawing order)."""
    return [
        Seg([(x0, y0), (x1, y0)]),
        Seg([(x1, y0), (x1, y1)]),
        Seg([(x1, y1), (x0, y1)]),
        Seg([(x0, y1), (x0, y0)]),
    ]


def run_tests():
    import random
    random.seed(42)

    PASS = '\033[92m✓\033[0m'
    FAIL = '\033[91m✗\033[0m'
    results = []

    def check(name, cond, detail=''):
        tag = PASS if cond else FAIL
        msg = f'  {tag} {name}'
        if not cond and detail:
            msg += f'\n      got: {detail}'
        print(msg)
        results.append((name, cond))

    print('\n══ border2json self-tests ══════════════════════════════════\n')

    # ── T1: Simple closed rectangle ───────────────────────────────────
    print('T1  Simple closed rectangle')
    segs = _rect_segs()
    n_comp, max_gap = find_components(segs)
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check('  component count = 1', n_comp == 1, n_comp)
    check('  max gap < SNAP_TOL',  max_gap < SNAP_TOL, f'{max_gap:.6f}')
    check('  exactly 4 corners',   len(corners) == 4, corners)
    expected = {(0,0),(100,0),(100,80),(0,80)}
    got      = {(round(x,3), round(y,3)) for x,y in corners}
    check('  correct corner coordinates', got == expected, got)

    # ── T2: Tiny gap (should be auto-bridged) ─────────────────────────
    print('T2  Tiny gap auto-bridge')
    segs = [
        Seg([(0, 0), (99.998, 0)]),    # ends 0.002 short of corner
        Seg([(100, 0), (100, 80)]),
        Seg([(100, 80), (0, 80)]),
        Seg([(0, 80), (0, 0)]),
    ]
    n_comp, max_gap = find_components(segs, max_bridge=MAX_BRIDGE)
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check('  gap bridged → 1 component', n_comp == 1, n_comp)
    check('  still 4 corners after bridge', len(corners) == 4, corners)

    # ── T3: Two disconnected shapes → should report 2 components ─────
    print('T3  Disconnected shapes detection')
    segs = _rect_segs() + [Seg([(500, 500), (600, 500)])]
    n_comp, _ = find_components(segs, max_bridge=MAX_BRIDGE)
    check('  2 shapes → 2 components', n_comp == 2, n_comp)

    # ── T4: Collinear intermediate point removal ───────────────────────
    print('T4  Collinear point removal')
    segs = [
        Seg([(0, 0), (50, 0)]),        # bottom split into two collinear segs
        Seg([(50, 0), (100, 0)]),
        Seg([(100, 0), (100, 80)]),
        Seg([(100, 80), (0, 80)]),
        Seg([(0, 80), (0, 0)]),
    ]
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check('  collinear midpoint removed → 4 corners', len(corners) == 4, corners)

    # ── T5: Shuffled segment order ────────────────────────────────────
    print('T5  Shuffled segment order')
    segs = _rect_segs()
    random.shuffle(segs)
    n_comp, _ = find_components(segs)
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check('  1 component', n_comp == 1, n_comp)
    check('  4 corners despite shuffle', len(corners) == 4, corners)

    # ── T6: Mixed segment directions ──────────────────────────────────
    print('T6  Mixed / reversed segment directions')
    segs = [
        Seg([(100, 0), (0, 0)]),    # reversed bottom
        Seg([(100, 0), (100, 80)]),
        Seg([(0, 80), (100, 80)]),  # reversed top
        Seg([(0, 0), (0, 80)]),
    ]
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check('  4 corners with mixed directions', len(corners) == 4, corners)

    # ── T7: Arc points are NOT flattened by collinear filter ──────────
    print('T7  Arc tessellation points preserved')
    # 90° arc (common in rounded PCB corners): each step changes direction
    # by 90/ARC_SEGS = 2.8° which is >> COLLINEAR_DEG (0.5°)
    arc_pts = arc_tessellate(0, 0, 10, 0, math.pi / 2, False, ARC_SEGS)
    # Embed in a simple shape: straight line → arc → straight line back
    full_pts = ([(0, -20), (0, 0)]
                + list(arc_pts[1:])          # join at (10, 0)
                + [(20, 10), (20, -20), (0, -20)])
    corners = remove_collinear(full_pts)
    arc_interior = [p for p in corners
                    if 0 < p[0] < 10 and 0 < p[1] < 10]
    check(f'  ≥ {ARC_SEGS//2} arc points preserved',
          len(arc_interior) >= ARC_SEGS // 2,
          f'{len(arc_interior)} arc interior points kept')

    # ── T8: Single-segment closed region (G36/G37 style) ─────────────
    print('T8  Single closed-region segment')
    poly = [(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)]   # closed
    segs = [Seg(poly)]
    n_comp, _ = find_components(segs)
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check('  1 component', n_comp == 1, n_comp)
    check('  4 corners', len(corners) == 4, corners)

    # ── T9: Empty input ───────────────────────────────────────────────
    print('T9  Empty input')
    check('  chain_segments([]) = []', chain_segments([]) == [], chain_segments([]))
    check('  find_components([]) = (0, 0)', find_components([]) == (0, 0.0),
          find_components([]))

    # ── T10: Many collinear points on one edge ────────────────────────
    print('T10 Many collinear points on one straight edge')
    n_pts = 20
    bottom = [Seg([(i * 5, 0), ((i+1) * 5, 0)]) for i in range(n_pts)]
    other  = [
        Seg([(100, 0), (100, 80)]),
        Seg([(100, 80), (0, 80)]),
        Seg([(0, 80), (0, 0)]),
    ]
    segs    = bottom + other
    pts     = chain_segments(segs)
    corners = remove_collinear(pts)
    check(f'  {n_pts} collinear segs → 4 corners', len(corners) == 4, corners)

    # ── T11: Degenerate zero-length segment gracefully skipped ───────────
    print('T11 Degenerate zero-length segment gracefully ignored')
    # chain_segments must filter out the zero-length seg; output stays 4 corners
    segs_degen = _rect_segs() + [Seg([(10, 10), (10, 10)])]
    pts     = chain_segments(segs_degen)
    corners = remove_collinear(pts)
    check('  degenerate seg ignored → still 4-corner rect', len(corners) == 4, corners)
    # is_degenerate() correctness
    check('  Seg([p,p]) is degenerate',        Seg([(1,1),(1,1)]).is_degenerate())
    check('  normal Seg is not degenerate',    not Seg([(0,0),(10,0)]).is_degenerate())
    circle_pts = arc_tessellate(0, 0, 5, 0, 2*math.pi, False, 32)
    check('  full-circle arc is not degenerate', not Seg(list(circle_pts)).is_degenerate())

    # ── T12: Two truly isolated segments → 2 components ──────────────
    print('T12 Truly isolated segments → 2 components')
    # No shared endpoints and gap > MAX_BRIDGE → no alternate path between them
    segs_iso = [
        Seg([(0, 0),  (50, 0)]),
        Seg([(55, 0), (100, 0)]),   # 5-unit gap; no other connecting segments
    ]
    n_comp, _ = find_components(segs_iso, max_bridge=MAX_BRIDGE)
    check('  isolated pair → 2 components', n_comp == 2, n_comp)

    # ── Summary ───────────────────────────────────────────────────────
    passed = sum(1 for _, ok in results if ok)
    total  = len(results)
    print(f'\n  {passed}/{total} checks passed\n')
    if passed < total:
        print('  FAILED:')
        for name, ok in results:
            if not ok:
                print(f'    • {name}')
        sys.exit(1)
    else:
        print('  All tests passed ✓\n')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    root.withdraw()

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Cache folder path
    cache_dir = os.path.join(script_dir, "cache")

    # Check if cache folder exists
    if not os.path.exists(cache_dir):
        print("Cache folder not found.")
        exit()

    gko_path = None
    gml_path = None

    for root, dirs, files in os.walk(cache_dir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()

            if ext == ".gko" and gko_path is None:
                gko_path = os.path.join(root, file)

            elif ext == ".gml" and gml_path is None:
                gml_path = os.path.join(root, file)

    filename = gko_path if gko_path else gml_path

    
    if not filename:
        print('No file selected – exiting.')
        return

    print(f'File     : {filename}')

    # ── Parse ──────────────────────────────────────────────────────────
    parser = BorderParser()
    try:
        segs = parser.parse(filename)
    except Exception as exc:
        write_error(f'Parse failed: {exc}')
        return

    if not segs:
        write_error(
            'No drawable line/arc geometry found in the file.\n'
            'Make sure this is a board-outline (Edge.Cuts) Gerber layer.'
        )
        
        return

    print(f'Segments : {len(segs)} raw segments parsed')

    # ── Check topology ─────────────────────────────────────────────────
    n_comp, max_gap = find_components(segs, max_bridge=MAX_BRIDGE)

    if n_comp > 1:
        msg = (
            f'Found {n_comp} disconnected shape(s) in the outline file.\n'
            f'A board outline must be a single closed loop.\n'
            f'Largest bridgeable gap: {max_gap:.4f} units (limit: {MAX_BRIDGE}).\n'
            f'Please remove stray geometry from the edge-cuts layer and re-export.'
        )
        write_error(msg)
        return

    had_gap = max_gap > SNAP_TOL
    if had_gap:
        print(f'Note     : gap of {max_gap:.4f} units auto-bridged')

    # ── Chain into one polygon ─────────────────────────────────────────
    poly_pts = chain_segments(segs, snap=SNAP_TOL)

    if len(poly_pts) < 3:
        write_error(
            f'Chained polygon has only {len(poly_pts)} vertices – degenerate outline.'
        )
        return

    # ── Simplify: remove collinear intermediate points ─────────────────
    corners = remove_collinear(poly_pts)
    print(f'Vertices : {len(poly_pts)} → {len(corners)} after collinear removal')

    # ── Round & output ─────────────────────────────────────────────────
    corners = [[round(x, 6), round(y, 6)] for x, y in corners]

    out_path = os.path.join(SCRIPT_DIR, 'activefiles/border.json')
    with open(out_path, 'w') as fh:
        json.dump([corners], fh, separators=(',', ':'))

    msg = (
        f'Exported border.json\n'
        f'{len(corners)} corner vertices\n'
        f'→ {out_path}'
        + ('\n(gap(s) auto-bridged)' if had_gap else '')
    )
    print(f'Output   : {out_path}')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if '--test' in sys.argv:
        run_tests()
    else:
        main()
