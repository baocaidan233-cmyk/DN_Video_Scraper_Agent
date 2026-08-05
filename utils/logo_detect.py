"""
Detect "this isn't a photo of the story, it's the publisher's logo".

Some outlets set `og:image` to a masthead, a favicon or a fixed house graphic, so
the article technically has an image but the post ends up carrying nothing but a
source logo. Those articles are candidates for AI video generation.

Two independent signals; either one is enough:

  1. URL shape — the path or filename says logo/masthead/placeholder/etc.
     Works from a cold start, no state required.
  2. Repeat use — the same image URL has already been seen on N different
     articles. Catches house images whose URLs give nothing away, but only
     after it has been seen a few times.

Deliberately conservative: a false positive replaces a real photo with a
generated video, so both signals demand fairly explicit evidence. Every trip is
logged with the URL so mistakes are visible in the logs.

GENERIC_IMAGE_PATTERNS / LOGO_ONLY_SOURCE_DOMAINS were originally discovered for
services/pollinations_client.py, which still imports them from here.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from utils.hashing import sha256_url_hash

logger = logging.getLogger(__name__)

# URL substrings that identify generic source logos / placeholder images.
GENERIC_IMAGE_PATTERNS: tuple[str, ...] = (
    "rcom-default",                     # Reuters default image
    "reuters.com/pf/resources/images",  # Reuters resource images
)

# Domains that always use a site logo as their preview image, whatever
# url_to_image says.
LOGO_ONLY_SOURCE_DOMAINS: tuple[str, ...] = (
    "cls.cn",
)

# Filename / path evidence. Anchored on separators so "iconic" doesn't match
# "icon" and an article slug containing "default" doesn't match on its own.
_LOGO_PATH_RE = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:logos?|masthead|placeholder|fallback|watermark|favicon|avatar|brandmark"
    r"|no[-_]?image|og[-_]?image|share[-_]?image|social[-_]?image"
    r"|default[-_](?:image|img|thumb|thumbnail|photo|share|share[-_]image)"
    r"|(?:image|img|thumb|photo)[-_]default)"
    r"(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

# How many distinct articles must reuse one image before it is treated as a
# house graphic rather than a coincidence.
DEFAULT_REPEAT_THRESHOLD = 3
_SEEN_TTL_S = 7 * 24 * 3600


def looks_like_logo(url_to_image: str | None, article_url: str | None = None) -> bool:
    """Signal 1 — decide from the URL alone. No I/O."""
    if not url_to_image:
        return False
    lower = url_to_image.lower()
    # Already on the Gettr CDN: something upstream uploaded it deliberately.
    if "gettr.com" in lower:
        return False
    if any(p in lower for p in GENERIC_IMAGE_PATTERNS):
        return True
    if article_url and any(d in article_url.lower() for d in LOGO_ONLY_SOURCE_DOMAINS):
        return True
    # Match on path + filename only — query strings are full of unrelated tokens.
    return bool(_LOGO_PATH_RE.search(urlsplit(lower).path))


def seen_key(prefix: str, url_to_image: str) -> str:
    """Redis key counting how many articles have used this image.

    Keyed on the URL with query params stripped (sha256_url_hash), so the same
    house image served at several sizes still collides — which is what we want.
    """
    return f"{prefix}imgseen:{sha256_url_hash(url_to_image)}"


async def record_and_check_repeat(
    redis,
    prefix: str,
    url_to_image: str,
    threshold: int = DEFAULT_REPEAT_THRESHOLD,
) -> bool:
    """Signal 2 — count this image's use, and report whether it is now a repeat.

    Increments first, so the returned count includes the current article: with
    threshold 3, the third distinct article carrying the same image trips it.
    Fails open (returns False) — a Redis hiccup must never invent a logo.
    """
    if not url_to_image or threshold <= 0:
        return False
    try:
        key = seen_key(prefix, url_to_image)
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _SEEN_TTL_S)
        return count >= threshold
    except Exception as e:
        logger.warning("Repeat-image check failed for %s: %s", url_to_image, e)
        return False


async def is_source_logo(
    redis,
    prefix: str,
    url_to_image: str | None,
    article_url: str | None = None,
    threshold: int = DEFAULT_REPEAT_THRESHOLD,
) -> bool:
    """Both signals. Always records the sighting, even when signal 1 already hit,
    so the repeat counter stays accurate."""
    if not url_to_image:
        return False
    by_shape = looks_like_logo(url_to_image, article_url)
    by_repeat = await record_and_check_repeat(redis, prefix, url_to_image, threshold)
    if by_shape or by_repeat:
        logger.info(
            "Source-logo image detected (%s): %s",
            "url shape" if by_shape else f"reused by >={threshold} articles",
            url_to_image,
        )
        return True
    return False
