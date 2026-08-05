"""Renders a branded news poster: title text + optional tag badge,
composited onto either a photo background or a plain color background,
with an optional logo overlay.

No face detection, no distortion, no color filters, no stamp — this is
meant for normal news content. The photo (if any) is shown as-is, just
cropped to fill the frame.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .fonts import resolve_font

W, H = 960, 1200
BG = (245, 245, 245)
RED = (200, 30, 40)
BLACK = (20, 20, 20)


def _font(size: int, font_path: str, font_index: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, size, index=font_index)


def _wrap_by_pixel(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=f) > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _draw_text_punchy(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, f: ImageFont.FreeTypeFont, fill, shadow) -> None:
    """Title text with a hard drop shadow — needed for legibility when the
    background is a busy photo, not needed on a flat color background."""
    x, y = xy
    draw.text((x + 4, y + 4), text, font=f, fill=shadow)
    draw.text((x, y), text, font=f, fill=fill)


_LOGO_POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left")


def _paste_logo(img: Image.Image, logo_path: str, position: str, size: int, margin: int) -> None:
    if position not in _LOGO_POSITIONS:
        raise ValueError(f"logo_position must be one of {_LOGO_POSITIONS}, got {position!r}")
    logo = Image.open(logo_path).convert("RGBA")
    logo.thumbnail((size, size), Image.LANCZOS)
    positions = {
        "top-right": (img.width - logo.width - margin, margin),
        "top-left": (margin, margin),
        "bottom-right": (img.width - logo.width - margin, img.height - logo.height - margin),
        "bottom-left": (margin, img.height - logo.height - margin),
    }
    img.paste(logo, positions[position], logo)


def make_plain_card(
    title: str,
    out_path: str,
    tag_text: str = "",
    logo_path: str | None = None,
    logo_position: str = "top-right",
    logo_size: int = 160,
    logo_margin: int = 32,
    font_path: str | None = None,
    font_index: int = 0,
) -> None:
    """Plain color background — used when no photo is available for this
    story."""
    resolved_font_path, resolved_font_index = resolve_font(font_path, font_index)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W, 14], fill=RED)

    y = 380
    if tag_text:
        tag_font = _font(34, resolved_font_path, resolved_font_index)
        pad_x, pad_y = 26, 16
        tag_w = draw.textlength(tag_text, font=tag_font) + pad_x * 2
        tag_h = 34 + pad_y * 2
        tag_x, tag_y = 64, 380
        draw.rectangle([tag_x, tag_y, tag_x + tag_w, tag_y + tag_h], fill=RED)
        draw.text((tag_x + pad_x, tag_y + pad_y - 4), tag_text, font=tag_font, fill="white")
        y = tag_y + tag_h + 40

    title_font = _font(70, resolved_font_path, resolved_font_index)
    max_width = W - 64 * 2
    lines = _wrap_by_pixel(draw, title, title_font, max_width)
    for line in lines[:5]:
        draw.text((64, y), line, font=title_font, fill=BLACK)
        y += 88

    draw.rectangle([0, H - 14, W, H], fill=RED)
    if logo_path:
        _paste_logo(img, logo_path, logo_position, logo_size, logo_margin)

    img.save(out_path)


def make_photo_card(
    title: str,
    photo_path: str,
    out_path: str,
    tag_text: str = "",
    logo_path: str | None = None,
    logo_position: str = "top-right",
    logo_size: int = 160,
    logo_margin: int = 32,
    font_path: str | None = None,
    font_index: int = 0,
) -> None:
    """Photo as the background — the normal case. The photo is cropped to
    fill the frame (center-crop, no distortion) and gets a darkening
    gradient at the bottom purely so the title text stays legible,
    regardless of how bright or dark the photo itself is."""
    resolved_font_path, resolved_font_index = resolve_font(font_path, font_index)

    bg = Image.open(photo_path).convert("RGB")
    scale = max(W / bg.width, H / bg.height)
    new_size = (round(bg.width * scale), round(bg.height * scale))
    bg = bg.resize(new_size)
    left = (bg.width - W) // 2
    top = (bg.height - H) // 2
    img = bg.crop((left, top, left + W, top + H))

    gradient = Image.new("L", (1, H), 0)
    grad_start = int(H * 0.32)
    max_dark = 210
    for y in range(H):
        if y < grad_start:
            gradient.putpixel((0, y), 0)
        else:
            frac = (y - grad_start) / (H - grad_start)
            gradient.putpixel((0, y), int(max_dark * (frac ** 1.3)))
    gradient = gradient.resize((W, H))
    black = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.composite(black, img, gradient)

    draw = ImageDraw.Draw(img)

    y = 700
    if tag_text:
        tag_font = _font(34, resolved_font_path, resolved_font_index)
        pad_x, pad_y = 26, 16
        tag_w = draw.textlength(tag_text, font=tag_font) + pad_x * 2
        tag_h = 34 + pad_y * 2
        tag_x, tag_y = 64, 700
        draw.rectangle([tag_x, tag_y, tag_x + tag_w, tag_y + tag_h], fill=RED)
        draw.text((tag_x + pad_x, tag_y + pad_y - 4), tag_text, font=tag_font, fill="white")
        y = tag_y + tag_h + 34

    title_font = _font(64, resolved_font_path, resolved_font_index)
    max_width = W - 64 * 2
    lines = _wrap_by_pixel(draw, title, title_font, max_width)
    for line in lines[:4]:
        _draw_text_punchy(draw, (64, y), line, title_font, "white", (0, 0, 0))
        y += 78

    draw.rectangle([0, H - 10, W, H], fill=RED)
    if logo_path:
        _paste_logo(img, logo_path, logo_position, logo_size, logo_margin)

    img.save(out_path)
