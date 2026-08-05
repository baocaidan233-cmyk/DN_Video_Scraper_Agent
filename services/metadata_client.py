"""
MetadataClient — mirrors the retrieve_preview_metadata n8n subflow.

Routing (same as n8n Switch node):
  youtube.com / youtu.be   → YouTube Data API v3 thumbnail
  x.com / facebook.com     → urlmeta.org → self-hosted fallback
  no_preview_domains       → None immediately (blacklisted)
  everything else          → caps.gettr.com → urlmeta.org → self-hosted fallback

For all non-YouTube URLs the self-hosted URL resolver is called first to
unwrap redirect chains (Google News, shortlinks, etc.) before metadata fetch.

urlmeta.org auth: HTTP Basic — base64("apikey:") as Authorization header.
Self-hosted fallback: POST with X-API-Key header, returns {image, title, description}.
caps.gettr.com: Gettr's own scraping proxy; called as https://caps.gettr.com/<url>.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_YOUTUBE_ID_RE = re.compile(
    r'(?:[?&]v=|youtu\.be/|/embed/|/shorts/|/live/)([^?&#/]+)',
    re.IGNORECASE,
)

_URLMETA_URL = "https://api.urlmeta.org/meta"
_YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_TIMEOUT = aiohttp.ClientTimeout(total=12)

# Minimal OG/Twitter image extraction for caps.gettr.com HTML responses
_OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]+(?:property|name)\s*=\s*["\']og:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']'
    r'|<meta\b[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*(?:property|name)\s*=\s*["\']og:image["\']',
    re.IGNORECASE | re.DOTALL,
)
_TW_IMAGE_RE = re.compile(
    r'<meta\b[^>]+(?:property|name)\s*=\s*["\']twitter:image(?::src)?["\'][^>]*content\s*=\s*["\']([^"\']+)["\']'
    r'|<meta\b[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*(?:property|name)\s*=\s*["\']twitter:image(?::src)?["\']',
    re.IGNORECASE | re.DOTALL,
)


def _og_image_from_html(html: str) -> Optional[str]:
    for pat in (_OG_IMAGE_RE, _TW_IMAGE_RE):
        m = pat.search(html)
        if m:
            return m.group(1) or m.group(2) or None
    return None


def _extract_youtube_id(url: str) -> Optional[str]:
    m = _YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


def _basic_auth_header(api_key: str) -> str:
    """Build Authorization header value for HTTP Basic with api_key as username."""
    encoded = base64.b64encode(f"{api_key}:".encode()).decode()
    return f"Basic {encoded}"


class MetadataClient:
    def __init__(self, config, session: aiohttp.ClientSession) -> None:
        self._cfg = config
        self._session = session

    async def fetch_image_url(self, url: str) -> Optional[str]:
        """
        Return the best available image URL for the given article URL, or None.
        Mirrors n8n retrieve_preview_metadata subflow routing logic.

        Order of operations for non-YouTube URLs:
          1. Resolve redirect chain via self-hosted URL resolver (Gap 6)
          2. caps.gettr.com scraping proxy (Gap 2) — skipped for x.com/facebook.com
          3. urlmeta.org → self-hosted metadata fallback
        """
        if not url:
            return None

        lower = url.lower()

        # Blacklisted domains — no preview (mirrors n8n filter_blacklist + Switch NO_PREVIEW)
        for domain in (self._cfg.no_preview_domains or []):
            if domain and domain.lower() in lower:
                logger.debug("MetadataClient: no-preview domain for %s", url)
                return None

        # YouTube → YouTube Data API v3 (no URL resolver needed)
        if "youtube.com/" in lower or "youtu.be/" in lower:
            vid = _extract_youtube_id(url)
            if vid:
                img = await self._fetch_youtube_thumbnail(vid)
                if img:
                    return img
            logger.debug("MetadataClient: YouTube thumbnail not found for %s", url)
            return None

        # All other URLs: resolve redirect chain first (mirrors n8n 'get final url')
        resolved = await self._resolve_url(url)

        lower_resolved = resolved.lower()

        # x.com / facebook.com: urlmeta → self-hosted (caps.gettr.com not used for these)
        if (
            "x.com/" in lower_resolved
            or "twitter.com/" in lower_resolved
            or "facebook.com/" in lower_resolved
        ):
            return await self._fetch_urlmeta_with_fallback(resolved)

        # Everything else: caps.gettr.com first, then urlmeta → self-hosted
        img = await self._fetch_caps_gettr(resolved)
        if img:
            logger.debug("MetadataClient: caps.gettr.com image found for %s", resolved)
            return img

        return await self._fetch_urlmeta_with_fallback(resolved)

    async def _resolve_url(self, url: str) -> str:
        """Resolve redirect chain via self-hosted service (mirrors n8n 'get final url').

        Called for all non-YouTube URLs so Google News links, shortlinks, and other
        redirects are unwrapped before metadata fetching.
        """
        if not self._cfg.url_resolver_url:
            return url
        try:
            async with self._session.post(
                self._cfg.url_resolver_url,
                json={"url": url},
                headers={"X-API-Key": self._cfg.self_hosted_api_key or ""},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    resolved = data.get("final_url") or url
                    if resolved != url:
                        logger.debug(
                            "MetadataClient: URL resolved %s → %s", url[:60], resolved[:60]
                        )
                    return resolved
        except Exception as e:
            logger.debug("MetadataClient: URL resolver failed for %s: %s", url, e)
        return url

    async def _fetch_caps_gettr(self, url: str) -> Optional[str]:
        """Fetch og:image via Gettr's caps scraping proxy (mirrors n8n 'Get Metadata - HTTP Request').

        Gettr hosts a scraping proxy at https://caps.gettr.com/<full_url> that can
        bypass bot-detection on many news sites. Returns the og:image or twitter:image
        value from the fetched HTML.
        """
        try:
            caps_url = f"https://caps.gettr.com/{url}"
            async with self._session.get(
                caps_url,
                headers={"origin": "https://gettr.com", "referer": "https://gettr.com/"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        "MetadataClient: caps.gettr.com returned %d for %s", resp.status, url
                    )
                    return None
                html = await resp.text(errors="replace")
            return _og_image_from_html(html)
        except Exception as e:
            logger.debug("MetadataClient: caps.gettr.com failed for %s: %s", url, e)
            return None

    async def _fetch_youtube_thumbnail(self, video_id: str) -> Optional[str]:
        if not self._cfg.youtube_api_key:
            return None
        try:
            async with self._session.get(
                _YOUTUBE_API_URL,
                params={"id": video_id, "part": "snippet", "key": self._cfg.youtube_api_key},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.debug("YouTube API returned %d for video %s", resp.status, video_id)
                    return None
                data = await resp.json(content_type=None)
                items = data.get("items") or []
                if not items:
                    return None
                thumbs = items[0].get("snippet", {}).get("thumbnails", {})
                return (
                    thumbs.get("maxres", {}).get("url")
                    or thumbs.get("standard", {}).get("url")
                    or thumbs.get("high", {}).get("url")
                    or thumbs.get("medium", {}).get("url")
                    or thumbs.get("default", {}).get("url")
                )
        except Exception as e:
            logger.debug("YouTube API error for %s: %s", video_id, e)
            return None

    async def _fetch_urlmeta_with_fallback(self, url: str) -> Optional[str]:
        """urlmeta.org primary; if image missing or API unavailable → self-hosted fallback."""
        if self._cfg.urlmeta_api_key:
            try:
                async with self._session.get(
                    _URLMETA_URL,
                    params={"url": url},
                    headers={"Authorization": _basic_auth_header(self._cfg.urlmeta_api_key)},
                    timeout=_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        img = (data.get("meta") or {}).get("image")
                        if img:
                            logger.debug("MetadataClient: urlmeta image found for %s", url)
                            return img
                        logger.debug(
                            "MetadataClient: urlmeta returned no image for %s — trying fallback", url
                        )
                    else:
                        logger.debug("MetadataClient: urlmeta returned %d for %s", resp.status, url)
            except Exception as e:
                logger.debug("MetadataClient: urlmeta error for %s: %s", url, e)

        # Self-hosted fallback (mirrors n8n 'call self-hosted api' node)
        return await self._fetch_self_hosted(url)

    async def _fetch_self_hosted(self, url: str) -> Optional[str]:
        if not self._cfg.self_hosted_url:
            return None
        try:
            async with self._session.post(
                self._cfg.self_hosted_url,
                json={"url": url},
                headers={"X-API-Key": self._cfg.self_hosted_api_key or ""},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    img = data.get("image") or None
                    if img:
                        logger.debug("MetadataClient: self-hosted image found for %s", url)
                    return img
                logger.debug("MetadataClient: self-hosted returned %d for %s", resp.status, url)
        except Exception as e:
            logger.debug("MetadataClient: self-hosted error for %s: %s", url, e)
        return None
