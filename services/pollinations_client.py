"""
Pollinations.AI image generation client.

Generates contextual news images using the FLUX.1 model via Pollinations.AI —
completely free, no account or API key required.

API: GET https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&...
Rate limit: ~1 request / 15 s (anonymous)
Response: JPEG image bytes returned directly; same URL = same image (deterministic)
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

_BASE_URL = "https://image.pollinations.ai/prompt"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Optional API key — set at startup via configure() if pollinations_api_key is in config.
# Since Mar 2026, Pollinations.AI requires an API key (free: https://auth.pollinations.ai).
_API_KEY: str = ""


def configure(api_key: str) -> None:
    """Set the Pollinations API key. Call once at startup if key is available."""
    global _API_KEY
    _API_KEY = api_key or ""

# Source-logo detection moved to utils/logo_detect.py, which the video generator
# also uses. Re-exported here so existing imports keep working.
from utils.logo_detect import (  # noqa: E402
    GENERIC_IMAGE_PATTERNS,
    LOGO_ONLY_SOURCE_DOMAINS,
    looks_like_logo,
)


def needs_generated_image(url_to_image: str | None, article_url: str | None = None) -> bool:
    """Return True if this article should have an image generated via Pollinations."""
    if not url_to_image:
        return True
    return looks_like_logo(url_to_image, article_url)


def _build_url(title: str, url_hash: str | None) -> str:
    prompt = (
        f"photorealistic news photograph: {title}. "
        "Professional journalism photo, no text, no logos, no watermarks"
    )
    encoded = quote(prompt, safe="")

    # Derive deterministic seed from url_hash so the same article always gets
    # the same image — first 8 hex chars converted to int (fits in 32-bit range)
    seed = 0
    if url_hash and len(url_hash) >= 8:
        try:
            seed = int(url_hash[:8], 16)
        except ValueError:
            seed = 0

    url = f"{_BASE_URL}/{encoded}?model=flux&width=1280&height=720&nologo=true&seed={seed}"
    if _API_KEY:
        url += f"&key={_API_KEY}"
    return url


async def generate_news_image_url(
    session: aiohttp.ClientSession,
    title: str,
    url_hash: str | None = None,
) -> str | None:
    """Return a stable Pollinations.AI image URL for the article title.

    Verifies the URL is reachable (200 OK) before returning it.
    Returns None on any failure — never raises.
    """
    url = _build_url(title, url_hash)
    try:
        async with session.get(url, timeout=_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                logger.warning(
                    "Pollinations returned %d for title=%r", resp.status, title[:60]
                )
                return None
            await resp.content.read(256)
        logger.debug("Generated image URL for %r", title[:60])
        return url
    except Exception as e:
        logger.warning("Pollinations image generation failed (%s): %r", e, title[:60])
        return None


async def download_news_image_bytes(
    session: aiohttp.ClientSession,
    title: str,
    url_hash: str | None = None,
) -> bytes | None:
    """Download the full JPEG image bytes from Pollinations.

    Uses a 120-second timeout — FLUX generation can take up to ~60 s on cold cache.
    Returns raw bytes on success, None on any failure.
    """
    url = _build_url(title, url_hash)
    timeout = aiohttp.ClientTimeout(total=120)
    try:
        async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
            if resp.status != 200:
                logger.warning(
                    "Pollinations returned %d for title=%r", resp.status, title[:60]
                )
                return None
            data = await resp.read()
        logger.debug("Downloaded %d bytes for %r", len(data), title[:60])
        return data
    except Exception as e:
        logger.warning("Pollinations download failed (%s): %r", e, title[:60])
        return None
