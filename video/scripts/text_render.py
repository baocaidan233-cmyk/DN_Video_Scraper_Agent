#!/usr/bin/env python3
"""
text_render.py — render all text elements to RGBA PNGs with Pillow.

This exists because many FFmpeg builds (incl. the common Homebrew build) ship
WITHOUT drawtext/libfreetype and without the subtitles/libass filter. Rendering
text to PNG and compositing with the always-available `overlay` filter is more
portable and gives better typography (antialiasing, outlines, word highlight).

Public helpers:
    find_fonts()                       -> (regular_path, bold_path)
    gradient_card(...)                 -> writes a full-frame background still
    lower_third(...)                   -> writes a transparent lower-third PNG
    end_card(...)                      -> writes a full-frame end-card PNG
    subtitle_png(...)                  -> writes a transparent subtitle PNG

LOCAL PATCH (dailynews-agent): every layout constant below was authored against
a 1920x1080 frame. `_scale(W, H)` derives a uniform factor from that reference
so the same layouts render correctly at other sizes (we publish 960x720, 4:3),
and vertical positions are anchored to the bottom of the frame rather than to
absolute Y. Callers pass sizes in 1920x1080 reference space; scaling happens
inside these functions.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = {
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ],
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
}


def _first(paths):
    for p in paths:
        if Path(p).exists():
            return p
    return None


def find_fonts():
    reg = _first(FONT_CANDIDATES["regular"])
    bold = _first(FONT_CANDIDATES["bold"]) or reg
    if not reg:
        raise SystemExit("[text] no usable TTF font found on this host")
    return reg, bold


_REF_W, _REF_H = 1920, 1080


def _scale(W, H):
    """Uniform layout scale relative to the 1920x1080 reference design."""
    return min(W / _REF_W, H / _REF_H)


def _px(value, s, minimum=1):
    """Scale a reference-space pixel value, never below `minimum`."""
    return max(int(round(value * s)), minimum)


def _hex(color):
    """Accept '0xRRGGBB' / '#RRGGBB' / (r,g,b[,a]) -> (r,g,b,a)."""
    if isinstance(color, (tuple, list)):
        c = list(color)
        return tuple(c + [255] * (4 - len(c)))
    s = str(color).lower().replace("0x", "").replace("#", "")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (r, g, b, 255)


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- backgrounds
def gradient_card(out, label="", W=1920, H=1080,
                  top="0x14213d", bottom="0x0d1117", accent="0xE60023"):
    s = _scale(W, H)
    top, bottom, accent = _hex(top), _hex(bottom), _hex(accent)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        px_row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(W):
            px[x, y] = px_row
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, _px(12, s), H], fill=accent)          # accent spine
    if label:
        _, bold = find_fonts()
        f = ImageFont.truetype(bold, _px(96, s, 10))
        lines = _wrap(d, label.upper(), f, W - _px(320, s))
        lh = f.size + _px(18, s)
        total = lh * len(lines)
        y = (H - total) // 2
        for ln in lines:
            w = d.textlength(ln, font=f)
            d.text(((W - w) / 2, y), ln, font=f, fill=(255, 255, 255, 230),
                   stroke_width=_px(2, s), stroke_fill=(0, 0, 0, 180))
            y += lh
    img.save(out)
    return out


# ---------------------------------------------------------------- lower third
def chyron_geometry(W, H):
    """(bar_top, bar_h) of the lower third — shared with the subtitle layout.

    Anchored to the bottom of the frame so the bar keeps its 50px reference
    margin at any output size instead of floating mid-frame.
    """
    s = _scale(W, H)
    bar_h = _px(150, s)
    return H - _px(50, s) - bar_h, bar_h


def _subtitle_bottom(W, H, has_chyron):
    """Baseline the subtitle block sits on: above the chyron, else near the floor."""
    s = _scale(W, H)
    if has_chyron:
        return chyron_geometry(W, H)[0] - _px(24, s)
    return H - _px(70, s)


def lower_third(out, headline, logo=None, W=1920, H=1080, accent="0xE60023"):
    s = _scale(W, H)
    accent = _hex(accent)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bar_top, bar_h = chyron_geometry(W, H)
    d.rectangle([0, bar_top, W, bar_top + bar_h], fill=(0, 0, 0, 140))
    stripe = _px(5, s)
    d.rectangle([0, bar_top - stripe, W, bar_top], fill=accent)  # accent stripe

    reg, bold = find_fonts()
    pad = _px(40, s)
    text_x = _px(60, s)
    if logo and Path(logo).exists():
        try:
            lg = Image.open(logo).convert("RGBA")
            side = _px(110, s)
            lg.thumbnail((side, side), Image.LANCZOS)
            ly = bar_top + (bar_h - lg.height) // 2
            img.alpha_composite(lg, (pad, ly))
            text_x = pad + side + _px(30, s)
        except Exception:
            pass

    f = ImageFont.truetype(bold, _px(54, s, 10))
    min_size = _px(30, s, 8)
    head = headline.upper()
    # shrink to fit one line within the bar
    while d.textlength(head, font=f) > W - text_x - _px(60, s) and f.size > min_size:
        f = ImageFont.truetype(bold, f.size - 2)
    ty = bar_top + (bar_h - f.size) // 2 - _px(4, s)
    d.text((text_x, ty), head, font=f, fill=(255, 255, 255, 255),
           stroke_width=_px(1, s), stroke_fill=(0, 0, 0, 150))
    img.save(out)
    return out


# ---------------------------------------------------------------- end card
def end_card(out, handle, W=1920, H=1080, bg="0x0d1117", accent="0xE60023",
             logo=None, cta="FOLLOW"):
    s = _scale(W, H)
    bg, accent = _hex(bg), _hex(accent)
    img = Image.new("RGBA", (W, H), bg)
    d = ImageDraw.Draw(img)
    reg, bold = find_fonts()

    cy = H // 2
    if logo and Path(logo).exists():
        try:
            lg = Image.open(logo).convert("RGBA")
            side = _px(180, s)
            lg.thumbnail((side, side), Image.LANCZOS)
            img.alpha_composite(lg, ((W - lg.width) // 2, cy - _px(300, s)))
        except Exception:
            pass

    fh = ImageFont.truetype(bold, _px(92, s, 10))
    hw = d.textlength(handle, font=fh)
    d.text(((W - hw) / 2, cy - _px(120, s)), handle, font=fh,
           fill=(255, 255, 255, 255))

    # follow pill
    fc = ImageFont.truetype(bold, _px(52, s, 8))
    cw = d.textlength(cta, font=fc)
    pill_w, pill_h = int(cw + _px(120, s)), _px(108, s)
    px0 = (W - pill_w) // 2
    py0 = cy + _px(40, s)
    d.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h],
                        radius=pill_h // 2, fill=accent)
    d.text((px0 + (pill_w - cw) / 2, py0 + (pill_h - fc.size) / 2 - _px(6, s)),
           cta, font=fc, fill=(255, 255, 255, 255))
    img.save(out)
    return out


# ---------------------------------------------------------------- subtitles
def subtitle_png(out, text, W=1920, H=1080, has_chyron=True, fontsize=46,
                 highlight=None, accent="0xE60023"):
    s = _scale(W, H)
    fontsize = _px(fontsize, s, 8)
    accent = _hex(accent)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    reg, bold = find_fonts()
    f = ImageFont.truetype(bold, fontsize)
    stroke = _px(3, s)

    lines = ([ln for ln in text.split("\n") if ln]
             or _wrap(d, text, f, W - _px(300, s)))
    # a long cue that arrived as one pre-split line can still overflow
    lines = [w for ln in lines for w in _wrap(d, ln, f, W - _px(300, s))]
    lh = fontsize + _px(14, s)
    block_h = lh * len(lines)
    # sit above the chyron bar when present, else near the bottom
    y = _subtitle_bottom(W, H, has_chyron) - block_h

    hl = (highlight or "").lower().strip()
    for ln in lines:
        w = d.textlength(ln, font=f)
        x = (W - w) / 2
        if hl and hl in ln.lower():
            # draw word-by-word so the highlighted token gets the accent color
            cx = x
            for tok in ln.split(" "):
                col = accent if tok.lower().strip(".,!?") == hl else (255, 255, 255, 255)
                d.text((cx, y), tok, font=f, fill=col, stroke_width=stroke,
                       stroke_fill=(0, 0, 0, 220))
                cx += d.textlength(tok + " ", font=f)
        else:
            d.text((x, y), ln, font=f, fill=(255, 255, 255, 255),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 220))
        y += lh
    img.save(out)
    return out


# ---------------------------------------------------------------- credit line
def credit_png(out, text, W=1920, H=1080, fontsize=22, opacity=140,
               corner="tr", margin=34):
    """Render a tiny, unobtrusive attribution line into a transparent frame.

    Deliberately small and semi-transparent so it never competes with the
    footage, chyron, or subtitles. Default corner is top-right (well clear of
    the bottom lower-third and subtitle area).
    """
    s = _scale(W, H)
    fontsize, margin = _px(fontsize, s, 7), _px(margin, s)
    reg, bold = find_fonts()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(reg, fontsize)

    # LOCAL PATCH (dailynews-agent): keep the credit inside the frame. A
    # multi-author line is easily wider than a 960px frame and used to run off
    # the edge. Shrink to fit, then ellipsize as a last resort.
    avail = W - 2 * margin
    while d.textlength(text, font=f) > avail and f.size > _px(12, s, 7):
        f = ImageFont.truetype(reg, f.size - 1)
    if d.textlength(text, font=f) > avail:
        while text and d.textlength(text + "…", font=f) > avail:
            text = text[:-1]
        text += "…"

    tw = d.textlength(text, font=f)
    if corner in ("tr", "br"):
        x = W - tw - margin
    else:
        x = margin
    y = margin if corner in ("tr", "tl") else H - fontsize - margin
    # soft shadow for legibility over bright frames, then low-opacity white
    d.text((x + 1, y + 1), text, font=f, fill=(0, 0, 0, min(opacity, 160)))
    d.text((x, y), text, font=f, fill=(255, 255, 255, opacity))
    img.save(out)
    return out


# ---------------------------------------------------------------- karaoke subs
def karaoke_frames(out_dir, prefix, words, W=1920, H=1080, has_chyron=True,
                   fontsize=46, hi_color=(255, 215, 0), pad_side=300):
    """Render one PNG per word with that word highlighted (karaoke).

    `words` is a list of {text, start, end}. Words are grouped into cues of up
    to 2 centered lines; every word gets a variant PNG (same layout, that word
    in hi_color, the rest white). Returns [{start, end, png}] for the renderer
    to overlay in sequence.
    """
    from pathlib import Path as _P
    s = _scale(W, H)
    fontsize, pad_side = _px(fontsize, s, 8), _px(pad_side, s)
    stroke = _px(3, s)
    _P(out_dir).mkdir(parents=True, exist_ok=True)
    reg, bold = find_fonts()
    f = ImageFont.truetype(bold, fontsize)
    scratch = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    space_w = scratch.textlength(" ", font=f)
    max_line_w = W - 2 * pad_side

    def tw(i):
        return scratch.textlength(words[i]["text"], font=f)

    def line_w(line):
        return sum(tw(i) for i in line) + space_w * max(len(line) - 1, 0)

    # ---- group words into cues (<=2 lines each) ----
    cues, cur = [], [[]]
    for idx in range(len(words)):
        line = cur[-1]
        prospective = (tw(idx) if not line
                       else line_w(line) + space_w + tw(idx))
        if line and prospective > max_line_w:
            if len(cur) >= 2:
                cues.append(cur)
                cur = [[idx]]
            else:
                cur.append([idx])
        else:
            line.append(idx)
    if any(cur):
        cues.append(cur)

    # ---- layout: (x, y) per word, and cue membership ----
    lh = fontsize + _px(14, s)
    pos = {}
    cue_of = {}
    cue_words = []
    bottom = _subtitle_bottom(W, H, has_chyron)
    for ci, lines in enumerate(cues):
        members = [i for ln in lines for i in ln]
        cue_words.append(members)
        for i in members:
            cue_of[i] = ci
        top = bottom - lh * len(lines)
        for li, ln in enumerate(lines):
            y = top + li * lh
            x = (W - line_w(ln)) / 2
            for i in ln:
                pos[i] = (x, y)
                x += tw(i) + space_w

    # ---- render a variant PNG per word ----
    frames = []
    for idx, w in enumerate(words):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for j in cue_words[cue_of[idx]]:
            x, y = pos[j]
            col = hi_color if j == idx else (255, 255, 255, 255)
            d.text((x, y), words[j]["text"], font=f, fill=col,
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 230))
        png = str(_P(out_dir) / f"{prefix}_{idx:03d}.png")
        img.save(png)
        frames.append({"start": w["start"], "end": w["end"], "png": png})
    return frames


if __name__ == "__main__":
    # quick self-test
    reg, bold = find_fonts()
    print("fonts:", reg, "|", bold)
