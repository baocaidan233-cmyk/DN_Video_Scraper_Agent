#!/usr/bin/env python3
"""
make_outro.py — render an animated ~3s brand outro clip (16:9).

Clean/modern/high-contrast, black-red-white. Three beats:

  Step 1 (0.0-1.0s) — Dynamic roll-in: the logo enters fast from the left,
      spinning on its VERTICAL axis (real 3D perspective) and tilted ~12°
      clockwise, trailing 3-5 bright red speed streaks (~1/3 screen wide) with
      moderate motion blur that decreases as it slows. Strong ease-out.
  Step 2 (1.0-2.0s) — Locks into place: rotation straightens to head-on, a tiny
      2-3px elastic overshoot settles, an elliptical red glow BLOOMS beneath
      (expands, peaks, then holds at ~28%), a faint reflection fades away, a
      subtle left-to-right highlight sweeps the logo, then a short still hold.
  Step 3 (2.0-3.0s) — FOLLOW button fades up from below (+ ~25px) with a soft
      glow and gentle easing, then holds.

All frames are composited with Pillow (real per-frame 3D warp, motion blur,
easing) and encoded with FFmpeg. No drawtext/libass needed.

LOCAL PATCH (dailynews-agent): --width/--height, with every hardcoded pixel
value scaled off the original 1920x1080 design via `S`. Run once per brand to
prebuild video/brand/<slug>/outro.mp4; never called at render time.

Usage:
    python3 make_outro.py --logo logo.png --out outro.mp4
        [--width 960] [--height 720]
        [--duration 3.0] [--fps 30] [--accent 0xFF1E1E] [--bg 0x0A0A0A]
        [--cta FOLLOW]
"""
import argparse
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import text_render as T

REF_W, REF_H = 1920, 1080
W, H = REF_W, REF_H
S = 1.0                       # layout scale vs the 1920x1080 reference


def sc(value, minimum=1):
    """Scale a reference-space pixel value, never below `minimum`."""
    return max(int(round(value * S)), minimum)


# ---------- easing ----------
def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out_cubic(p):
    return 1 - (1 - p) ** 3


def ease_out_quart(p):
    return 1 - (1 - p) ** 4


def hexc(s):
    s = str(s).lower().replace("0x", "").replace("#", "")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def set_alpha(img, a):
    r, g, b, al = img.split()
    al = al.point(lambda v: int(v * clamp01(a)))
    return Image.merge("RGBA", (r, g, b, al))


# ---------- fake 3D rotation about the vertical (Y) axis ----------
def _solve8(A, B):
    n = len(B)
    M = [A[i][:] + [B[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col] or 1e-12
        for r in range(n):
            if r != col:
                fct = M[r][col] / pv
                for c in range(col, n + 1):
                    M[r][c] -= fct * M[col][c]
    return [M[i][n] / (M[i][i] or 1e-12) for i in range(n)]


def _persp_coeffs(out_pts, in_pts):
    A, B = [], []
    for (xo, yo), (xi, yi) in zip(out_pts, in_pts):
        A.append([xo, yo, 1, 0, 0, 0, -xi * xo, -xi * yo]); B.append(xi)
        A.append([0, 0, 0, xo, yo, 1, -yi * xo, -yi * yo]); B.append(yi)
    return _solve8(A, B)


def rotate_y(img, yaw_deg, f=None):
    """Rotate an RGBA image about its vertical axis with perspective.

    Returns (warped_image, (center_x, center_y)) where the center maps to the
    logo's centre so callers can position it precisely.
    """
    w, h = img.size
    f = 900.0 * S if f is None else f     # focal length scales with the frame
    a = math.radians(yaw_deg)
    ca, sa = math.cos(a), math.sin(a)
    cw, ch = int(w * 1.9), int(h * 1.5)
    cx, cy = cw / 2, ch / 2
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    dst = []
    for (X, Y) in corners:
        xr = X * ca
        zr = X * sa
        s = f / (f - zr)
        dst.append((cx + xr * s, cy + Y * s))
    coeffs = _persp_coeffs(dst, src)     # output(dst) -> input(src)
    out = img.transform((cw, ch), Image.PERSPECTIVE, coeffs,
                        resample=Image.BICUBIC)
    return out, (cx, cy)


# ---------- sprites ----------
def radial_glow(w, h, color, strength=1.0):
    g = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    cx, cy = w // 2, h // 2
    d.ellipse([cx - w * 0.42, cy - h * 0.42, cx + w * 0.42, cy + h * 0.42],
              fill=(*color, int(200 * strength)))
    return g.filter(ImageFilter.GaussianBlur(max(w * 0.11, 1)))


def streak_sprite(length, thick, color):
    """A horizontal speed streak: bright near the right, fading to the left."""
    s = Image.new("RGBA", (length, thick), (0, 0, 0, 0))
    d = ImageDraw.Draw(s)
    for x in range(length):
        a = int(255 * (x / length) ** 1.6)
        d.line([(x, 0), (x, thick)], fill=(*color, a))
    return s.filter(ImageFilter.GaussianBlur(1.2))


def build_button(cta, accent):
    reg, bold = T.find_fonts()
    f = ImageFont.truetype(bold, sc(58, 10))
    scratch = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    tw = scratch.textlength(cta, font=f)
    pw, ph = int(tw + sc(150)), sc(120)
    btn = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(btn)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=ph // 2,
                        fill=(*accent, 255))
    d.text(((pw - tw) / 2, (ph - f.size) / 2 - sc(6)), cta, font=f,
           fill=(255, 255, 255, 255))
    pad = sc(60)
    glow = Image.new("RGBA", (pw + 2 * pad, ph + 2 * pad), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle([pad, pad, pad + pw - 1, pad + ph - 1],
                         radius=ph // 2, fill=(*accent, 210))
    glow = glow.filter(ImageFilter.GaussianBlur(sc(34)))
    return btn, glow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--accent", default="0xFF1E1E")
    ap.add_argument("--bg", default="0x0A0A0A")
    ap.add_argument("--cta", default="FOLLOW")
    ap.add_argument("--width", type=int, default=REF_W)
    ap.add_argument("--height", type=int, default=REF_H)
    args = ap.parse_args()

    global W, H, S
    W, H = args.width, args.height
    S = min(W / REF_W, H / REF_H)

    accent = hexc(args.accent)
    bg = hexc(args.bg)
    fps = args.fps
    N = int(round(args.duration * fps))

    logo = Image.open(args.logo).convert("RGBA")
    lh = sc(380, 20)
    lw = int(logo.width * lh / logo.height)
    logo = logo.resize((lw, lh), Image.LANCZOS)
    logo_cx, logo_cy = W // 2, H // 2 - sc(80)
    logo_left, logo_top = logo_cx - lw // 2, logo_cy - lh // 2
    logo_alpha = logo.split()[3]

    glow_base = radial_glow(sc(900), sc(300), accent)
    streak = streak_sprite(W // 3, sc(6), (255, 60, 60))
    btn, btn_glow = build_button(args.cta, accent)
    btn_cx, btn_cy = W // 2, logo_cy + lh // 2 + sc(190)

    # reflection: flipped logo with a downward-fading gradient
    refl = logo.transpose(Image.FLIP_TOP_BOTTOM)
    grad = Image.new("L", (lw, lh), 0)
    gd = ImageDraw.Draw(grad)
    for yy in range(lh):
        gd.line([(0, yy), (lw, yy)], fill=int(255 * (1 - yy / lh)))
    refl.putalpha(ImageChops.multiply(refl.split()[3], grad))

    start_off = -(W // 2 + lw)              # start fully off-screen left
    ROLL = 0.95                            # roll-in completes ~0.95s

    tmp = Path(args.out).parent / ".otmp"
    tmp.mkdir(parents=True, exist_ok=True)

    prev_x = None
    for i in range(N):
        t = i / fps
        frame = Image.new("RGBA", (W, H), (*bg, 255))

        # ---- primary horizontal travel (strong ease-out) ----
        p = clamp01(t / ROLL)
        base_x = start_off * (1 - ease_out_quart(p))
        # tiny elastic settle (2-3px overshoot, one gentle bounce)
        settle = 0.0
        if t >= 0.82:
            s = t - 0.82
            settle = 3.0 * S * math.exp(-7.5 * s) * math.sin(2 * math.pi * 3.2 * s)
        x = logo_cx + base_x + settle

        # ---- 3D vertical-axis spin + clockwise tilt, straightening out ----
        yaw = -46 * (1 - ease_out_quart(p))       # spins to face viewer
        tilt = 12 * (1 - ease_out_quart(p))       # clockwise -> 0

        sharp = abs(yaw) < 0.6 and abs(tilt) < 0.6
        if sharp:
            piece = logo
        else:
            warped, _ = rotate_y(logo, yaw)
            piece = warped.rotate(-tilt, expand=True, resample=Image.BICUBIC)
        pw2, ph2 = piece.size
        px = int(x - pw2 / 2)
        py = int(logo_cy - ph2 / 2)

        # ---- red speed streaks (Step 1, fade out as it slows) ----
        streak_amt = clamp01((0.5 - t) / 0.5)
        if streak_amt > 0.02:
            for k in range(4):
                oy = logo_cy - sc(90) + k * sc(60)
                sa = 0.85 * streak_amt * (0.6 + 0.4 * (k % 2))
                st = set_alpha(streak, sa)
                frame.alpha_composite(st, (int(x - lw // 2 - st.width),
                                           oy - st.height // 2))

        # ---- ground glow bloom (Step 2) ----
        if t >= 1.0:
            bp = clamp01((t - 1.0) / 0.28)
            scale = 0.5 + 0.6 * ease_out_cubic(bp)
            if t < 1.28:
                ga = 0.35 + 0.65 * ease_out_cubic(bp)      # rise to peak
            else:
                ga = 1.0 - 0.72 * clamp01((t - 1.28) / 0.34)  # settle ~0.28
            gw, gh = max(int(sc(900) * scale), 1), max(int(sc(300) * scale), 1)
            gl = set_alpha(glow_base.resize((gw, gh)), ga)
            frame.alpha_composite(gl, (logo_cx - gw // 2,
                                       logo_cy + lh // 2 - sc(46)))

        # ---- reflection (Step 2, fades within ~0.5s) ----
        if 1.0 <= t < 1.5:
            r2 = set_alpha(refl, 0.14 * (1 - (t - 1.0) / 0.5))
            frame.alpha_composite(r2, (logo_left, logo_cy + lh // 2 + sc(6)))

        # ---- motion blur ghosts along travel (Step 1) ----
        if prev_x is not None:
            dx = x - prev_x
            ghosts = min(int(abs(dx) / max(11 * S, 1)), 12)
            for g in range(ghosts):
                fr = (g + 1) / (ghosts + 1)
                gx = prev_x + dx * fr
                ghost = set_alpha(piece, 0.28 * (1 - fr))
                frame.alpha_composite(ghost, (int(gx - pw2 / 2), py))
        prev_x = x

        # ---- sharp logo ----
        frame.alpha_composite(piece, (px, py))

        # ---- highlight sweep across the logo (Step 2) ----
        if sharp and 1.05 <= t <= 1.5:
            sp = (t - 1.05) / 0.45
            bw = int(lw * 0.4)
            bx = int(-bw + (lw + bw) * sp)
            box = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
            bd = ImageDraw.Draw(box)
            for c in range(bw):
                xx = bx + c
                if 0 <= xx < lw:
                    a = int(70 * (1 - abs(c - bw / 2) / (bw / 2)))
                    if a > 0:
                        bd.line([(xx, 0), (xx, lh)], fill=(255, 255, 255, a))
            box.putalpha(ImageChops.multiply(box.split()[3], logo_alpha))
            frame.alpha_composite(box, (logo_left, logo_top))

        # ---- FOLLOW button (Step 3) ----
        bp = clamp01((t - 2.0) / 0.6)
        if bp > 0:
            ba = ease_out_cubic(bp)
            rise = int(sc(28) * (1 - ba))
            g2 = set_alpha(btn_glow, 0.85 * ba)
            frame.alpha_composite(g2, (btn_cx - g2.width // 2,
                                       btn_cy - g2.height // 2 + rise))
            b2 = set_alpha(btn, ba)
            frame.alpha_composite(b2, (btn_cx - b2.width // 2,
                                       btn_cy - b2.height // 2 + rise))

        frame.convert("RGB").save(tmp / f"f{i:04d}.png")

    subprocess.run(
        ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(tmp / "f%04d.png"),
         "-t", f"{args.duration}", "-c:v", "libx264", "-preset", "medium",
         "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
         "-s", f"{W}x{H}", args.out],
        check=True, capture_output=True)
    for p in tmp.glob("f*.png"):
        p.unlink()
    tmp.rmdir()
    print(f"[outro] wrote {args.out} ({args.duration:.1f}s, {N} frames)")


if __name__ == "__main__":
    main()
