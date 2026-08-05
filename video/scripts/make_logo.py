#!/usr/bin/env python3
"""Recreate the DAILY NEWS logo (placeholder for the user's exact PNG)."""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else "logo.png"
REG = "/System/Library/Fonts/Supplemental/Arial.ttf"
BLK = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
BLK = BLK if os.path.exists(BLK) else REG

S = 780
red = (240, 10, 16, 255)
blk = (10, 10, 10, 255)
wht = (255, 255, 255, 255)
cx = S // 2

img = Image.new("RGBA", (S, S), red)
d = ImageDraw.Draw(img)
d.ellipse([26, 26, S - 26, S - 26], fill=blk)

# DAILY (regular weight, wide)
fD = ImageFont.truetype(REG, 150)
tD, yD = "DAILY", 232
wD = d.textlength(tD, font=fD)
d.text((cx - wD / 2, yD), tD, font=fD, fill=wht)

# erase the central "I" so the red arrow can stand in for it
d.rectangle([cx - 16, yD + 8, cx + 16, yD + 150], fill=blk)

# NEWS (heavier, larger)
fN = ImageFont.truetype(BLK, 176)
tN, yN = "NEWS", 392
wN = d.textlength(tN, font=fN)
d.text((cx - wN / 2, yN), tN, font=fN, fill=wht)

# red up-arrow as the "I": thin shaft, slim head, base dot
tip_y, dot_y = 60, yD + 150
shaft = 9
d.rectangle([cx - shaft // 2, tip_y + 46, cx + shaft // 2, dot_y], fill=red)
d.polygon([(cx, tip_y), (cx - 20, tip_y + 60), (cx + 20, tip_y + 60)], fill=red)
d.ellipse([cx - 12, dot_y - 12, cx + 12, dot_y + 12], fill=red)

img.save(OUT)
print(f"logo -> {OUT}")
