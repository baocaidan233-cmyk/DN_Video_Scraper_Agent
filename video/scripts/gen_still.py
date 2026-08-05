#!/usr/bin/env python3
"""
gen_still.py — render a self-produced graphic still (zero licensing risk).

Fallback visual when no verified open-license media is available for a query,
or when the runtime has no network. Produces a 1920x1080 gradient card with an
optional keyword label via Pillow (no drawtext dependency). Mark it
"self-produced" in SOURCES.md.

LOCAL PATCH (dailynews-agent): --width/--height, so the card can be rendered at
the renderer's Ken Burns supersample size and in the OUTPUT aspect ratio. A
16:9 card fed into a 4:3 render would have its centred label cropped away.

Usage:
    python3 gen_still.py --out work/assets/card1.png [--label "COURT RULING"]
                         [--width 2400] [--height 1800]
                         [--top 0x14213d] [--bottom 0x0d1117] [--accent 0xE60023]
"""
import argparse
from pathlib import Path

import text_render as T


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--top", default="0x14213d")
    ap.add_argument("--bottom", default="0x0d1117")
    ap.add_argument("--accent", default="0xE60023")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    T.gradient_card(args.out, args.label, W=args.width, H=args.height,
                    top=args.top, bottom=args.bottom, accent=args.accent)
    print(f"[still] wrote self-produced graphic {args.out}"
          + (f" (label: {args.label})" if args.label else ""))


if __name__ == "__main__":
    main()
