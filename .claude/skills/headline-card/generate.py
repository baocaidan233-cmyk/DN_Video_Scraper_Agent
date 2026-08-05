#!/usr/bin/env python3
"""Generate a branded headline card for the DailyNews (dn) or EpicFury (ef)
pipeline.

Wraps the vendored `headline_card_generator` package with per-channel
branding (logo + tag) read from channels.yaml. Tries to find a person photo
via DuckDuckGo; if none is found, falls back to a plain-background card so a
post never goes out image-less.

Examples:
    python3 generate.py --pipeline dn --title "Company X unveils new product" --person "Elon Musk"
    python3 generate.py --pipeline ef --title "Markets tumble on rate fears" --tag BREAKING --out /tmp/card.png

Exit codes: 0 = card written; non-zero = usage/config/font error (with an
`error:` line on stderr). Prints the absolute output path (and nothing else)
to stdout on success, so a caller can capture it as the image to upload.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
import yaml

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

from headline_card_generator import (  # noqa: E402
    download_image,
    make_photo_card,
    make_plain_card,
    search_person_photo,
)
from extract import extract_subject  # noqa: E402


def _load_channel(pipeline: str) -> tuple[dict, dict]:
    cfg = yaml.safe_load((SKILL_DIR / "channels.yaml").read_text()) or {}
    if pipeline not in cfg:
        sys.exit(f"error: unknown pipeline {pipeline!r} — channels.yaml has: "
                 f"{[k for k in cfg if k in ('dn', 'ef')]}")
    return cfg[pipeline], cfg


def _resolve_path(p: str | None) -> str | None:
    """Resolve a channels.yaml path (relative → relative to skill dir)."""
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = SKILL_DIR / path
    return str(path)


def _resolve_logo(channel: dict) -> tuple[str | None, str]:
    logo = _resolve_path(channel.get("logo_path"))
    position = channel.get("logo_position", "top-right")
    if logo and not Path(logo).exists():
        print(f"note: logo not found at {logo} — rendering without a logo. "
              f"Drop the channel logo there to brand the card.", file=sys.stderr)
        logo = None
    return logo, position


def _resolve_font(cli_font: str | None, cfg: dict) -> tuple[str | None, int]:
    # Precedence: --font > HEADLINE_CARD_FONT env > channels.yaml > auto-detect.
    font = cli_font or os.environ.get("HEADLINE_CARD_FONT") or cfg.get("font_path") or None
    return _resolve_path(font), int(cfg.get("font_index", 0) or 0)


async def _fetch_photo(person: str) -> bytes | None:
    if not person:
        return None
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        url = await search_person_photo(client, person)
        return await download_image(client, url) if url else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a branded DN/EF headline card.")
    ap.add_argument("--pipeline", required=True, choices=["dn", "ef"],
                    help="which channel's branding to use")
    ap.add_argument("--title", required=True, help="headline text (auto-wrapped)")
    ap.add_argument("--person", default="",
                    help="explicit image-search term; overrides --auto extraction")
    ap.add_argument("--auto", action="store_true",
                    help="when no --person, extract the subject from the title (LLM) "
                         "and search a photo for it; falls back to a plain card if that "
                         "finds nothing. Uses OpenAI gpt-4o-mini by default "
                         "(OPENAI_API_KEY or openai.api_key); see extract.py for providers.")
    ap.add_argument("--description", default="",
                    help="optional article description, improves --auto extraction")
    ap.add_argument("--tag", default=None,
                    help="tag-badge text; overrides the channel default_tag")
    ap.add_argument("--out", default=None,
                    help="output PNG path (default: <skill>/output_<pipeline>.png)")
    ap.add_argument("--font", default=None, help="explicit .ttf/.ttc/.otf path")
    args = ap.parse_args()

    channel, cfg = _load_channel(args.pipeline)
    logo_path, logo_position = _resolve_logo(channel)
    font_path, font_index = _resolve_font(args.font, cfg)
    tag_text = channel.get("default_tag", "") if args.tag is None else args.tag
    out_path = args.out or str(SKILL_DIR / f"output_{args.pipeline}.png")

    font_kwargs = {}
    if font_path:
        font_kwargs = {"font_path": font_path, "font_index": font_index}

    # Decide the image-search term: explicit --person wins; else --auto extracts it.
    search_term = args.person
    if not search_term and args.auto:
        subj = extract_subject(args.title, args.description)
        search_term = subj["query"]
        if search_term:
            kind = subj["subject_type"]
            print(f"auto: subject={search_term!r} ({kind}) — searching a photo.",
                  file=sys.stderr)

    photo_bytes = asyncio.run(_fetch_photo(search_term))

    try:
        if photo_bytes:
            src = str(SKILL_DIR / f"_source_{args.pipeline}.jpg")
            Path(src).write_bytes(photo_bytes)
            make_photo_card(
                title=args.title, photo_path=src, out_path=out_path,
                tag_text=tag_text, logo_path=logo_path, logo_position=logo_position,
                **font_kwargs,
            )
        else:
            if search_term:
                print(f"note: no photo found for {search_term!r} — plain-background card.",
                      file=sys.stderr)
            make_plain_card(
                title=args.title, out_path=out_path,
                tag_text=tag_text, logo_path=logo_path, logo_position=logo_position,
                **font_kwargs,
            )
    except RuntimeError as e:  # raised by resolve_font when no font is available
        sys.exit(f"error: {e}")

    print(str(Path(out_path).resolve()))


if __name__ == "__main__":
    main()
