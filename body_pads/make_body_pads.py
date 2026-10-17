#!/usr/bin/env python
"""
make_body_pads.py - forearm / bicep / chest resistive tactile pads as FPCB pairs.

Follows the construction of the Flex-Glove paper (Murphy et al., CHI EA '25) and the
hand pipeline in make_fpcbs.ipynb, applied to three larger body pads:

  * two FPCBs per pad ("Front" = column electrodes on F.Cu, "Back" = row electrodes on
    B.Cu) with a piezoresistive (Velostat) layer in between,
  * coverlay (F.Mask / B.Mask) opened over the sensing region so the electrodes touch
    the Velostat, adhesive (Eco1.User) in the ring between the outline and the opening,
  * electrodes routed along nested offset contours of the sensing region (the paper's
    "inward extrusions" of the hand contour) to a tail, where - as in README Step 5 -
    every trace ends in a via and a 16-pin 0.5 mm FFC connector sits on the opposite
    side of the board,
  * inner cut-outs between taxels for flexibility (copper polygons buffered and
    subtracted from the coverlay opening, as in the paper),
  * at most 16 x 16 taxels per pad so the same 16 x 16 readout as the glove can be used.

Outputs per pad (body_pads/out/<pad>/):
  <pad>_Front.svg / <pad>_Back.svg     layer-grouped SVGs in the same convention as handFront.svg
  <pad>_Front.kicad_mod / _Back        footprints made with svg2mod exactly like the notebook does
  <pad>_Front.kicad_pcb / _Back        complete boards (vias + connector added with KiCad's python)
  gerbers_Front/, gerbers_Back/ (+zip) full Gerber set + PTH/NPTH drill files plotted by kicad-cli
  <pad>_cut_template.svg               outline + inner cuts in mm, for laser cutting Velostat/silicone
  pinout_Front.csv / pinout_Back.csv   connector pin -> electrode mapping
  <pad>_preview.png                    render of both boards
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import substring, unary_union

MM2PX = 96.0 / 25.4  # svg2mod DEFAULT_DPI is 96, same as the notebook

KICAD_CLI_CANDIDATES = [
    os.environ.get("KICAD_CLI", ""),
    r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "kicad-cli",
]
GERBER_LAYERS = "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts,Eco1.User,Eco2.User"


# --------------------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------------------
@dataclass
class Params:
    pitch: float = 12.5          # taxel pitch (mm)
    electrode_w: float = 3.0     # electrode strip width inside the sensing region (mm)
    trace_w: float = 0.25        # connecting trace width (paper: 0.1 mm; wider = more bend cycles)
    trace_gap: float = 0.25      # gap between connecting traces
    margin: float = 10.0         # outline -> coverlay opening (holds 16 traces + clearance)
    ring0: float = 1.0           # first trace lane offset from the opening edge
    opening_r: float = 3.0       # corner radius of the coverlay opening
    cut_clear: float = 0.8       # inner cut-out to copper clearance (paper: 0.5 mm)
    cut_r: float = 2.0           # corner radius of inner cut-outs
    cut_margin: float = 2.5      # keep cut-outs this far inside the opening edge
    max_ch: int = 16             # channels per board (16-pin FFC, 16 x 16 readout)
    edge_clear: float = 0.5      # copper to board edge (paper: 0.5 mm)
    # --- tail: 0.5 mm bus -> staircase fan-out to 1.0 mm -> two staggered via rows ->
    #     (other side) staircase back to the 0.5 mm pads of the FFC connector
    tail_w: float = 17.0
    tail_tip_r: float = 1.0
    tail_tip_margin: float = 0.6
    via_pitch: float = 1.0
    via_d: float = 0.45          # README: 0.35 / 0.15; 0.45 / 0.2 is inside common flex limits
    via_drill: float = 0.2
    fan_y0: float = 1.0          # lowest front jog above the body edge
    via_y0: float = 6.0          # first via row above the body edge
    via_row_gap: float = 1.5
    back_y0: float = 1.0         # lowest back jog above the second via row
    stiffener_from: float = 4.0  # stiffener (Eco2.User) from here to the tail tip
    conn_lib: str = "Connector_FFC-FPC.pretty"
    conn_footprint: str = "Hirose_FH12-16S-0.5SH_1x16-1MP_P0.50mm_Horizontal"
    conn_pad_pitch: float = 0.5
    conn_pad_size: tuple = (0.3, 1.3)
    conn_pad_offset: float = 1.85  # pad row to footprint origin
    conn_front: float = 4.9        # origin to courtyard front (cable side)
    conn_back: float = 3.0         # origin to courtyard rear (pad side)
    conn_half_w: float = 7.05

    @property
    def lane_pitch(self):
        return self.trace_w + self.trace_gap

    @property
    def web(self):
        # polyimide web left around an electrode strip when cut-outs are made
        return self.electrode_w + 2 * self.cut_clear + 0.4

    @property
    def tail_sep(self):
        return self.tail_w + 4.0


@dataclass
class PadSpec:
    name: str
    body: Polygon                 # mm, y up; must have a straight horizontal top edge at tail_x
    tail_x: float                 # centre of the tail pair on the top edge
    row_exit: str = "right"       # side where row electrodes leave the sensing region
    params: Params = field(default_factory=Params)
    notes: str = ""
    front_tail_side: int = -1     # Front (column) tail at tail_x - tail_sep/2 (-1) or + tail_sep/2 (+1)

    def mirrored(self, name):
        return PadSpec(
            name=name,
            body=affinity.scale(self.body, xfact=-1, yfact=1, origin=(0, 0)),
            tail_x=-self.tail_x,
            row_exit="left" if self.row_exit == "right" else "right",
            params=self.params,
            notes=self.notes,
            front_tail_side=-self.front_tail_side,
        )


# --------------------------------------------------------------------------------------
# Small geometry helpers
# --------------------------------------------------------------------------------------
def arc_points(p0, p1, sagitta, n=32):
    """Circular arc from p0 to p1. sagitta > 0 bulges to the left of p0->p1.
    Returns the points strictly between p0 and p1."""
    p0 = np.asarray(p0, float)
    p1 = np.asarray(p1, float)
    d = p1 - p0
    L = float(np.linalg.norm(d))
    u = d / L
    nrm = np.array([-u[1], u[0]])
    s = abs(sagitta)
    if s < 1e-9:
        return []
    R = (L * L / 4 + s * s) / (2 * s)
    sign = 1.0 if sagitta > 0 else -1.0
    centre = (p0 + p1) / 2 - sign * nrm * (R - s)
    a0 = math.atan2(*(p0 - centre)[::-1])
    a1 = math.atan2(*(p1 - centre)[::-1])
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    pts = []
    for t in np.linspace(0, 1, n + 2)[1:-1]:
        a = a0 + t * da
        pts.append((centre[0] + R * math.cos(a), centre[1] + R * math.sin(a)))
    return pts


def opening(poly, r):
    """Round convex corners (erode then dilate)."""
    return poly.buffer(-r, join_style="round").buffer(r, join_style="round")


def closing(poly, r):
    """Round concave corners / fillet necks (dilate then erode)."""
    return poly.buffer(r, join_style="round").buffer(-r, join_style="round")


def polys_of(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def hole_free(geom):
    """Split polygons with holes into hole-free pieces (gr_poly / gerber regions have no holes)."""
    out = []
    for p in polys_of(geom):
        if not p.interiors:
            out.append(p)
            continue
        cx = Polygon(p.interiors[0]).representative_point().x
        minx, miny, maxx, maxy = p.bounds
        out += hole_free(p.intersection(box(minx - 1, miny - 1, cx, maxy + 1)))
        out += hole_free(p.intersection(box(cx, miny - 1, maxx + 1, maxy + 1)))
    return out


def ring_arc(ring: LineString, s_a: float, s_b: float, forward: bool):
    """Coordinates along a closed ring from parameter s_a to s_b, travelling in the
    direction of increasing parameter when forward=True, else decreasing."""
    L = ring.length
    if abs((s_b - s_a) % L) < 1e-9:
        p = ring.interpolate(s_a)
        return [(p.x, p.y)]

    def fwd(sa, sb):
        if sa <= sb:
            return list(substring(ring, sa, sb).coords)
        return list(substring(ring, sa, L).coords) + list(substring(ring, 0, sb).coords)

    return fwd(s_a, s_b) if forward else fwd(s_b, s_a)[::-1]


# --------------------------------------------------------------------------------------
# Pad outlines (mm, y up).  The tail(s) attach to the straight TOP edge at tail_x.
# --------------------------------------------------------------------------------------
def forearm_spec():
    """Underside-of-forearm wrap. Elbow end at the top (tail), wrist end at the bottom with a
    concave scoop so the pad clears the wrist crease ("stretches around for underside forces")."""
    L, w_elbow, w_wrist, scoop, r = 220.0, 200.0, 150.0, 15.0, 10.0
    pts = [(-w_elbow / 2, L / 2), (w_elbow / 2, L / 2), (w_wrist / 2, -L / 2)]
    pts += arc_points((w_wrist / 2, -L / 2), (-w_wrist / 2, -L / 2), -scoop)  # concave: bulges up into the pad
    pts += [(-w_wrist / 2, -L / 2)]
    body = closing(opening(Polygon(pts), r), r)
    return PadSpec("forearm", body, tail_x=0.0, row_exit="right",
                   params=Params(pitch=12.5),
                   notes="Forearm underside wrap, elbow end up. 220 mm long, 200 mm (elbow) to 150 mm (wrist) wide.")


def bicep_spec():
    """Upper-arm wrap, wider at the shoulder end so it reaches round to the lateral side.
    Shoulder end at the top (tail); concave scoop at the elbow end for the elbow crease."""
    L, w_shoulder, w_elbow, scoop, r = 220.0, 230.0, 200.0, 20.0, 12.0
    pts = [(-w_shoulder / 2, L / 2), (w_shoulder / 2, L / 2), (w_elbow / 2, -L / 2)]
    pts += arc_points((w_elbow / 2, -L / 2), (-w_elbow / 2, -L / 2), -scoop)   # concave elbow scoop
    pts += [(-w_elbow / 2, -L / 2)]
    body = closing(opening(Polygon(pts), r), r)
    return PadSpec("bicep", body, tail_x=0.0, row_exit="right",
                   params=Params(pitch=13.5),
                   notes="Bicep wrap, shoulder end up. 220 mm long, 230 mm (shoulder) to 200 mm (elbow) wide.")


def chest_left_spec():
    """Left half of the chest pad (viewer's left, x < 0). Sternum edge at x = -5 (10 mm gap
    between the halves), V notch in the top edge, convex outer (armpit) edge, tail at the
    shoulder on the flat outer part of the top edge."""
    r = 8.0
    pts = [(-5, 90), (-40, 90)]
    pts += [(-72, 52)]                                           # notch bottom (rounded below)
    pts += [(-98, 95), (-158, 95)]                               # flat shoulder segment (tail here)
    pts += arc_points((-158, 95), (-146, -92), -18)              # outer edge bulging outward (-x)
    pts += [(-146, -92)]
    pts += arc_points((-146, -92), (-5, -100), +8)               # bottom edge, slightly concave
    pts += [(-5, -100)]
    body = closing(opening(Polygon(pts), r), r)
    return PadSpec("chest_left", body, tail_x=-128.0, row_exit="left",
                   params=Params(pitch=12.0),
                   notes="Left chest half. ~165 x 195 mm, V neckline notch, 10 mm sternum gap to the right half.")


def all_specs():
    cl = chest_left_spec()
    return [forearm_spec(), bicep_spec(), cl, cl.mirrored("chest_right")]


# --------------------------------------------------------------------------------------
# Board construction
# --------------------------------------------------------------------------------------
@dataclass
class Channel:
    idx: int                 # electrode index (row or column number, 0-based)
    pos: float               # electrode coordinate (x for columns, y for rows)
    seg: LineString          # electrode strip centre line
    exit_pt: Point
    group: str = ""
    ring: int = -1
    slot: int = -1           # bus / connector slot 0..15 (left to right at the tail)
    via: tuple = None
    copper: Polygon = None   # electrode-side copper (strip + trace + via pad)
    copper_back: Polygon = None  # connector-side copper (via pad + track + connector pad)
    path: list = None
    back_path: list = None
    pin: int = -1            # connector pad number (filled in by kicad_post.py)


@dataclass
class Board:
    side: str                # "Front" or "Back"
    kind: str                # "col" or "row"
    cu_layer: str            # electrode copper layer
    mask_layer: str
    tail_x: float
    conn_layer: str = ""     # connector copper layer (the other one)
    outline: Polygon = None
    channels: list = field(default_factory=list)
    mask: list = field(default_factory=list)
    adhesive: list = field(default_factory=list)
    stiffener: Polygon = None
    tail: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    drc: dict = field(default_factory=dict)


def tail_layout(P: Params, y_top):
    """y levels of the tail (mm, y up), see Params."""
    half = P.max_ch // 2
    L = {"h": [y_top + P.fan_y0 + P.lane_pitch * (half - 1 - j) for j in range(half)]}   # outer jogs lowest
    L["yA"] = y_top + P.via_y0
    L["yB"] = L["yA"] + P.via_row_gap
    # connector side: the narrow (0.5 mm) end is at the pads, so the nesting mirrors the front: inner jogs lowest
    L["g"] = [L["yB"] + P.back_y0 + P.lane_pitch * j for j in range(half)]
    L["y_pad"] = L["g"][-1] + P.lane_pitch + P.conn_pad_size[1] / 2 + P.trace_gap
    L["conn_y"] = L["y_pad"] + P.conn_pad_offset
    L["y_tip"] = L["conn_y"] + P.conn_front + P.tail_tip_margin
    L["y_top"] = y_top
    return L


def make_grid(sensing, P: Params):
    minx, miny, maxx, maxy = sensing.bounds
    def positions(lo, hi):
        span = hi - lo - P.electrode_w - 2.0
        n = min(P.max_ch, int(span // P.pitch) + 1)
        c = (lo + hi) / 2
        return [c + (i - (n - 1) / 2) * P.pitch for i in range(n)]
    return positions(minx, maxx), positions(miny, maxy)


def electrodes(sensing, P: Params, kind, positions, row_exit):
    """Clip grid lines to the opening (inset by half the strip width) -> electrode strips."""
    region = sensing.buffer(-(P.electrode_w / 2 + 0.5))
    minx, miny, maxx, maxy = sensing.bounds
    out = []
    for i, c in enumerate(positions):
        if kind == "col":
            line = LineString([(c, miny - 5), (c, maxy + 5)])
        else:
            line = LineString([(minx - 5, c), (maxx + 5, c)])
        inter = line.intersection(region)
        pieces = [inter] if isinstance(inter, LineString) else [g for g in getattr(inter, "geoms", []) if isinstance(g, LineString)]
        pieces = [g for g in pieces if g.length >= 1.5 * P.pitch]
        if not pieces:
            continue
        # a scoop or notch may split the line: keep the piece nearest the exit side
        if kind == "col":
            key = lambda p: max(p.coords[0][1], p.coords[-1][1])            # columns leave at the top
        elif row_exit == "right":
            key = lambda p: max(p.coords[0][0], p.coords[-1][0])
        else:
            key = lambda p: -min(p.coords[0][0], p.coords[-1][0])
        seg = max(pieces, key=key)
        a, b = seg.coords[0], seg.coords[-1]
        if kind == "col":
            exit_pt = max((a, b), key=lambda p: p[1])
        elif row_exit == "right":
            exit_pt = max((a, b), key=lambda p: p[0])
        else:
            exit_pt = min((a, b), key=lambda p: p[0])
        out.append(Channel(idx=i, pos=c, seg=seg, exit_pt=Point(exit_pt)))
    return out


def route_board(board: Board, sensing, y_top, P: Params):
    """Assign lanes/slots and build copper.  Traces follow offset contours of the opening:
    the exit closest to the tail (along the contour) takes the innermost lane; lanes fan
    into the 0.5 mm bus without crossing, then a staircase fans the bus out to 1.0 mm,
    two staggered via rows take the signals to the other side, and a mirrored staircase
    brings them back to the 0.5 mm pads of the FFC connector."""
    chans = board.channels
    tail_x = board.tail_x
    T = tail_layout(P, y_top)
    board.tail = T
    n_f = P.max_ch
    half = n_f // 2

    ring0 = LineString(sensing.buffer(P.ring0, join_style="round").exterior.coords)
    Lr = ring0.length
    s_tail = ring0.project(Point(tail_x, y_top))
    # direction of travel along the ring (increasing parameter) at the tail entry point
    pa = ring0.interpolate((s_tail - 0.5) % Lr)
    pb = ring0.interpolate((s_tail + 0.5) % Lr)
    t_fwd_x = pb.x - pa.x

    for ch in chans:
        s = ring0.project(ch.exit_pt)
        d_f = (s_tail - s) % Lr            # distance to the tail travelling forward
        ch._forward = d_f <= Lr - d_f      # take the short way round
        ch._dist = min(d_f, Lr - d_f)
        arrive_x = t_fwd_x if ch._forward else -t_fwd_x
        ch.group = "L" if arrive_x > 0 else "R"   # moving +x at the tail = arriving from the left
        if board.kind == "col" and abs(ch.exit_pt.x - tail_x) < (P.max_ch / 2) * P.lane_pitch + 1.5:
            raise ValueError(f"{board.side}: electrode {ch.idx} leaves the opening under the tail bus "
                             f"(x={ch.exit_pt.x:.1f}, tail at {tail_x:.1f}); move the tail or the grid")

    groupL = sorted([c for c in chans if c.group == "L"], key=lambda c: c._dist)
    groupR = sorted([c for c in chans if c.group == "R"], key=lambda c: c._dist)
    nL = len(groupL)
    if nL + len(groupR) > n_f:
        raise ValueError(f"{board.side}: {nL + len(groupR)} electrodes exceed {n_f} connector pins")
    for k, ch in enumerate(groupL):
        ch.ring, ch.slot = k, nL - 1 - k           # arrives from the left -> innermost takes the right-most L slot
    for k, ch in enumerate(groupR):
        ch.ring, ch.slot = k, nL + k               # arrives from the right -> innermost takes the left-most R slot

    bus_x = lambda k: tail_x + (k - (n_f - 1) / 2) * P.lane_pitch
    via_x = lambda k: tail_x + (k - (n_f - 1) / 2) * P.via_pitch
    jog = lambda k: int(abs(k - (n_f - 1) / 2) - 0.5)        # 0 = innermost .. half-1 = outermost
    pw, pl = P.conn_pad_size

    rings = {}
    for ch in chans:
        if ch.ring not in rings:
            rings[ch.ring] = LineString(sensing.buffer(P.ring0 + ch.ring * P.lane_pitch, join_style="round").exterior.coords)
        ring = rings[ch.ring]
        k = ch.slot
        j = jog(k)
        bx, vx = bus_x(k), via_x(k)
        vy = T["yA"] if k % 2 == 0 else T["yB"]
        s_in = ring.project(ch.exit_pt)
        s_out = ring.project(Point(bx, y_top))
        arc = ring_arc(ring, s_in, s_out, ch._forward)
        p_out = arc[-1]
        path = [(ch.exit_pt.x, ch.exit_pt.y)] + arc + [(bx, p_out[1]), (bx, T["h"][j]), (vx, T["h"][j]), (vx, vy)]
        ch.path = path
        ch.via = (vx, vy)
        ch.back_path = [(vx, vy), (vx, T["g"][j]), (bx, T["g"][j]), (bx, T["y_pad"])]
        strip = ch.seg.buffer(P.electrode_w / 2, cap_style="round")
        trace = LineString(path).buffer(P.trace_w / 2, cap_style="flat", join_style="round")
        via_pad = Point(vx, vy).buffer(P.via_d / 2)
        ch.copper = unary_union([strip, trace, via_pad]).buffer(0)
        back = LineString(ch.back_path).buffer(P.trace_w / 2, cap_style="flat", join_style="round")
        pad = box(bx - pw / 2, T["y_pad"] - pl / 2, bx + pw / 2, T["y_pad"] + pl / 2)
        ch.copper_back = unary_union([via_pad, back, pad]).buffer(0)

    # unconnected connector pads (fewer than 16 electrodes) only exist on the connector side
    used = {c.slot for c in chans}
    board.unused_pads = [box(bus_x(k) - pw / 2, T["y_pad"] - pl / 2, bus_x(k) + pw / 2, T["y_pad"] + pl / 2)
                         for k in range(n_f) if k not in used]
    # coverlay opening on the electrode side: the sensing region only (vias stay tented)
    board.mask = [sensing]
    # stiffener under the via field and the connector (Eco2.User)
    board.stiffener = box(tail_x - P.tail_w / 2, y_top + P.stiffener_from, tail_x + P.tail_w / 2, T["y_tip"])


def make_outline(body, tail_x, y_top, P: Params, y_tip):
    tail = box(tail_x - P.tail_w / 2, y_top - 3.0, tail_x + P.tail_w / 2, y_tip)
    tail = opening(tail, P.tail_tip_r)
    return closing(unary_union([body, tail]), 2.0)   # 2 mm fillet at the tail neck


def inner_cuts(sensing, xs, ys, copper_all, P: Params):
    keep_in = sensing.buffer(-P.cut_margin)
    forbidden = copper_all.buffer(P.cut_clear, join_style="round")
    w = P.web
    cuts = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cell = box(xs[i] + w / 2, ys[j] + w / 2, xs[i + 1] - w / 2, ys[j + 1] - w / 2)
            if cell.is_empty or cell.area < 4:
                continue
            cell = opening(cell, P.cut_r)
            g = cell.intersection(keep_in).difference(forbidden)
            for p in polys_of(g):
                if p.buffer(-1.2).is_empty or p.area < 12:
                    continue
                cuts.append(opening(p, 1.0).simplify(0.02))
    return [c for c in cuts if not c.is_empty]


def check_board(board: Board, cuts, P: Params):
    """Self-contained design-rule check with shapely (kicad-cli DRC is run separately)."""
    def min_pair(items):
        best, worst = 1e9, None
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                d = items[a][1].distance(items[b][1])
                if d < best:
                    best, worst = d, (items[a][0], items[b][0])
        return best, worst

    front = [(f"ch{c.idx}", c.copper) for c in board.channels]
    back = [(f"ch{c.idx}", c.copper_back) for c in board.channels] + [(f"nc{k}", p) for k, p in enumerate(board.unused_pads)]
    f_clear, f_worst = min_pair(front)
    b_clear, b_worst = min_pair(back)
    all_cu = unary_union([g for _, g in front] + [g for _, g in back])
    edge_clear = all_cu.distance(board.outline.exterior)
    cut_clear = min([all_cu.distance(c) for c in cuts], default=1e9)
    inside = all_cu.difference(board.outline).area < 1e-6
    board.checks = {
        "electrode_side_clearance_mm": round(f_clear, 4), "electrode_side_worst": f_worst,
        "connector_side_clearance_mm": round(b_clear, 4), "connector_side_worst": b_worst,
        "copper_to_edge_mm": round(edge_clear, 4),
        "copper_to_cut_mm": round(cut_clear, 4),
        "copper_inside_outline": bool(inside),
        "ok": bool(min(f_clear, b_clear) >= P.trace_gap - 0.06 and edge_clear >= P.edge_clear - 0.05
                   and cut_clear >= P.cut_clear - 0.05 and inside),
    }
    return board.checks


def build_pad(spec: PadSpec):
    P = spec.params
    body = spec.body
    y_top = body.bounds[3]
    sensing = opening(body.buffer(-P.margin, join_style="round"), P.opening_r)
    xs, ys = make_grid(sensing, P)
    T = tail_layout(P, y_top)

    front_tail_x = spec.tail_x + spec.front_tail_side * P.tail_sep / 2
    back_tail_x = spec.tail_x - spec.front_tail_side * P.tail_sep / 2
    # the column electrodes leave the opening at the top: shift the column grid so that a gap
    # between two columns sits under the Front tail's 0.5 mm bus (columns must not exit there)
    gaps = [(a + b) / 2 for a, b in zip(xs[:-1], xs[1:])] or [xs[0]]
    nearest_gap = min(gaps, key=lambda g: abs(g - front_tail_x))
    xs = [x + (front_tail_x - nearest_gap) for x in xs]
    for tx in (front_tail_x, back_tail_x):
        for dx in (-(P.tail_w / 2 + 2.0), P.tail_w / 2 + 2.0):
            if not body.contains(Point(tx + dx, y_top - 0.3)):
                raise ValueError(f"{spec.name}: top edge must be straight around the tail at x={tx:.1f}")

    front = Board("Front", "col", "F.Cu", "F.Mask", tail_x=front_tail_x, conn_layer="B.Cu")
    back = Board("Back", "row", "B.Cu", "B.Mask", tail_x=back_tail_x, conn_layer="F.Cu")
    front.channels = electrodes(sensing, P, "col", xs, spec.row_exit)
    back.channels = electrodes(sensing, P, "row", ys, spec.row_exit)
    for b in (front, back):
        b.outline = make_outline(body, b.tail_x, y_top, P, T["y_tip"])
        route_board(b, sensing, y_top, P)
        b.adhesive = hole_free(body.difference(sensing))

    copper_all = unary_union([c.copper for c in front.channels + back.channels])
    cuts = inner_cuts(sensing, [c.pos for c in front.channels], [c.pos for c in back.channels], copper_all, P)
    for b in (front, back):
        check_board(b, cuts, P)

    taxels = 0
    for c in front.channels:
        for r in back.channels:
            if c.seg.buffer(P.electrode_w / 2).intersects(r.seg.buffer(P.electrode_w / 2)):
                taxels += 1
    return dict(spec=spec, body=body, sensing=sensing, front=front, back=back, cuts=cuts,
                taxels=taxels, xs=xs, ys=ys, y_top=y_top, tail=T)


# --------------------------------------------------------------------------------------
# Exporters
# --------------------------------------------------------------------------------------
class Frame:
    """Shared mm->file transform for one pad (y flipped, everything positive)."""
    def __init__(self, pad):
        geoms = [pad["front"].outline, pad["back"].outline]
        minx = min(g.bounds[0] for g in geoms)
        maxy = max(g.bounds[3] for g in geoms)
        self.x0 = minx - 10.0
        self.y1 = maxy + 10.0

    def mm(self, coords):
        return [(x - self.x0, self.y1 - y) for x, y in coords]

    def px(self, coords):
        return [((x - self.x0) * MM2PX, (self.y1 - y) * MM2PX) for x, y in coords]


def write_svg(pad, board: Board, frame: Frame, path):
    """Same group/layer convention as handFront.svg so the notebook's create_kicad_mod() works.
    (Electrode-side layers only; vias and the connector are added to the .kicad_pcb.)"""
    import svgwrite
    dwg = svgwrite.Drawing(path)
    g_cu = dwg.g(id=board.cu_layer, fill="black", stroke="none")
    g_edge = dwg.g(id="Edge.Cuts", fill="gray", stroke="none")
    g_mask = dwg.g(id=board.mask_layer, fill="black", stroke="none")
    g_adh = dwg.g(id="Eco1.User", fill="black", stroke="none")
    g_stf = dwg.g(id="Eco2.User", fill="black", stroke="none")

    for ch in board.channels:
        for p in polys_of(ch.copper):
            g_cu.add(dwg.polygon(points=frame.px(p.exterior.coords), fill="black", stroke="none"))
    g_edge.add(dwg.polygon(points=frame.px(board.outline.exterior.coords), fill="black", stroke="none"))
    for c in pad["cuts"]:
        g_edge.add(dwg.polygon(points=frame.px(c.exterior.coords), fill="black", stroke="none"))
    for m in board.mask:
        g_mask.add(dwg.polygon(points=frame.px(m.exterior.coords), fill="black", stroke="none"))
    adh = unary_union(board.adhesive)
    for p in polys_of(adh):
        d = "M " + " L ".join(f"{x},{y}" for x, y in frame.px(p.exterior.coords)) + " Z"
        for hole in p.interiors:
            d += " M " + " L ".join(f"{x},{y}" for x, y in frame.px(hole.coords)) + " Z"
        g_adh.add(dwg.path(d=d, fill="black", stroke="none"))
    g_stf.add(dwg.polygon(points=frame.px(board.stiffener.exterior.coords), fill="black", stroke="none"))
    for g in (g_cu, g_edge, g_mask, g_adh, g_stf):
        dwg.add(g)
    dwg.save()


def svg_to_kicad_mod(svg_path, mod_path):
    """Replicates create_kicad_mod() from make_fpcbs.ipynb (inkscape labels + svg2mod)."""
    from lxml import etree
    from svg2mod.exporter import DEFAULT_DPI, Svg2ModExportLatest
    from svg2mod.importer import Svg2ModImport

    tree = etree.parse(svg_path)
    root = tree.getroot()
    ns = "http://www.inkscape.org/namespaces/inkscape"
    for g in root.findall(".//{http://www.w3.org/2000/svg}g"):
        if g.get("id"):
            g.set(f"{{{ns}}}label", g.get("id"))
    labeled = svg_path.replace(".svg", "New.svg")
    tree.write(labeled, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    imported = Svg2ModImport(labeled, "svg2mod", "G***", True, None)
    exported = Svg2ModExportLatest(imported, mod_path, False, 1.0, 5.0, dpi=DEFAULT_DPI, pads=False)
    exported.write("make_body_pads.py")


KICAD_LAYERS = """    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)"""


def kicad_poly(coords, layer, fill=True, width=0.0, net=None):
    pts = " ".join(f"(xy {x:.4f} {y:.4f})" for x, y in coords)
    net_s = f" (net {net})" if net is not None else ""
    fill_s = "solid" if fill else "none"
    return f'  (gr_poly (pts {pts}) (stroke (width {width}) (type solid)) (fill {fill_s}) (layer "{layer}"){net_s})\n'


def write_kicad_pcb(pad, board: Board, frame: Frame, path):
    spec = pad["spec"]
    lines = ['(kicad_pcb (version 20240108) (generator "make_body_pads") (generator_version "8.0")\n',
             "  (general (thickness 0.11) (legacy_teardrops no))\n",
             '  (paper "A2")\n',
             f'  (title_block (title "{spec.name} {board.side} FPCB") (comment 1 "{spec.notes}"))\n',
             f"  (layers\n{KICAD_LAYERS}\n  )\n",
             "  (setup (pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no)\n"
             "    (pcbplotparams (layerselection 0x00010fc_ffffffff) (plot_on_all_layers_selection 0x0000000_00000000)\n"
             "      (disableapertmacros no) (usegerberextensions yes) (usegerberattributes yes) (usegerberadvancedattributes yes)\n"
             "      (creategerberjobfile yes) (dashed_line_dash_ratio 12.0) (dashed_line_gap_ratio 3.0) (svgprecision 4)\n"
             "      (plotframeref no) (viasonmask no) (mode 1) (useauxorigin no) (hpglpennumber 1) (hpglpenspeed 20)\n"
             "      (hpglpendiameter 15.0) (pdf_front_fp_property_popups yes) (pdf_back_fp_property_popups yes)\n"
             "      (dxfpolygonmode yes) (dxfimperialunits yes) (dxfusepcbnewfont yes) (psnegative no) (psa4output no)\n"
             "      (plotreference yes) (plotvalue yes) (plotfptext yes) (plotinvisibletext no) (sketchpadsonfab no)\n"
             "      (subtractmaskfromsilk no) (outputformat 1) (mirror no) (drillshape 1) (scaleselection 1) (outputdirectory \"\"))\n"
             "  )\n",
             '  (net 0 "")\n']
    for i, ch in enumerate(board.channels):
        lines.append(f'  (net {i + 1} "{net_name(board, ch)}")\n')

    # electrode-side copper (the via pad itself is added as a real via by kicad_post.py)
    for i, ch in enumerate(board.channels):
        for p in hole_free(ch.copper):
            lines.append(kicad_poly(frame.mm(p.exterior.coords), board.cu_layer, net=i + 1))
    # coverlay openings on the electrode side
    for m in board.mask:
        for p in hole_free(m):
            lines.append(kicad_poly(frame.mm(p.exterior.coords), board.mask_layer))
    # adhesive
    for p in board.adhesive:
        lines.append(kicad_poly(frame.mm(p.exterior.coords), "Eco1.User"))
    # stiffener
    lines.append(kicad_poly(frame.mm(board.stiffener.exterior.coords), "Eco2.User"))
    # edge cuts: outline + inner cut-outs
    lines.append(kicad_poly(frame.mm(board.outline.exterior.coords), "Edge.Cuts", fill=False, width=0.05))
    for c in pad["cuts"]:
        lines.append(kicad_poly(frame.mm(c.exterior.coords), "Edge.Cuts", fill=False, width=0.05))
    # fab notes
    x0, y0 = frame.mm([(board.outline.bounds[0], board.outline.bounds[1])])[0]
    text = (f"{spec.name} {board.side}: 2-layer flex. Electrodes on {board.cu_layer} under the {board.mask_layer} "
            f"opening; FFC connector J1 on {board.conn_layer}. Eco1.User = PSA adhesive on the {board.cu_layer} side. "
            f"Eco2.User = stiffener under the connector (on the {board.cu_layer} side of the tail).")
    lines.append(f'  (gr_text "{text}" (at {x0:.2f} {y0 + 6:.2f}) (layer "Cmts.User") (effects (font (size 2 2) (thickness 0.3)) (justify left)))\n')
    lines.append(")\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def net_name(board: Board, ch: Channel):
    return f"{board.kind.upper()}{ch.idx:02d}"


def write_kicad_pro(path, P: Params):
    """Project file with flex design rules (kicad-cli drc reads it from next to the board).
    Mask bridges between electrodes are the sensing opening, so that check is ignored."""
    pro = {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": round(P.trace_gap - 0.05, 3),
                    "min_connection": 0.0,
                    "min_copper_edge_clearance": round(P.edge_clear - 0.05, 3),
                    "min_hole_clearance": 0.2,
                    "min_hole_to_hole": 0.25,
                    "min_microvia_diameter": 0.2,
                    "min_microvia_drill": 0.1,
                    "min_resolved_spokes": 0,
                    "min_silk_clearance": 0.0,
                    "min_text_height": 0.8,
                    "min_text_thickness": 0.08,
                    "min_through_hole_diameter": round(P.via_drill - 0.05, 3),
                    "min_track_width": round(P.trace_w - 0.05, 3),
                    "min_via_annular_width": 0.1,
                    "min_via_diameter": round(P.via_d - 0.05, 3),
                    "solder_mask_to_copper_clearance": 0.0,
                    "use_height_for_length_calcs": True,
                },
                "rule_severities": {"solder_mask_bridge": "ignore"},
            },
        },
        "meta": {"filename": os.path.basename(path), "version": 1},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pro, f, indent=2)


def write_tail_json(pad, board: Board, frame: Frame, path, kicad_root):
    """Everything kicad_post.py needs (KiCad frame, mm)."""
    P = pad["spec"].params
    T = board.tail
    conn = frame.mm([(board.tail_x, T["conn_y"])])[0]
    label = frame.mm([(board.tail_x, T["y_top"] + 2.0)])[0]
    data = {
        "cu_layer": board.cu_layer,
        "conn_side": "B" if board.conn_layer == "B.Cu" else "F",
        "footprint_lib": os.path.join(kicad_root, "share", "kicad", "footprints", P.conn_lib),
        "footprint": P.conn_footprint,
        "reference": "J1",
        "conn_pos": list(conn),
        "rotation": 0 if board.conn_layer == "B.Cu" else 180,
        "via_d": P.via_d, "via_drill": P.via_drill, "track_w": P.trace_w,
        "label": f"{pad['spec'].name} {board.side}",
        "label_pos": list(label),
        "channels": [{"net": net_name(board, ch), "via": list(frame.mm([ch.via])[0]),
                      "back_path": frame.mm(ch.back_path)} for ch in board.channels],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def find_kicad_cli(explicit=None):
    for c in ([explicit] if explicit else []) + KICAD_CLI_CANDIDATES:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    return None


def kicad_paths(kicad_cli):
    bindir = os.path.dirname(os.path.abspath(shutil.which(kicad_cli) or kicad_cli))
    root = os.path.dirname(bindir)
    py = os.path.join(bindir, "python.exe" if os.name == "nt" else "python3")
    if not os.path.isfile(py):
        py = os.path.join(root, "Frameworks", "Python.framework", "Versions", "Current", "bin", "python3")
    return root, py


def run_kicad_post(kicad_py, pcb_path, json_path):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kicad_post.py")
    r = subprocess.run([kicad_py, script, pcb_path, json_path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"kicad_post.py failed:\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def export_gerbers(kicad_cli, pcb_path, out_dir):
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    cmd = [kicad_cli, "pcb", "export", "gerbers", "--output", out_dir, "--layers", GERBER_LAYERS,
           "--no-protel-ext", "--no-netlist", pcb_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli gerbers failed:\n{r.stdout}\n{r.stderr}")
    cmd = [kicad_cli, "pcb", "export", "drill", "--output", out_dir + os.sep, "--format", "excellon",
           "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2", pcb_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli drill failed:\n{r.stdout}\n{r.stderr}")
    prl = os.path.splitext(pcb_path)[0] + ".kicad_prl"   # kicad-cli side effect
    if os.path.isfile(prl):
        os.remove(prl)
    zip_path = out_dir.rstrip("\\/") + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in sorted(os.listdir(out_dir)):
            z.write(os.path.join(out_dir, fn), fn)
    return sorted(os.listdir(out_dir)), zip_path


def run_drc(kicad_cli, pcb_path, report_path):
    """KiCad DRC; 'solder_mask_bridge' between electrodes is the sensing opening and is expected."""
    cmd = [kicad_cli, "pcb", "drc", "--output", report_path, "--severity-all", "--format", "json", pcb_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    prl = os.path.splitext(pcb_path)[0] + ".kicad_prl"
    if os.path.isfile(prl):
        os.remove(prl)
    if not os.path.isfile(report_path):
        raise RuntimeError(f"kicad-cli drc failed:\n{r.stdout}\n{r.stderr}")
    with open(report_path, encoding="utf-8") as f:
        rep = json.load(f)
    counts = {}
    for v in rep.get("violations", []):
        counts[v["type"]] = counts.get(v["type"], 0) + 1
    other = {k: n for k, n in counts.items() if k != "solder_mask_bridge"}
    return {"violations": counts, "unconnected": len(rep.get("unconnected_items", [])),
            "schematic_parity": len(rep.get("schematic_parity", [])), "ok": not other}


def write_cut_template(pad, path):
    """Body outline + inner cuts in real mm units for laser cutting Velostat / silicone."""
    body = pad["body"]
    minx, miny, maxx, maxy = body.bounds
    W, H = maxx - minx + 10, maxy - miny + 10
    def tr(coords):
        return " ".join(f"{x - minx + 5:.3f},{maxy - y + 5:.3f}" for x, y in coords)
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="{W:.2f}mm" height="{H:.2f}mm" viewBox="0 0 {W:.2f} {H:.2f}">\n',
             f'<g fill="none" stroke="#000" stroke-width="0.1">\n<polygon points="{tr(body.exterior.coords)}"/>\n']
    for c in pad["cuts"]:
        parts.append(f'<polygon points="{tr(c.exterior.coords)}"/>\n')
    parts.append(f'<polygon points="{tr(pad["sensing"].exterior.coords)}" stroke="#c00" stroke-dasharray="2,1"/>\n')
    parts.append("</g>\n</svg>\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(parts)


def write_pinout(board: Board, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["J1 pin", "channel", "electrode_index", "electrode_pos_mm", "lane", "via_row"])
        rows = sorted(board.channels, key=lambda c: c.pin if c.pin > 0 else 99)
        for c in rows:
            w.writerow([c.pin if c.pin > 0 else "?", net_name(board, c), c.idx, f"{c.pos:.2f}", c.ring, "A" if c.slot % 2 == 0 else "B"])
        used = {c.pin for c in board.channels}
        for k in range(1, 17):
            if k not in used:
                w.writerow([k, "NC", "", "", "", ""])


def render(pad, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.path import Path

    def patch(geom, **kw):
        verts, codes = [], []
        for p in polys_of(geom):
            for ring in [p.exterior, *p.interiors]:
                c = list(ring.coords)
                verts += c
                codes += [Path.MOVETO] + [Path.LINETO] * (len(c) - 2) + [Path.CLOSEPOLY]
        return PathPatch(Path(verts, codes), **kw)

    spec = pad["spec"]
    P = spec.params
    front, back = pad["front"], pad["back"]
    fig = plt.figure(figsize=(24, 13))
    gs = fig.add_gridspec(2, 3, height_ratios=[3.2, 1])
    axes = [fig.add_subplot(gs[0, i]) for i in range(3)]
    panels = [(axes[0], [front], "Front FPCB (bottom board, columns on F.Cu, J1 on B.Cu)"),
              (axes[1], [back], "Back FPCB (top board, rows on B.Cu, J1 on F.Cu)"),
              (axes[2], [front, back], "Stack (both boards overlaid)")]
    colors = {"Front": "#d62728", "Back": "#1f77b4"}
    for ax, boards, title in panels:
        for b in boards:
            ax.add_patch(patch(b.outline, facecolor="#f3e9c9", edgecolor="k", lw=1.2, alpha=0.9 if len(boards) == 1 else 0.5))
        b0 = boards[0]
        ax.add_patch(patch(unary_union(b0.adhesive), facecolor="#2c2c6e", edgecolor="none", alpha=0.35, label="adhesive (Eco1)"))
        ax.add_patch(patch(pad["sensing"], facecolor="#ffc0e0", edgecolor="none", alpha=0.6, label="coverlay opening"))
        for c in pad["cuts"]:
            ax.add_patch(patch(c, facecolor="white", edgecolor="k", lw=0.4))
        for b in boards:
            cu = unary_union([c.copper for c in b.channels])
            ax.add_patch(patch(cu, facecolor=colors[b.side], edgecolor="none", alpha=0.85 if len(boards) == 1 else 0.7,
                               label=f"{b.side} copper ({b.kind}s)"))
            ax.add_patch(patch(b.stiffener, facecolor="none", edgecolor="green", lw=1.0, ls="--"))
        ax.set_aspect("equal")
        minx, miny, maxx, maxy = unary_union([b.outline for b in boards]).bounds
        ax.set_xlim(minx - 5, maxx + 5)
        ax.set_ylim(miny - 5, maxy + 5)
        ax.set_title(title)
        ax.set_xlabel("mm")
        ax.grid(True, lw=0.3, alpha=0.5)
        ax.legend(loc="lower right", fontsize=8)

    # tail close-ups (also saved on their own at a readable scale)
    def draw_tail(ax, b):
        T = b.tail
        ax.add_patch(patch(b.outline, facecolor="#f3e9c9", edgecolor="k", lw=1.0))
        ax.add_patch(patch(b.stiffener, facecolor="none", edgecolor="green", lw=1.0, ls="--", label="stiffener (Eco2)"))
        ax.add_patch(patch(unary_union([c.copper for c in b.channels]), facecolor=colors[b.side], edgecolor="none", alpha=0.85,
                           label=f"{b.cu_layer} electrode side"))
        ax.add_patch(patch(unary_union([c.copper_back for c in b.channels] + b.unused_pads), facecolor="#2ca02c", edgecolor="none",
                           alpha=0.6, label=f"{b.conn_layer} connector side"))
        for c in b.channels:
            ax.add_patch(patch(Point(c.via).buffer(P.via_drill / 2), facecolor="white", edgecolor="k", lw=0.3))
        ax.add_patch(Rectangle((b.tail_x - P.conn_half_w, T["conn_y"] - P.conn_back), 2 * P.conn_half_w, P.conn_back + P.conn_front,
                               facecolor="none", edgecolor="#555", lw=1.0, ls=":", label="J1 courtyard"))
        ax.set_xlim(b.tail_x - P.tail_w / 2 - 3, b.tail_x + P.tail_w / 2 + 3)
        ax.set_ylim(T["y_top"] - 12, T["y_tip"] + 2)
        ax.set_aspect("equal")
        ax.set_title(f"{b.side} tail: 0.5 mm bus -> vias -> FFC pads on {b.conn_layer}")
        ax.grid(True, lw=0.3, alpha=0.5)
        ax.legend(loc="lower left", fontsize=7)

    tail_fig, tail_axes = plt.subplots(1, 2, figsize=(14, 9))
    for i, b in enumerate((front, back)):
        draw_tail(fig.add_subplot(gs[1, i]), b)
        draw_tail(tail_axes[i], b)
    tail_fig.suptitle(f"{spec.name} tails: 0.5 mm bus -> staircase to 1.0 mm -> vias -> staircase -> {P.conn_footprint}", fontsize=11)
    tail_fig.tight_layout()
    tail_fig.savefig(path.replace("_preview.png", "_tail.png"), dpi=110)
    plt.close(tail_fig)
    T = back.tail
    ax = fig.add_subplot(gs[1, 2])
    ax.axis("off")
    ax.text(0, 1, "\n".join([
        f"Taxel pitch {P.pitch} mm, electrode strips {P.electrode_w} mm, traces {P.trace_w}/{P.trace_gap} mm",
        f"Vias {P.via_d}/{P.via_drill} mm in two rows at {P.via_pitch} mm pitch",
        f"Connector: {P.conn_footprint}",
        f"Tail {P.tail_w} x {T['y_tip'] - T['y_top']:.1f} mm, stiffener from +{P.stiffener_from} mm",
        "Layers: F.Cu B.Cu F/B.Mask F/B.Paste F/B.SilkS Edge.Cuts Eco1 (adhesive) Eco2 (stiffener) + PTH drill",
    ]), va="top", fontsize=10, family="monospace")
    W = pad["body"].bounds[2] - pad["body"].bounds[0]
    H = pad["body"].bounds[3] - pad["body"].bounds[1]
    fig.suptitle(f"{spec.name}: body {W:.0f} x {H:.0f} mm, {len(front.channels)} columns x {len(back.channels)} rows, "
                 f"{pad['taxels']} taxels at {spec.params.pitch} mm pitch, {len(pad['cuts'])} inner cut-outs", fontsize=14)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)


# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pads", nargs="*", default=["forearm", "bicep", "chest_left", "chest_right"])
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    ap.add_argument("--kicad-cli", default=None)
    ap.add_argument("--no-kicad", action="store_true", help="skip KiCad (no vias/connector/gerbers; still writes SVG/kicad_mod/kicad_pcb)")
    ap.add_argument("--no-mod", action="store_true", help="skip the svg2mod footprint step")
    args = ap.parse_args()

    specs = {s.name: s for s in all_specs()}
    kicad_cli = None if args.no_kicad else find_kicad_cli(args.kicad_cli)
    if not args.no_kicad and not kicad_cli:
        print("kicad-cli not found: vias, connector and gerbers skipped (use --kicad-cli PATH)", file=sys.stderr)
    kicad_root, kicad_py = kicad_paths(kicad_cli) if kicad_cli else (None, None)
    summary = {}
    for name in args.pads:
        spec = specs[name]
        print(f"== {name}")
        pad = build_pad(spec)
        frame = Frame(pad)
        out = os.path.join(args.out, name)
        os.makedirs(out, exist_ok=True)
        info = {"notes": spec.notes, "params": vars(spec.params), "taxels": pad["taxels"],
                "columns": len(pad["front"].channels), "rows": len(pad["back"].channels),
                "inner_cuts": len(pad["cuts"]), "boards": {}}
        for b in (pad["front"], pad["back"]):
            base = os.path.join(out, f"{name}_{b.side}")
            write_svg(pad, b, frame, base + ".svg")
            write_kicad_pcb(pad, b, frame, base + ".kicad_pcb")
            write_kicad_pro(base + ".kicad_pro", spec.params)
            if not args.no_mod:
                svg_to_kicad_mod(base + ".svg", base + ".kicad_mod")
            binfo = {"checks": b.checks, "outline_mm": [round(v, 1) for v in b.outline.bounds],
                     "size_mm": [round(b.outline.bounds[2] - b.outline.bounds[0], 1), round(b.outline.bounds[3] - b.outline.bounds[1], 1)],
                     "channels": len(b.channels)}
            if kicad_cli:
                write_tail_json(pad, b, frame, base + "_tail.json", kicad_root)
                pins = run_kicad_post(kicad_py, base + ".kicad_pcb", base + "_tail.json")
                for ch in b.channels:
                    ch.pin = pins["pads"].get(net_name(b, ch), -1)
                files, zp = export_gerbers(kicad_cli, base + ".kicad_pcb", os.path.join(out, f"gerbers_{b.side}"))
                b.drc = run_drc(kicad_cli, base + ".kicad_pcb", os.path.join(out, f"drc_{b.side}.json"))
                binfo.update({"gerbers": files, "zip": os.path.basename(zp), "kicad_drc": b.drc})
            write_pinout(b, os.path.join(out, f"pinout_{b.side}.csv"))
            info["boards"][b.side] = binfo
            print(f"   {b.side}: {len(b.channels)} ch, size {binfo['size_mm']} mm, checks {b.checks}")
            if b.drc:
                print(f"   {b.side}: KiCad DRC {b.drc}")
        write_cut_template(pad, os.path.join(out, f"{name}_cut_template.svg"))
        render(pad, os.path.join(out, f"{name}_preview.png"))
        with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, default=str)
        summary[name] = info
        print(f"   {pad['taxels']} taxels, {len(pad['cuts'])} cut-outs")
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)


if __name__ == "__main__":
    main()
