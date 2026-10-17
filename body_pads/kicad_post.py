"""
kicad_post.py - run with KiCad's own python (…\\KiCad\\10.0\\bin\\python.exe).

Finishes a board written by make_body_pads.py the way README Step 5 does by hand:
adds the through vias at the end of every electrode trace, the FFC connector footprint
from KiCad's library on the side opposite the electrodes, the short tracks from the
vias to the connector pads, assigns nets, adds a silkscreen label and saves the board.
Prints a JSON pin map (net -> connector pad number) on stdout.

usage: python kicad_post.py board.kicad_pcb tail.json
"""
import json
import os
import sys

import pcbnew

pcb_path, json_path = sys.argv[1], sys.argv[2]
with open(json_path, encoding="utf-8") as f:
    D = json.load(f)

board = pcbnew.LoadBoard(pcb_path)
mm = pcbnew.FromMM
V = lambda x, y: pcbnew.VECTOR2I(mm(x), mm(y))
layer_id = {"F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu}


def net_of(name):
    net = board.FindNet(name)
    if net is None:
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
    return net


# ---- connector footprint ---------------------------------------------------------------
fp = pcbnew.FootprintLoad(D["footprint_lib"], D["footprint"])
if fp is None:
    sys.exit(f"footprint {D['footprint']} not found in {D['footprint_lib']}")
fp.SetReference(D.get("reference", "J1"))
fp.SetValue(D["footprint"])
board.Add(fp)
cx, cy = D["conn_pos"]
fp.SetPosition(V(cx, cy))
if D["conn_side"] == "B":
    try:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    except AttributeError:
        fp.Flip(fp.GetPosition(), False)
try:
    fp.SetOrientation(pcbnew.EDA_ANGLE(D["rotation"], pcbnew.DEGREES_T))
except (AttributeError, TypeError):
    fp.SetOrientationDegrees(D["rotation"])

# the signal pads must sit on the body side of the connector (larger y in KiCad's frame)
sig = [p for p in fp.Pads() if p.GetNumber().isdigit()]
mean_y = sum(p.GetPosition().y for p in sig) / len(sig) / 1e6
if mean_y < cy:
    try:
        fp.SetOrientation(pcbnew.EDA_ANGLE(D["rotation"] + 180, pcbnew.DEGREES_T))
    except (AttributeError, TypeError):
        fp.SetOrientationDegrees(D["rotation"] + 180)
    sig = [p for p in fp.Pads() if p.GetNumber().isdigit()]
    mean_y = sum(p.GetPosition().y for p in sig) / len(sig) / 1e6
    if mean_y < cy:
        sys.exit("could not orient the connector with its pads toward the board body")

# ---- vias, tracks, pad nets ------------------------------------------------------------
pinmap = {}
other = "B.Cu" if D["cu_layer"] == "F.Cu" else "F.Cu"
for ch in D["channels"]:
    net = net_of(ch["net"])
    vx, vy = ch["via"]
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(V(vx, vy))
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetDrill(mm(D["via_drill"]))
    try:
        via.SetWidth(mm(D["via_d"]))
    except TypeError:
        via.SetWidth(pcbnew.PADSTACK_ALL_LAYERS, mm(D["via_d"]))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetNet(net)
    board.Add(via)

    pts = ch["back_path"]
    for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:]):
        if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(V(x1, y1))
        t.SetEnd(V(x2, y2))
        t.SetWidth(mm(D["track_w"]))
        t.SetLayer(layer_id[other])
        t.SetNet(net)
        board.Add(t)

    px, py = pts[-1]
    best = min(sig, key=lambda p: (p.GetPosition().x / 1e6 - px) ** 2 + (p.GetPosition().y / 1e6 - py) ** 2)
    d = ((best.GetPosition().x / 1e6 - px) ** 2 + (best.GetPosition().y / 1e6 - py) ** 2) ** 0.5
    if d > 0.15:
        sys.exit(f"track end for {ch['net']} is {d:.2f} mm from the nearest connector pad")
    best.SetNet(net)
    pinmap[ch["net"]] = int(best.GetNumber())

# ---- silkscreen label on the connector side --------------------------------------------
if D.get("label"):
    txt = pcbnew.PCB_TEXT(board)
    txt.SetText(D["label"])
    lx, ly = D["label_pos"]
    txt.SetPosition(V(lx, ly))
    txt.SetLayer(pcbnew.B_SilkS if D["conn_side"] == "B" else pcbnew.F_SilkS)
    txt.SetTextSize(pcbnew.VECTOR2I(mm(1.0), mm(1.0)))
    txt.SetTextThickness(mm(0.15))
    if D["conn_side"] == "B":
        txt.SetMirrored(True)
    board.Add(txt)

pcbnew.SaveBoard(pcb_path, board)
print(json.dumps({"pads": pinmap, "footprint_pads": len(sig)}))
