#!/usr/bin/env python3
"""
gerber_to_json.py
─────────────────
Translates Extended Gerber (RS-274X) and Excellon drill files into a flat
JSON array of vertex polygons.

Output format:
    [ [[x0,y0],[x1,y1],...], [[x0,y0],...], ... ]

Every graphical object (flash, draw, arc, region, drill hole) becomes one
polygon entry.  Curved geometry is tessellated into 16-20 evenly-spaced
vertices.

Usage:
    python gerber_to_json.py
    → opens a file-picker dialog
    → writes <original_name>.json beside this script
"""

import json
import math
import os
import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

# ─────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────

with open("cache/config.json", "r") as file:
    data = json.load(file)

ARC_SEGS   = data["circlevertices"]   # vertices used to approximate curved segments
CIRC_SEGS  = data["circlevertices"]   # vertices used to approximate full circles / drill holes


def _circle_verts(cx, cy, r, n=CIRC_SEGS):
    """n evenly-spaced vertices on a circle centred at (cx, cy)."""
    return [
        [cx + r * math.cos(2 * math.pi * i / n),
         cy + r * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]


def _arc_verts(cx, cy, r, a_start, a_end, clockwise, n=ARC_SEGS):
    """
    Tessellate an arc into n vertices.
    a_start / a_end are in radians.  The arc goes clockwise or CCW from
    a_start to a_end; the shorter angular path is NOT assumed – the
    direction flag determines which way we go.
    """
    if clockwise:
        while a_end > a_start:
            a_end -= 2 * math.pi
        while a_end < a_start - 2 * math.pi:
            a_end += 2 * math.pi
    else:
        while a_end < a_start:
            a_end += 2 * math.pi
        while a_end > a_start + 2 * math.pi:
            a_end -= 2 * math.pi

    span = a_end - a_start
    if abs(span) < 1e-12:
        return [[cx + r * math.cos(a_start), cy + r * math.sin(a_start)]]
    return [
        [cx + r * math.cos(a_start + span * i / (n - 1)),
         cy + r * math.sin(a_start + span * i / (n - 1))]
        for i in range(n)
    ]


def _stadium_verts(x0, y0, x1, y1, r, n=ARC_SEGS):
    """
    Outline vertices of a stadium shape (line segment + round caps).
    Used for Gerber draw (D01) operations.
    """
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length < 1e-12 or r < 1e-12:
        return _circle_verts(x0, y0, max(r, 1e-9), n)

    base = math.atan2(dy, dx)
    half = max(n // 2, 3)
    pts = []
    # Cap at end point (right semicircle)
    for i in range(half + 1):
        a = base - math.pi / 2 + math.pi * i / half
        pts.append([x1 + r * math.cos(a), y1 + r * math.sin(a)])
    # Cap at start point (left semicircle)
    for i in range(half + 1):
        a = base + math.pi / 2 + math.pi * i / half
        pts.append([x0 + r * math.cos(a), y0 + r * math.sin(a)])
    return pts


def _thick_arc_verts(cx, cy, r_mid, r_pen, a_start, a_end, clockwise, n=ARC_SEGS):
    """
    Outline of a thick arc (circular draw) as a closed polygon.
    Outer arc forward + inner arc backward.
    """
    r_out = r_mid + r_pen
    r_in  = max(r_mid - r_pen, 0.0)
    outer = _arc_verts(cx, cy, r_out, a_start, a_end, clockwise, n)
    inner = _arc_verts(cx, cy, r_in,  a_end, a_start, not clockwise, n)
    return outer + inner


def _rect_verts(cx, cy, w, h, rot_deg=0.0):
    """Rectangle corners, optionally rotated (degrees CCW)."""
    corners = [[-w / 2, -h / 2], [w / 2, -h / 2],
               [w / 2,  h / 2], [-w / 2,  h / 2]]
    if rot_deg:
        a = math.radians(rot_deg)
        ca, sa = math.cos(a), math.sin(a)
        return [[cx + x * ca - y * sa, cy + x * sa + y * ca] for x, y in corners]
    return [[cx + x, cy + y] for x, y in corners]


def _obround_verts(cx, cy, w, h, rot_deg=0.0, n=ARC_SEGS):
    """Obround (pill/stadium) aperture vertices before translation/rotation."""
    if abs(w - h) < 1e-9:
        return _circle_verts(cx, cy, w / 2, n)

    half = max(n // 2, 3)
    pts = []
    if w >= h:
        r  = h / 2
        ex = (w - h) / 2
        for i in range(half + 1):                        # right cap
            a = -math.pi / 2 + math.pi * i / half
            pts.append([ex + r * math.cos(a), r * math.sin(a)])
        for i in range(half + 1):                        # left cap
            a = math.pi / 2 + math.pi * i / half
            pts.append([-ex + r * math.cos(a), r * math.sin(a)])
    else:
        r  = w / 2
        ey = (h - w) / 2
        for i in range(half + 1):                        # top cap
            a = math.pi * i / half
            pts.append([r * math.cos(a), ey + r * math.sin(a)])
        for i in range(half + 1):                        # bottom cap
            a = math.pi + math.pi * i / half
            pts.append([r * math.cos(a), -ey + r * math.sin(a)])

    if rot_deg:
        a = math.radians(rot_deg)
        ca, sa = math.cos(a), math.sin(a)
        pts = [[cx + x * ca - y * sa, cy + x * sa + y * ca] for x, y in pts]
    else:
        pts = [[cx + x, cy + y] for x, y in pts]
    return pts


def _polygon_verts(cx, cy, diam, n_sides, rot_deg=0.0):
    """Regular polygon – outer_diameter is the circumscribed circle."""
    r = diam / 2
    rot = math.radians(rot_deg)
    return [
        [cx + r * math.cos(2 * math.pi * i / n_sides + rot),
         cy + r * math.sin(2 * math.pi * i / n_sides + rot)]
        for i in range(n_sides)
    ]


def _round_pts(pts, decimals=6):
    return [[round(v, decimals) for v in p] for p in pts]


# ─────────────────────────────────────────────────────────────────
# Gerber (RS-274X / Extended Gerber) parser
# ─────────────────────────────────────────────────────────────────

class GerberParser:
    """
    Parses Extended Gerber (RS-274X) files and returns a list of vertex
    polygons representing every graphical object.

    Supports:
      • FS / MO / AD / AM commands
      • Standard apertures: C, R, O, P
      • Aperture macros: primitives 0,1,2,20,21,4,5,7
      • D01 linear draws, D01 arc draws (G02/G03), D02 moves, D03 flashes
      • G36/G37 regions (filled polygons with arc segments)
      • LPD / LPC polarity (shapes are emitted for dark objects only)
      • LM / LR / LS aperture transformations
      • Step-and-repeat (SR) – shapes are replicated
      • All deprecated/no-op commands gracefully ignored
    """

    def __init__(self):
        self.shapes = []

        # Coordinate format: (int_digits, frac_digits, leading_zeros_omitted)
        self.coord_fmt = None
        self.unit      = None          # 'mm' | 'inch'

        self.cur_x = 0.0
        self.cur_y = 0.0
        self.cur_ap = None             # current aperture D-code (int)
        self.apertures = {}            # {d_code: aperture_dict}
        self.macros    = {}            # {name: [primitive_list]}
        self.interp    = 'linear'      # 'linear' | 'cw_arc' | 'ccw_arc'
        self.polarity  = 'dark'
        self.rotation  = 0.0
        self.mirroring = 'N'
        self.scaling   = 1.0

        # Region state
        self.in_region      = False
        self.region_contour = []       # vertices of the in-progress contour
        self.region_done    = []       # completed contours for current G36/G37

        # Step-and-repeat
        self.sr_stack = []             # [(nx,ny,ix,iy)] when inside SR

    # ── coordinate decoding ────────────────────────────────────────

    def _decode(self, s):
        """Decode a raw Gerber coordinate string → float."""
        if s is None:
            return None
        s = s.strip()
        if '.' in s:
            return float(s)
        sign = 1
        if s and s[0] == '-':
            sign = -1; s = s[1:]
        elif s and s[0] == '+':
            s = s[1:]
        if not s:
            return 0.0
        _, frac, leading_omit = self.coord_fmt
        if leading_omit:
            # Standard modern Gerber: just divide by 10^frac
            return sign * int(s) / (10 ** frac)
        else:
            # Trailing-zeros omitted: pad right to (int_digits+frac) then divide
            total = self.coord_fmt[0] + frac
            s_pad = s.ljust(total, '0')
            return sign * int(s_pad) / (10 ** frac)

    # ── aperture shape helpers ─────────────────────────────────────

    def _ap_verts(self, ap, cx, cy):
        """Vertices for aperture `ap` flashed at (cx, cy)."""
        kind = ap['type']
        # Apply current global transformation to size
        s    = self.scaling
        rot  = ap.get('rotation', 0.0) + self.rotation

        mirror_x = 'X' in self.mirroring or 'XY' == self.mirroring
        mirror_y = 'Y' in self.mirroring or 'XY' == self.mirroring

        if kind == 'C':
            verts = _circle_verts(cx, cy, ap['diameter'] / 2 * s)
        elif kind == 'R':
            verts = _rect_verts(cx, cy, ap['x_size'] * s, ap['y_size'] * s, rot)
        elif kind == 'O':
            verts = _obround_verts(cx, cy, ap['x_size'] * s, ap['y_size'] * s, rot)
        elif kind == 'P':
            verts = _polygon_verts(cx, cy, ap['outer_diameter'] * s,
                                   ap['vertices'], rot)
        elif kind == 'macro':
            verts = self._render_macro(ap, cx, cy)
        else:
            verts = _circle_verts(cx, cy, 0.05 * s)  # fallback dot

        if mirror_x:
            verts = [[-v[0], v[1]] for v in verts]
        if mirror_y:
            verts = [[v[0], -v[1]] for v in verts]

        return verts

    # ── aperture macro renderer ────────────────────────────────────

    def _render_macro(self, ap, cx, cy):
        """Best-effort render of an aperture macro → list of [x,y]."""
        name   = ap.get('macro_name', '')
        params = ap.get('params', [])
        if name not in self.macros:
            return _circle_verts(cx, cy, 0.1)

        variables = {str(i + 1): p for i, p in enumerate(params)}

        def ev(expr):
            try:
                e = str(expr)
                # Replace $n variables, longest first to avoid partial replacement
                for k in sorted(variables, key=lambda x: -len(x)):
                    e = e.replace('$' + k, str(variables[k]))
                # Macro uses lowercase 'x' for multiply
                e = e.replace('x', '*').replace('X', '*')
                return float(eval(e, {"__builtins__": {}}))  # noqa: S307
            except Exception:
                return 0.0

        all_verts = []
        for prim in self.macros[name]:
            code = prim[0]
            args = [ev(a) for a in prim[1:]]
            if not args:
                continue

            def rot_pt(x, y, angle_deg):
                a = math.radians(angle_deg)
                return (x * math.cos(a) - y * math.sin(a) + cx,
                        x * math.sin(a) + y * math.cos(a) + cy)

            if code == 0:   # comment
                continue
            elif code == 1:  # circle
                if not args[0]:
                    continue
                d  = args[1] if len(args) > 1 else 0.1
                px = args[2] if len(args) > 2 else 0.0
                py = args[3] if len(args) > 3 else 0.0
                ra = args[4] if len(args) > 4 else 0.0
                px2, py2 = rot_pt(px, py, ra)
                all_verts.append(_circle_verts(px2, py2, d / 2))
            elif code in (2, 20):  # vector line
                if not args[0]:
                    continue
                width = args[1] if len(args) > 1 else 0.1
                sx = args[2] if len(args) > 2 else 0.0
                sy = args[3] if len(args) > 3 else 0.0
                ex = args[4] if len(args) > 4 else 0.0
                ey = args[5] if len(args) > 5 else 0.0
                ra = args[6] if len(args) > 6 else 0.0
                p1 = rot_pt(sx, sy, ra)
                p2 = rot_pt(ex, ey, ra)
                all_verts.append(_stadium_verts(p1[0], p1[1], p2[0], p2[1], width / 2))
            elif code == 21:  # center line (rectangle)
                if not args[0]:
                    continue
                w  = args[1] if len(args) > 1 else 0.1
                h  = args[2] if len(args) > 2 else 0.1
                px = args[3] if len(args) > 3 else 0.0
                py = args[4] if len(args) > 4 else 0.0
                ra = args[5] if len(args) > 5 else 0.0
                px2, py2 = rot_pt(px, py, 0)
                all_verts.append(_rect_verts(px2, py2, w, h, ra))
            elif code == 4:  # outline polygon
                if not args[0]:
                    continue
                n_v = int(args[1]) if len(args) > 1 else 0
                pts = []
                for i in range(n_v + 1):
                    xi = args[2 + 2 * i] if 2 + 2 * i < len(args) else 0.0
                    yi = args[3 + 2 * i] if 3 + 2 * i < len(args) else 0.0
                    pts.append([xi, yi])
                ra_idx = 2 + 2 * (n_v + 1)
                ra = args[ra_idx] if ra_idx < len(args) else 0.0
                if ra:
                    a = math.radians(ra)
                    ca, sa = math.cos(a), math.sin(a)
                    pts = [[x * ca - y * sa + cx, x * sa + y * ca + cy] for x, y in pts]
                else:
                    pts = [[x + cx, y + cy] for x, y in pts]
                if pts and pts[-1] == pts[0]:
                    pts = pts[:-1]
                if len(pts) >= 3:
                    all_verts.append(pts)
            elif code == 5:  # polygon primitive
                if not args[0]:
                    continue
                n_s = int(args[1]) if len(args) > 1 else 6
                px  = args[2] if len(args) > 2 else 0.0
                py  = args[3] if len(args) > 3 else 0.0
                d   = args[4] if len(args) > 4 else 0.1
                ra  = args[5] if len(args) > 5 else 0.0
                all_verts.append(_polygon_verts(cx + px, cy + py, d, n_s, ra))
            elif code == 7:  # thermal – approximate as annular sector polygon
                px  = args[0] if len(args) > 0 else 0.0
                py  = args[1] if len(args) > 1 else 0.0
                od  = args[2] if len(args) > 2 else 0.2
                # Just render the outer ring (ignore gap/inner detail)
                all_verts.append(_circle_verts(cx + px, cy + py, od / 2))

        if not all_verts:
            return _circle_verts(cx, cy, 0.1)
        # Return first (primary) shape – macro multi-shape not yet composited
        return all_verts[0]

    # ── tokeniser ──────────────────────────────────────────────────

    def _tokenize(self, content):
        """
        Split Gerber content into ('extended', text) and ('word', text) tokens.
        Line endings and whitespace are insignificant outside string values.
        """
        tokens = []
        i = 0
        n = len(content)
        while i < n:
            ch = content[i]
            if ch == '%':
                j = content.find('%', i + 1)
                if j == -1:
                    break
                tokens.append(('extended', content[i + 1:j]))
                i = j + 1
            elif ch in ' \t\r\n':
                i += 1
            elif ch == '*':
                i += 1
            else:
                j = i
                while j < n and content[j] not in ('*', '%'):
                    j += 1
                word = content[i:j].strip()
                if word:
                    tokens.append(('word', word))
                i = j + 1 if j < n else j
        return tokens

    # ── main parse entry ───────────────────────────────────────────

    def parse(self, filename):
        with open(filename, 'r', errors='replace') as fh:
            content = fh.read()
        tokens = self._tokenize(content)
        for tok in tokens:
            try:
                if tok[0] == 'extended':
                    self._ext(tok[1])
                else:
                    self._word(tok[1])
            except Exception:
                pass  # be lenient with malformed commands
        return self.shapes

    # ── extended command dispatcher ────────────────────────────────

    def _ext(self, raw):
        # Split extended block on '*' to get individual word commands inside
        words = [w.strip() for w in raw.split('*') if w.strip()]
        if not words:
            return
        first = words[0]

        if first.startswith('FS'):
            self._parse_fs(first)
        elif first.startswith('MO'):
            self._parse_mo(first)
        elif first.startswith('ADD'):
            self._parse_add(first)
        elif first.startswith('AM'):
            self._parse_am(first, words)
        elif first.startswith('LP'):
            self.polarity = 'dark' if first[2:3] == 'D' else 'clear'
        elif first.startswith('LM'):
            self.mirroring = first[2:].strip() or 'N'
        elif first.startswith('LR'):
            try:
                self.rotation = float(first[2:])
            except ValueError:
                pass
        elif first.startswith('LS'):
            try:
                self.scaling = float(first[2:])
            except ValueError:
                pass
        elif first.startswith('SR'):
            self._parse_sr(first)
        # AB (block aperture), TF/TA/TO/TD (attributes), IN/LN, IP, AS, IR, MI, OF, SF
        # → all silently ignored (no vertex data)

    def _parse_fs(self, w):
        # %FSLAX26Y26*%  or  %FSTAX26Y26*%  (T = trailing zeros omitted)
        m = re.match(r'FS([LT])([AI])X(\d)(\d)Y\d\d', w)
        if m:
            leading_omit = (m.group(1) == 'L')
            int_d  = int(m.group(3))
            frac_d = int(m.group(4))
            self.coord_fmt = (int_d, frac_d, leading_omit)
        else:
            # Fallback: try just extracting the X digit pair
            m2 = re.search(r'X(\d)(\d)', w)
            if m2:
                leading_omit = ('L' in w)
                self.coord_fmt = (int(m2.group(1)), int(m2.group(2)), leading_omit)

    def _parse_mo(self, w):
        if 'MM' in w:
            self.unit = 'mm'
        elif 'IN' in w:
            self.unit = 'inch'

    def _parse_add(self, w):
        # %ADDnnC,0.5*%  %ADDnnR,0.8X0.4*%  etc.
        m = re.match(r'ADD(\d+)([A-Za-z_][A-Za-z0-9_]*),?(.*)', w)
        if not m:
            return
        dcode  = int(m.group(1))
        tname  = m.group(2).upper()
        pstr   = m.group(3)
        # Parameters separated by X (case-insensitive in Gerber)
        params = []
        for p in re.split(r'[Xx]', pstr):
            p = p.strip()
            if p:
                try:
                    params.append(float(p))
                except ValueError:
                    pass

        if tname == 'C':
            ap = {'type': 'C',
                  'diameter': params[0] if params else 0.0}
        elif tname == 'R':
            ap = {'type': 'R',
                  'x_size': params[0] if len(params) > 0 else 0.1,
                  'y_size': params[1] if len(params) > 1 else params[0] if params else 0.1}
        elif tname == 'O':
            ap = {'type': 'O',
                  'x_size': params[0] if len(params) > 0 else 0.1,
                  'y_size': params[1] if len(params) > 1 else params[0] if params else 0.1}
        elif tname == 'P':
            ap = {'type': 'P',
                  'outer_diameter': params[0] if len(params) > 0 else 1.0,
                  'vertices':       int(params[1]) if len(params) > 1 else 6,
                  'rotation':       params[2] if len(params) > 2 else 0.0}
        else:
            # Macro aperture
            ap = {'type': 'macro', 'macro_name': tname, 'params': params}

        if len(params) > (3 if tname in ('C',) else
                          3 if tname in ('R', 'O') else
                          4 if tname == 'P' else 100):
            ap['hole_diameter'] = params[-1]

        self.apertures[dcode] = ap

    def _parse_am(self, first_word, all_words):
        # %AMNAME*prim1*prim2*...%
        name = first_word[2:].strip()
        prims = []
        for w in all_words[1:]:
            w = w.strip()
            if not w:
                continue
            if w.startswith('$'):
                continue   # variable assignment – skip for simple rendering
            parts = w.split(',')
            try:
                code = int(parts[0])
                prims.append([code] + parts[1:])
            except (ValueError, IndexError):
                pass
        self.macros[name] = prims

    def _parse_sr(self, w):
        # %SRX3Y2I5.0J5.0*%  or  %SR*%  (end)
        if re.match(r'SR\s*$', w) or w == 'SR':
            # End SR – pop stack; shapes were already added during parsing
            return
        mx = re.search(r'X(\d+)', w)
        my = re.search(r'Y(\d+)', w)
        mi = re.search(r'I([+-]?[\d.]+)', w)
        mj = re.search(r'J([+-]?[\d.]+)', w)
        if mx and my and mi and mj:
            self.sr_stack.append((int(mx.group(1)), int(my.group(1)),
                                  float(mi.group(1)), float(mj.group(1))))

    # ── word command dispatcher ────────────────────────────────────

    def _word(self, cmd):
        if not cmd:
            return

        # ── G04 comment ──────────────────────────────────────────
        if cmd.startswith('G04') or cmd.startswith('G4'):
            return

        # ── End of file ──────────────────────────────────────────
        if cmd in ('M02', 'M00', 'M2', 'M0'):
            return

        # ── No-op M codes ────────────────────────────────────────
        if cmd in ('M01', 'M1'):
            return

        # ── Deprecated unit G codes ──────────────────────────────
        if cmd in ('G70',):
            self.unit = 'inch'; return
        if cmd in ('G71',):
            self.unit = 'mm'; return

        # ── Aperture selection: bare D10, D11 … ─────────────────
        dm = re.match(r'^D(\d+)$', cmd)
        if dm:
            dn = int(dm.group(1))
            if dn >= 10:
                self.cur_ap = dn
            return

        # ── Extract G codes from this command ────────────────────
        for g in re.findall(r'G0*(\d+)', cmd):
            gn = int(g)
            if gn == 1:
                self.interp = 'linear'
            elif gn == 2:
                self.interp = 'cw_arc'
            elif gn == 3:
                self.interp = 'ccw_arc'
            elif gn == 36:
                self.in_region      = True
                self.region_contour = []
                self.region_done    = []
            elif gn == 37:
                self._close_region()
            elif gn == 75:
                pass  # multi-quadrant arc mode – we always use it
            elif gn in (54, 55, 74, 90, 91):
                pass  # deprecated / no-op

        # ── Need coord_fmt to proceed ────────────────────────────
        if self.coord_fmt is None:
            return

        # ── Extract coordinates ──────────────────────────────────
        xm = re.search(r'X([+-]?\d+)', cmd)
        ym = re.search(r'Y([+-]?\d+)', cmd)
        im = re.search(r'I([+-]?\d+)', cmd)
        jm = re.search(r'J([+-]?\d+)', cmd)

        new_x = self._decode(xm.group(1)) if xm else self.cur_x
        new_y = self._decode(ym.group(1)) if ym else self.cur_y

        # ── Extract D operation (0-9) ────────────────────────────
        # D op must have value 1-9 (not aperture-select 10+)
        d_op = None
        for d_str in re.findall(r'D0*(\d+)', cmd):
            dn = int(d_str)
            if dn <= 9:
                d_op = dn
                break
            elif dn >= 10 and d_op is None:
                # Inline aperture selection (rare but valid)
                self.cur_ap = dn

        # ── No coordinates and no D op → nothing to do ───────────
        if xm is None and ym is None and d_op is None:
            return

        # ── Dispatch D operation ─────────────────────────────────
        if d_op == 1:
            self._op_draw(new_x, new_y,
                          self._decode(im.group(1)) if im else 0.0,
                          self._decode(jm.group(1)) if jm else 0.0)
            self.cur_x, self.cur_y = new_x, new_y

        elif d_op == 2:
            # Move – close in-progress region contour if active
            if self.in_region and self.region_contour:
                self.region_done.append(self.region_contour)
                self.region_contour = []
            self.cur_x, self.cur_y = new_x, new_y

        elif d_op == 3:
            # Flash – only dark polarity produces real geometry
            if not self.in_region and self.polarity == 'dark':
                ap = self.apertures.get(self.cur_ap)
                if ap:
                    verts = self._ap_verts(ap, new_x, new_y)
                    if len(verts) >= 2:
                        self._emit(verts)
            self.cur_x, self.cur_y = new_x, new_y

        elif d_op is None and (xm or ym):
            # Coordinates without explicit D code → treat as D01 (deprecated)
            self._op_draw(new_x, new_y,
                          self._decode(im.group(1)) if im else 0.0,
                          self._decode(jm.group(1)) if jm else 0.0)
            self.cur_x, self.cur_y = new_x, new_y

    # ── draw / arc operation ───────────────────────────────────────

    def _op_draw(self, nx, ny, i_off, j_off):
        if self.interp == 'linear':
            if self.in_region:
                if not self.region_contour:
                    self.region_contour.append([round(self.cur_x, 6),
                                                round(self.cur_y, 6)])
                self.region_contour.append([round(nx, 6), round(ny, 6)])
            else:
                if self.polarity != 'dark':
                    return
                ap = self.apertures.get(self.cur_ap)
                if ap and ap['type'] == 'C':
                    r = ap['diameter'] / 2 * self.scaling
                    self._emit(_stadium_verts(self.cur_x, self.cur_y, nx, ny, r))

        elif self.interp in ('cw_arc', 'ccw_arc'):
            ccx = self.cur_x + i_off
            ccy = self.cur_y + j_off
            r_s = math.hypot(self.cur_x - ccx, self.cur_y - ccy)
            r_e = math.hypot(nx - ccx, ny - ccy)
            r   = (r_s + r_e) / 2 if r_s + r_e > 0 else 1e-6

            a_s = math.atan2(self.cur_y - ccy, self.cur_x - ccx)
            a_e = math.atan2(ny - ccy,          nx - ccx)
            cw  = (self.interp == 'cw_arc')

            # Full circle when start == end
            same = (abs(self.cur_x - nx) < 1e-7 and abs(self.cur_y - ny) < 1e-7)

            if self.in_region:
                if not self.region_contour:
                    self.region_contour.append([round(self.cur_x, 6),
                                                round(self.cur_y, 6)])
                if same:
                    arc_pts = _circle_verts(ccx, ccy, r, ARC_SEGS * 2)
                else:
                    arc_pts = _arc_verts(ccx, ccy, r, a_s, a_e, cw, ARC_SEGS)
                for p in arc_pts[1:]:
                    self.region_contour.append([round(p[0], 6), round(p[1], 6)])
            else:
                if self.polarity != 'dark':
                    return
                ap = self.apertures.get(self.cur_ap)
                if ap and ap['type'] == 'C':
                    pen = ap['diameter'] / 2 * self.scaling
                    if same:
                        # Full-circle draw → thick ring
                        verts = _thick_arc_verts(ccx, ccy, r, pen,
                                                 a_s, a_s - 2 * math.pi
                                                 if cw else a_s + 2 * math.pi,
                                                 cw, ARC_SEGS * 2)
                    else:
                        verts = _thick_arc_verts(ccx, ccy, r, pen,
                                                 a_s, a_e, cw, ARC_SEGS)
                    if verts:
                        self._emit(verts)

    def _close_region(self):
        self.in_region = False
        if self.region_contour:
            self.region_done.append(self.region_contour)
            self.region_contour = []
        if self.polarity == 'dark':
            for c in self.region_done:
                if len(c) >= 3:
                    self._emit(c)
        self.region_done = []

    def _emit(self, verts):
        """Add a polygon (possibly replicated by SR) to the output."""
        pts = _round_pts(verts)
        if len(pts) < 2:
            return
        # If inside step-and-repeat, emit all copies
        if self.sr_stack:
            nx, ny, ix, iy = self.sr_stack[-1]
            for row in range(ny):
                for col in range(nx):
                    ox, oy = col * ix, row * iy
                    self.shapes.append([[p[0] + ox, p[1] + oy] for p in pts])
        else:
            self.shapes.append(pts)


# ─────────────────────────────────────────────────────────────────
# Excellon drill-file parser
# ─────────────────────────────────────────────────────────────────

class ExcellonParser:
    """
    Parses Excellon Format 1 and Format 2 drill files.

    Handles:
      • M48 header with INCH/METRIC, LZ/TZ, FMAT, tool definitions
      • Body: tool selection (Tn), drill hits (X/Y), repeat (R)
      • Mode: G05/G81 drill, G00 rapid move, G01 route/slot
      • G85 slot (overlapping holes), G32/G33 circular pocket
      • Incremental mode (ICI,ON / G91)
      • Missing header – assumes inch / 2.4 / LZ defaults
    """

    DEFAULT_HOLE_DIAMETER_INCH   = 0.8 / 25.4   # ~0.8 mm in inches
    DEFAULT_HOLE_DIAMETER_METRIC = 0.8           # mm

    def __init__(self):
        self.shapes = []

        self.unit        = 'inch'
        self.zero_mode   = 'LZ'
        self.int_digits  = 2
        self.frac_digits = 4    # 2.4 format for inches
        self.format_ver  = 2    # FMAT,2 by default

        self.tools       = {}   # {n: {'diameter': float}}
        self.cur_tool    = None
        self.mode        = 'drill'  # 'drill' | 'route' | 'rout_line' | 'slot'
        self.cur_x       = 0.0
        self.cur_y       = 0.0
        self.incremental = False

    # ── coordinate decode ──────────────────────────────────────────

    def _decode(self, s):
        """Decode an Excellon coordinate string → float in file units."""
        if not s:
            return 0.0
        s = s.strip()
        if '.' in s:
            return float(s)
        sign = 1
        if s and s[0] == '-':
            sign = -1; s = s[1:]
        elif s and s[0] == '+':
            s = s[1:]
        if not s:
            return 0.0
        # Both LZ and TZ: integer value / 10^frac_digits
        # (see research doc §3 – the stored integer is identical for a given value
        #  regardless of mode; only the number of written digits differs)
        try:
            return sign * int(s) / (10 ** self.frac_digits)
        except ValueError:
            return 0.0

    def _xy(self, line):
        """Parse X and Y from a line, respecting incremental mode."""
        xm = re.search(r'X([+-]?\d+\.?\d*)', line)
        ym = re.search(r'Y([+-]?\d+\.?\d*)', line)
        nx = self._decode(xm.group(1)) if xm else self.cur_x
        ny = self._decode(ym.group(1)) if ym else self.cur_y
        if self.incremental:
            if xm: nx += self.cur_x
            if ym: ny += self.cur_y
        # Force absolute for axes that weren't updated
        if not xm:
            nx = self.cur_x
        if not ym:
            ny = self.cur_y
        return nx, ny

    # ── shape emitters ─────────────────────────────────────────────

    def _tool_diam(self):
        t = self.tools.get(self.cur_tool, {})
        d = t.get('diameter', None)
        if d is None:
            d = (self.DEFAULT_HOLE_DIAMETER_INCH
                 if self.unit == 'inch'
                 else self.DEFAULT_HOLE_DIAMETER_METRIC)
        return d

    def _emit_hole(self, x, y):
        r = self._tool_diam() / 2
        verts = _circle_verts(x, y, r, CIRC_SEGS)
        self.shapes.append(_round_pts(verts))

    def _emit_slot(self, x0, y0, x1, y1):
        r = self._tool_diam() / 2
        verts = _stadium_verts(x0, y0, x1, y1, r, ARC_SEGS)
        self.shapes.append(_round_pts(verts))

    # ── main parse entry ───────────────────────────────────────────

    def parse(self, filename):
        with open(filename, 'r', errors='replace') as fh:
            lines = fh.readlines()

        in_header    = False
        header_found = False

        for raw in lines:
            # Strip comments and whitespace
            line = raw.strip()
            sc   = line.find(';')
            if sc != -1:
                line = line[:sc].strip()
            if not line:
                continue

            upper = line.upper()

            # ── Header start ──────────────────────────────────────
            if upper == 'M48':
                in_header    = True
                header_found = True
                continue

            # ── Header end ────────────────────────────────────────
            if in_header and upper in ('M95', '%'):
                in_header = False
                continue

            if in_header:
                self._header_line(line, upper)
                continue

            # ── Body ──────────────────────────────────────────────
            self._body_line(line, upper)

        return self.shapes

    # ── header line parser ─────────────────────────────────────────

    def _header_line(self, line, upper):
        if upper.startswith('INCH'):
            self.unit = 'inch'
            self.int_digits, self.frac_digits = 2, 4
            if 'TZ' in upper:
                self.zero_mode = 'TZ'
            else:
                self.zero_mode = 'LZ'
        elif upper.startswith('METRIC'):
            self.unit = 'metric'
            self.int_digits, self.frac_digits = 3, 3
            if 'TZ' in upper:
                self.zero_mode = 'TZ'
            else:
                self.zero_mode = 'LZ'
        elif upper.startswith('FMAT'):
            m = re.search(r'FMAT,(\d)', upper)
            if m:
                self.format_ver = int(m.group(1))
        elif upper == 'M71':
            self.unit = 'metric'; self.int_digits, self.frac_digits = 3, 3
        elif upper == 'M72':
            self.unit = 'inch';   self.int_digits, self.frac_digits = 2, 4
        elif upper.startswith('ICI'):
            self.incremental = ('ON' in upper)
        else:
            # Tool definition: T1C0.040  T01C0.040F200S65
            m = re.match(r'T(\d+)C([0-9.]+)', line, re.IGNORECASE)
            if m:
                self.tools[int(m.group(1))] = {'diameter': float(m.group(2))}

    # ── body line parser ───────────────────────────────────────────

    def _body_line(self, line, upper):
        # End of program
        if upper in ('M30', 'M02', 'M00', 'M2', 'M0'):
            return

        # ── G codes ───────────────────────────────────────────────
        if upper.startswith('G'):
            gm = re.match(r'G0*(\d+)', upper)
            if gm:
                gn = int(gm.group(1))
                if gn in (5, 81):
                    self.mode = 'drill'
                elif gn == 0:
                    self.mode = 'route'       # rapid move, no drill
                elif gn == 1:
                    self.mode = 'rout_line'   # linear route (slot)
                elif gn in (85, 87):
                    self.mode = 'slot'
                elif gn in (32, 33):
                    self.mode = 'drill'       # circular pocket → treat as drill
                elif gn == 90:
                    self.incremental = False
                elif gn == 91:
                    self.incremental = True
            # Line may also carry coordinates (e.g. G05X...Y...)
            if 'X' in upper or 'Y' in upper:
                self._coord_line(line, upper)
            return

        # ── M codes ───────────────────────────────────────────────
        if upper.startswith('M'):
            return

        # ── ICI mode inline ───────────────────────────────────────
        if 'ICI' in upper:
            self.incremental = ('ON' in upper)
            return

        # ── Tool selection: T1  T01  (body only) ─────────────────
        # Must NOT be a tool definition (which has 'C')
        if re.match(r'^T\d+$', upper):
            self.cur_tool = int(upper[1:])
            return
        tm = re.match(r'^T(\d+)([^C]|$)', upper)
        if tm:
            self.cur_tool = int(tm.group(1))
            if not ('X' in upper or 'Y' in upper):
                return

        # ── Repeat: R9X001Y000M22  or  R9M26 ─────────────────────
        rm = re.match(r'^R(\d+)(.*)', upper)
        if rm:
            count = int(rm.group(1))
            rest  = rm.group(2)
            xm    = re.search(r'X([+-]?\d+\.?\d*)', rest)
            ym    = re.search(r'Y([+-]?\d+\.?\d*)', rest)
            dx    = self._decode(xm.group(1)) if xm else 0.0
            dy    = self._decode(ym.group(1)) if ym else 0.0
            for _ in range(count):
                self.cur_x += dx
                self.cur_y += dy
                if self.mode != 'route':
                    self._emit_hole(self.cur_x, self.cur_y)
            return

        # ── Coordinate record ─────────────────────────────────────
        if 'X' in upper or 'Y' in upper:
            self._coord_line(line, upper)

    def _coord_line(self, line, upper):
        nx, ny = self._xy(line)

        if self.mode == 'drill':
            self._emit_hole(nx, ny)

        elif self.mode == 'route':
            # G00 rapid – no drilling; just update position
            pass

        elif self.mode in ('rout_line', 'slot'):
            # Slot / route line → stadium polygon
            if math.hypot(nx - self.cur_x, ny - self.cur_y) > 1e-9:
                self._emit_slot(self.cur_x, self.cur_y, nx, ny)
            else:
                self._emit_hole(nx, ny)

        self.cur_x, self.cur_y = nx, ny


# ─────────────────────────────────────────────────────────────────
# Format detection
# ─────────────────────────────────────────────────────────────────

GERBER_EXTS = {
    '.gbr', '.ger', '.gtl', '.gbl', '.gts', '.gbs', '.gto', '.gbo',
    '.gtp', '.gbp', '.gko', '.gm1', '.gm2', '.gm3', '.gm4', '.gm5',
    '.g2l', '.g3l', '.g2',  '.g3',  '.cmp', '.sol', '.stc', '.sts',
    '.plc', '.pls', '.crc', '.crs', '.pth', '.smb', '.smt', '.art',
    '.copper', '.gerber', '.top', '.bot',
}
DRILL_EXTS = {
    '.drl', '.exc', '.xln', '.drill', '.ncd', '.tap', '.drd',
    '.xnc', '.cnc', '.nc',
}


def detect_format(filename):
    """Return 'gerber' or 'excellon' for the given file."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in GERBER_EXTS:
        return 'gerber'
    if ext in DRILL_EXTS:
        return 'excellon'

    # Content sniffing (first 4 KB)
    try:
        with open(filename, 'r', errors='replace') as fh:
            head = fh.read(4096)
    except OSError:
        return 'gerber'

    # Strong Gerber signals
    if re.search(r'%FS[LT]A', head):
        return 'gerber'
    if re.search(r'%MO(MM|IN)\*%', head):
        return 'gerber'
    if re.search(r'%ADD\d+[CROP]', head):
        return 'gerber'

    # Strong Excellon signals
    if re.search(r'^M48\s*$', head, re.MULTILINE):
        return 'excellon'
    if re.search(r'^T\d+C[\d.]+', head, re.MULTILINE):
        return 'excellon'
    if re.search(r'^(INCH|METRIC)(,LZ|,TZ)?', head, re.MULTILINE):
        return 'excellon'

    # Gerber fallback – most PCB files that reach here are Gerber
    if '%' in head:
        return 'gerber'

    return 'gerber'


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

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

    file_path = None

    for root, dirs, files in os.walk(cache_dir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()

            if ext == ".gtl" and file_path is None:
                file_path = os.path.join(root, file)

    filename = file_path

    if not filename:
        print('No file selected – exiting.')
        return

    print(f'File     : {filename}')
    fmt = detect_format(filename)
    print(f'Format   : {fmt}')

    if fmt == 'gerber':
        parser = GerberParser()
    else:
        parser = ExcellonParser()

    try:
        shapes = parser.parse(filename)
    except Exception as exc:
        raise

    # Drop degenerate shapes (single points)
    shapes = [s for s in shapes if len(s) >= 2]

    # Output path: same directory as this script, original stem + .json
    script_dir = os.path.dirname(os.path.abspath(__file__))
    stem       = os.path.splitext(os.path.basename(filename))[0]
    out_path   = os.path.join(script_dir, 'activefiles/topcopper.json')

    with open(out_path, 'w') as fh:
        json.dump(shapes, fh, separators=(',', ':'))

    msg = f'Exported {len(shapes)} shapes → {out_path}'
    print(msg)


if __name__ == '__main__':
    main()
