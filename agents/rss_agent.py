"""
RSS Agent — faithful Python port of resss_copy_v2.json workflow.

Replicates:
- Transform node: RSS entry → NewsAPI Article format
- limit node: UTC time window filtering
- link_url_hash node: SHA256 URL hash (strip query params, 16 chars)
- flattern node: dedup by url_hash, truncate fields to 1000 chars, trim whitespace
- Redis pipeline for batch SETNX dedup
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import logging
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import aiohttp
import feedparser

from core.config import RssConfig
from core.models import Article
from core.redis_client import RedisClient
from services.notion_client import RssSource
from utils.hashing import sha256_url_hash

logger = logging.getLogger(__name__)


def _url_hash(url: str) -> str:
    """Strip query string (split on '?'), faithful to n8n link_url_hash node."""
    return sha256_url_hash(url)


def _truncate(s: str | None, max_len: int = 1000) -> str | None:
    if not s:
        return s
    return s[:max_len] if len(s) > max_len else s


def _trim(s: str | None) -> str | None:
    return s.strip() if isinstance(s, str) else s


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'<[^>]+>', re.DOTALL)
# Block-level HTML tags that represent paragraph boundaries — replaced with newline
# so that line-based cleaners (_clean_scraped_body) can see each paragraph separately.
_HTML_BLOCK_RE = re.compile(
    r'</?(?:p|div|br|li|tr|h[1-6]|article|section|blockquote|figure|figcaption)'
    r'(?:\s[^>]*)?>',
    re.IGNORECASE,
)


def _strip_html(text: str) -> str:
    """Remove HTML tags, preserving block-level tag boundaries as newlines.

    Block-level tags (p, div, br, li, etc.) become newlines so the paragraph
    structure of RSS descriptions is preserved.  This is important for
    line-based content cleaners that run later (e.g. in claude_client.py).
    """
    text = _HTML_BLOCK_RE.sub('\n', text)   # block tags → newline
    text = _HTML_TAG_RE.sub(' ', text)       # remaining inline tags → space
    text = re.sub(r'[ \t]+', ' ', text)      # collapse horizontal whitespace
    text = re.sub(r'\n[ \t]+', '\n', text)   # strip leading spaces on each line
    text = re.sub(r'\n{3,}', '\n\n', text)   # max two consecutive blank lines
    return text.strip()


def _resolve_google_news_url(google_url: str, description: str | None) -> str:
    """
    Decode the real article URL from a news.google.com RSS link.

    Strategy 1: base64-decode the article ID — Google News encodes the real URL
    as a protobuf-like structure; searching for http(s) bytes works reliably.
    Strategy 2: extract first non-Google href from the description HTML.
    Falls back to the original URL if both fail.
    """
    # Strategy 1: base64 decode the article ID
    m = re.search(r'/articles/([A-Za-z0-9_=-]+)', google_url)
    if m:
        article_id = m.group(1)
        padded = article_id + '=' * (-len(article_id) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded)
            m2 = re.search(rb'https?://[^\x00-\x1f\x7f\s]+', decoded)
            if m2:
                real = m2.group(0).decode('utf-8', errors='ignore').rstrip('.')
                if 'google.com' not in real:
                    return real
        except Exception:
            pass

    # Strategy 2: extract real URL from description HTML anchor
    if description:
        for m3 in _HREF_RE.finditer(description):
            href = m3.group(1)
            if href.startswith('http') and 'google.com' not in href:
                return href

    return google_url


def _normalize_entry(entry: dict, feed_name: str = "RSS") -> dict | None:
    """
    Convert a feedparser entry to NewsAPI format.
    Replicates the Transform node in resss_copy_v2.json.
    """
    title = entry.get("title")
    url = entry.get("link")
    if not title or not url:
        return None

    description = entry.get("summary") or entry.get("content", [{}])[0].get("value")

    # Resolve Google News redirect URLs to the real article URL
    if url and "news.google.com" in url:
        url = _resolve_google_news_url(url, description)
    author = entry.get("author")

    # Use feedparser's pre-parsed time struct (UTC) when available — avoids RFC 2822 parsing issues
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if pub_struct:
        pub_date = datetime.utcfromtimestamp(calendar.timegm(pub_struct)).replace(tzinfo=timezone.utc).isoformat()
    else:
        pub_date = entry.get("published") or entry.get("updated")

    # Try image: media_content → media_thumbnail → enclosures → <img> in description
    url_to_image = None
    media = entry.get("media_content", [])
    if media:
        url_to_image = media[0].get("url")
    if not url_to_image:
        thumbnails = entry.get("media_thumbnail", [])
        if thumbnails:
            url_to_image = thumbnails[0].get("url")
    if not url_to_image:
        enclosures = entry.get("enclosures", [])
        for enc in enclosures:
            if enc.get("type", "").startswith("image/"):
                url_to_image = enc.get("url")
                break
    if not url_to_image and description:
        # Many news sites embed images in RSS description HTML (e.g. Reuters)
        m = re.search(r'<img\b[^>]+\bsrc=["\']([^"\']+)["\']', description, re.IGNORECASE)
        if m and not m.group(1).startswith("data:"):
            url_to_image = m.group(1)

    # Try video: enclosures (video/*) → media_content (video/*) → <video>/<source> in description
    _VIDEO_EXT_RE = re.compile(r'\.(mp4|mov|webm|m4v|avi)([?#]|$)', re.IGNORECASE)
    video_url = None
    for enc in entry.get("enclosures", []):
        enc_type = enc.get("type", "")
        enc_url = enc.get("url", "") or enc.get("href", "")
        if enc_type.startswith("video/") or _VIDEO_EXT_RE.search(enc_url):
            video_url = enc_url
            break
    if not video_url:
        for mc in entry.get("media_content", []):
            mc_type = mc.get("type", "")
            mc_url = mc.get("url", "")
            if mc_type.startswith("video/") or _VIDEO_EXT_RE.search(mc_url):
                video_url = mc_url
                break
    if not video_url and description:
        vm = re.search(
            r'<(?:video|source)\b[^>]+\bsrc=["\']([^"\']+\.(?:mp4|mov|webm|m4v))["\']',
            description, re.IGNORECASE,
        )
        if vm:
            video_url = vm.group(1)

    return {
        "title": title,
        "description": _strip_html(description) if description else description,
        "url": url,
        "urlToImage": url_to_image,
        "publishedAt": pub_date,
        "author": author,
        "source": {"id": "RSS", "name": feed_name},
        "cookie": None,
        "video_url": video_url,
        "has_video": bool(video_url),
    }


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse a date string into a UTC-aware datetime."""
    if not date_str:
        return None
    # ISO format (already set from pub_struct path)
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # RFC 2822 fallback (e.g. "Wed, 11 Mar 2026 21:52:30 GMT")
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        return None


# Retry-only headers. Publishers are split on which client they will talk to: some
# reject aiohttp's default "Python/3.x aiohttp/x.y.z" (judicialwatch + thenationalpulse
# 403, independent.co.uk 405, yahoo 429, aa.com.tr connection reset) while others reject
# a browser UA just as hard (npr and army.mil 403, newsmax hangs until timeout). Sending
# these by default measured as a NET LOSS across the 109 live feeds, so they are only
# ever used as a second attempt after the plain request has already failed.
_BROWSER_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _feed_request(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    timeout: int,
) -> tuple[bytes | None, str]:
    """Single feed GET. Returns (body, "") on HTTP 200, else (None, reason)."""
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            return await resp.read(), ""
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


async def _fetch_feed(
    session: aiohttp.ClientSession,
    source: RssSource,
    timeout: int,
    loop: asyncio.AbstractEventLoop,
) -> list[tuple]:
    """Fetch and parse a single RSS feed. Returns list of (entry, feed_title, cookie).

    A failed fetch is retried once posing as a browser — see _BROWSER_FEED_HEADERS for
    why that is a fallback and not the default. The retry gets a shorter window, since
    a feed that already failed should not be able to double the run's fetch time.
    """
    base = {"cookie": source.cookie} if source.cookie else {}

    raw, err = await _feed_request(session, source.url, base, timeout)
    if raw is None:
        retry_headers = {**_BROWSER_FEED_HEADERS, **base}
        raw, retry_err = await _feed_request(
            session, source.url, retry_headers, min(timeout, 15)
        )
        if raw is None:
            logger.warning(
                "Feed %s failed: %s (browser-UA retry: %s)", source.url, err, retry_err
            )
            return []
        logger.info(
            "Feed %s recovered on browser-UA retry (%s on first attempt)", source.url, err
        )

    try:
        # feedparser is synchronous — run in thread pool to stay non-blocking
        parsed = await loop.run_in_executor(None, feedparser.parse, raw)
        entries = parsed.get("entries", [])
        feed_title = parsed.feed.get("title", None) or source.name
        return [(e, feed_title, source.cookie) for e in entries]
    except Exception as e:
        logger.warning("Error parsing feed %s: %s", source.url, e)
        return []


class RssAgent:
    def __init__(
        self,
        config: RssConfig,
        redis: RedisClient,
        session: aiohttp.ClientSession,
    ) -> None:
        self._config = config
        self._redis = redis
        self._session = session
        self._loop = asyncio.get_event_loop()

    async def run(self, sources: list[RssSource]) -> tuple[list[Article], int]:
        """
        Fetch all RSS feeds from Notion sources, deduplicate, and return fresh Articles.
        Returns (articles_after_dedup, raw_count_before_redis_dedup).
        raw_count is used by the pipeline to show accurate url_dedup step counts.
        Equivalent to the full resss_copy_v2 workflow execution.
        """
        if not sources:
            logger.warning("No RSS sources provided (check Notion database).")
            return []

        # Skip Google News aggregator feeds — they produce title-only descriptions
        # with no images, making it impossible to generate quality posts.
        filtered = [s for s in sources if "news.google.com" not in s.url]
        skipped = len(sources) - len(filtered)
        if skipped:
            logger.info("Skipping %d Google News source(s)", skipped)
        sources = filtered

        semaphore = asyncio.Semaphore(self._config.concurrency)
        cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=self._config.filter_feed_hours)

        async def fetch_limited(source: RssSource) -> list[tuple]:
            async with semaphore:
                return await _fetch_feed(
                    self._session, source, self._config.fetch_timeout_s, self._loop
                )

        # Fetch all feeds concurrently
        results = await asyncio.gather(*[fetch_limited(s) for s in sources])

        # Collect and normalize all entries — replicate Transform + limit nodes
        raw_articles: list[dict] = []
        for feed_entries in results:
            per_source = 0
            for entry, feed_name, cookie in feed_entries:
                if per_source >= self._config.max_feed_per_source:
                    break
                normalized = _normalize_entry(entry, feed_name)
                if not normalized:
                    continue
                pub_date = _parse_date(normalized["publishedAt"])
                if pub_date is None or pub_date < cutoff_utc:
                    continue
                normalized["publishedAt"] = pub_date.isoformat()
                normalized["cookie"] = cookie  # carry per-source cookie
                raw_articles.append(normalized)
                per_source += 1

        if not raw_articles:
            return [], 0

        # Sort by publishedAt DESC (replicate filter_cnt sort)
        raw_articles.sort(key=lambda a: a["publishedAt"], reverse=True)

        # Cap at max_feed_items
        raw_articles = raw_articles[: self._config.max_feed_items]

        # Compute URL hashes (replicate link_url_hash node)
        for art in raw_articles:
            art["url_hash"] = _url_hash(art["url"])

        raw_count = len(raw_articles)  # count before Redis dedup (for url_dedup step metric)

        # Redis pipeline batch SETNX dedup (replicate flattern + Redis dedup)
        redis_key_prefix = self._redis._config.url_hash_key_prefix
        ttl = self._redis._config.url_hash_ttl_s
        key_value_pairs = [
            (f"{redis_key_prefix}{art['url_hash']}", art["publishedAt"])
            for art in raw_articles
        ]
        is_new = await self._redis.batch_setnx_with_ttl(key_value_pairs, ttl)

        # Deduplicate in-memory too (same url_hash seen twice in this batch)
        seen_hashes: set[str] = set()
        articles: list[Article] = []

        for art, new_in_redis in zip(raw_articles, is_new):
            h = art["url_hash"]
            if not new_in_redis or h in seen_hashes:
                continue
            seen_hashes.add(h)

            # Truncate and trim fields (replicate flattern node)
            try:
                articles.append(
                    Article(
                        url=_trim(art["url"]) or "",
                        title=_trim(_truncate(art["title"])) or "",
                        description=_trim(_truncate(art.get("description"))),
                        author=_trim(art.get("author")),
                        publishedAt=art["publishedAt"],
                        urlToImage=art.get("urlToImage"),
                        source=art.get("source", {}).get("name") if isinstance(art.get("source"), dict) else art.get("source"),
                        url_hash=h,
                        cookie=art.get("cookie"),
                        video_url=art.get("video_url"),
                        has_video=art.get("has_video", False),
                    )
                )
            except Exception as e:
                logger.debug("Skipping malformed article %s: %s", art.get("url"), e)

        logger.info("RSS agent: %d new articles after dedup (from %d raw)", len(articles), raw_count)
        return articles, raw_count
