# Automatic Design Pipeline for Flex PCB Based Resistive Sensing Gloves

This repo contains instructions for automating the design of FPCB-based tactile sensing gloves from a hand photo on legal-size paper.

## Prerequisites

- Python 3.12
- Photoshop or some photo editing software (for customizing hand mask contour)

## Step 1: Setup

pip install -r requirements.txt

Take a picture of your hand on legal-size paper (8.5 x 11 inches). Make sure that

- There is some amount of space between adjacent fingers
- You have at least 2 inches of wrist on the page
- Your phone camera is pointing directly down (not at an off-angle). For example:

![Example hand photo](handDevin6.jpg)

## Step 2: Create Binary Mask

Place the image you took of your hand in the root of this directory. Open the jupyter notebook, run the first cell to import the required libraries, and set the "originalHand" variable to the name of the image file.

Run the first cell (Crop Hand to Paper)

A cropped version of the photo will be saved to "croppedHand.jpg". Open this in a photo editor of your choice and create a binary hand mask representing the hand's contour (instructions below for Photoshop)

In Photoshop, select a first outline of the hand using the object selection tool abd select the button to create a mask from your selection

![Rough outline](assets/createMaskFromSelection.png)

Edit the mask you've selected using whatever tools you want. Add cutouts (using the brush tool) between the fingers and along bending regions for increased flexibility. Correct the thumb area to account for the photo taken at an off angle. 

![Edit Mask](assets/editMask.png)

Right-click the mask in the layers panel and select "Add mask to selection"
![Add to selection](assets/addmasktoselection.png)

Create a Hue-Saturation adjustment layer and set the lightness to 100
![Hue saturation Light](assets/adjustments1.png)

Invert the original mask and add it to selection
![Invert mask](assets/invertmask.png)

Create another Hue-Saturation adjustment layer and set the lightness to 0. Delete the original mask (so that the bottom layer has no masks directly attached)
![Final Mask](assets/finalMask.png)

Save the final file in the root directory of this repo

## Step 3: Configure Sensing Regions

Set the variable "cropped_mask_path" to the filename of the hand mask you just made. Run the following cells up to and including "Sensing Region Widget"

The code automatically tries to lay out the sensing regions by various geometric operations. This is not perfect, so there is a gui to help drag the vertices of each sensing region. Make sure each vertex lies within the hand contour and outside bending regions, and that contact points are contained within the sensing regions. When satisfied, click save. 

![Edit polygons](assets/editPolygons.png)


## Step 4: Create Kicad Footprint

Run the remaining code under "Extrude the contour and connect all traces" and "Create SVG and KiCad Mod". This will create two footprints, handFront and handBack, representing the two PCBs you must order to make a sensor.

## Step 5: Prepare the footprints for ordering

Open one of the footprints in KiCad using the footprint editor. Add vias to the bottom of the tracks (0.35 diameter, 0.15 hole). Save the footprint to a library.

![Edit Footprint](assets/editFootprint.png)

Also download and import the connector footprint: https://www.digikey.com/en/models/5132522 

Create a new project and open the PCB editor. 

Import the hand footprint and place it in the editor (you may want to set the page settings to portrait to accomodate it). 

Then import the connector and place it below the ends of the vias you just added, on the opposite side of the copper traces of the hand. Connect the pads of the connector to the vias using the route tracks tool. 

![Edit Board](assets/editBoard.png)

When all tracks are connected, export the gerber and drill files under File > Fabrication Outputs > Gerbers. Make sure the User.Eco1 layer is selected, as this is the adhesive that the manufacturer will need to deposit. Click Plot, and then Generate Drill Files > Plot. 

![Export Gerbers](assets/exportGerber.png)

## Step 6: Order from Manufacturer

Go to the pcbway instant quote tool: https://www.pcbway.com/flexible.aspx

Enter the size of the footprint (rough estimate using the measure tool in KiCad is fine).

Most of the default options are fine. Make sure 
- 2 sided is selected
- Board thickness 0.08mm
- 3M/Tessa Tape -> One-sided (Same side as copper tracks(opposite side of connector). Front Cu Tracks= One-sided top)

In the special request box, be sure to specify
- Adhesive should go in the area defined by User.Eco1 layer
- Please use Rolled-Annealed Copper
- Please leave off surface finish

The engineer will likely reccomend you to put surface finish on because the copper will oxidize- we encapsulate it in silicone so this is not a problem for us, and bare copper is more flexible. Tell them no surface finish is fine. 

![PCB Options](assets/pcbOptions.png)


## Body pads: forearm, bicep and chest (`body_pads/`)

`body_pads/make_body_pads.py` applies the same construction to three larger pads for the care-robotics
arm/chest sensing idea: a forearm underside wrap, a bicep wrap that reaches round to the lateral side, and a
chest pad made of two mirrored halves with a V neckline. The pads do not need a photo or MediaPipe - the
outlines are parametric (`forearm_spec()`, `bicep_spec()`, `chest_left_spec()`), so sizes are edited in code.

```bash
.\.venv\Scripts\python.exe body_pads\make_body_pads.py
```

What is kept from the glove design (Sec. 3.1 of the paper):

- two single-sided FPCBs per pad, "Front" = column electrodes on F.Cu (bottom board), "Back" = row
  electrodes on B.Cu (top board), Velostat between them, silicone outside;
- coverlay (`F.Mask`/`B.Mask`) opened over the sensing region, adhesive (`Eco1.User`) in the ring between the
  outline and the opening, both boards share one body outline;
- connecting traces routed along nested inward offsets of the opening (the paper's 0.2 mm "inward
  extrusions" of the hand contour), here 0.25 mm traces at 0.5 mm pitch in a 10 mm margin;
- inner cut-outs for flexibility, made by buffering all copper (0.8 mm) and subtracting it from the cells
  between electrodes, shared by both boards and by the Velostat/silicone cut template;
- at most 16 x 16 electrodes per pad, so the glove's 16 x 16 readout and 16-pin 0.5 mm FFC path work unchanged.

What is different, because the pads are large and bend gently:

- electrodes are 3 mm strips instead of 0.1 mm lines, so each ~12-13 mm taxel has a real contact area;
- traces are 0.25 mm wide (the paper found wider traces more than double the bend life).

The tail is built the way Step 5 describes for the glove, but automatically: every trace ends in a through
via (0.45 mm pad / 0.2 mm drill, two staggered rows) and a 16-pin 0.5 mm FFC connector (KiCad library
footprint `Connector_FFC-FPC:Hirose_FH12-16S-0.5SH_1x16-1MP_P0.50mm_Horizontal`, swap it in `Params` if you use
another part) sits on the side opposite the electrodes, so the boards are 2-layer like the glove. Between the
0.5 mm bus and the 1.0 mm via pitch the traces run through a nested staircase (no crossings) on each side. The
Front and Back tails sit 21 mm apart; the Front connector faces down (B.Cu), the Back connector faces up
(F.Cu), i.e. both face outward when the boards are stacked.

| pad | body (mm) | columns x rows | taxels | board incl. tail (mm) |
|---|---|---|---|---|
| forearm | 198 x 216 | 14 x 15 | 186 | 198 x 237 |
| bicep | 228 x 215 | 15 x 14 | 192 | 228 x 236 |
| chest_left / chest_right | 165 x 193 each | 12 x 14 | 132 each | 166 x 213 |

`make_body_pads.py` writes the electrode-side geometry itself, then calls `body_pads/kicad_post.py` with
KiCad's bundled Python (`…\KiCad\10.0\bin\python.exe`) to add the vias, the connector footprint, the
connector-side tracks and the nets, and finally `kicad-cli` to plot and check. Per pad, `body_pads/out/<pad>/`
contains:

- `<pad>_Front.svg` / `_Back.svg` - same layer groups as `handFront.svg` (the notebook's `create_kicad_mod()`
  accepts them) and the svg2mod `.kicad_mod` footprints, electrode side only;
- `<pad>_Front.kicad_pcb` / `_Back.kicad_pcb` + `.kicad_pro` (flex design rules) - the complete boards;
- `gerbers_Front/`, `gerbers_Back/` (+ `.zip`) - the same file set as the glove: `F_Cu`, `B_Cu`, `F_Mask`,
  `B_Mask`, `F_Paste`, `B_Paste`, `F_Silkscreen`, `B_Silkscreen`, `Edge_Cuts`, `User_Eco1` (adhesive),
  `User_Eco2` (stiffener), `PTH.drl` (vias), `NPTH.drl`, drill maps and the `.gbrjob`;
- `pinout_*.csv` (J1 pin -> electrode), `<pad>_cut_template.svg` (mm units, body outline + cut-outs for the
  Velostat and silicone), `<pad>_preview.png`, `<pad>_tail.png`, `drc_*.json` and `summary.json`.

Checks: shapely clearances (copper-copper >= 0.25 mm on traces, 0.2 mm at the connector pads, copper-edge
>= 0.5 mm, copper-cut 1.0 mm) and `kicad-cli pcb drc` with the flex rules in the `.kicad_pro` - both must be
clean. Mask bridges between electrodes are the sensing opening and are ignored in the DRC.

Ordering notes (PCBWay flex quote, as in Step 6): 2 layers, 0.08-0.11 mm polyimide, rolled-annealed copper,
coverlay with openings per the mask layers, PSA tape on the electrode side in the `Eco1.User` area, polyimide
stiffener under the connector per `Eco2.User` (on the electrode side of the tail), surface finish only needed
on the connector pads.

## Known Issues

During the final connection of traces, you may get the error "Multipolygon object has no attribute exterior". Decreasing the min_trace_spacing by 0.01 seems to avoid this error. 

Why this is happening I'm not quite sure of yet, my hypothesis is some part of the contour is folding over on itself when being "extruded inward" causing a hole to form. 
