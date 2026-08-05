"""
Website Agent — scrapes defense/military websites for Epic Fury articles.

Per website:
  1. Auto-detect RSS feed (common paths) → feedparser if found
  2. Fallback: fetch homepage → extract article links → fetch each → trafilatura extract
  3. Keyword pre-filter (Epic Fury relevance)
  4. Time filter (filter_feed_hours)
  5. Redis SETNX dedup
  6. Build Article objects (has_video=True if page contains video embed)
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
import feedparser

from core.config import EpicFuryConfig
from core.models import Article
from core.redis_client import RedisClient
from utils.hashing import sha256_url_hash

logger = logging.getLogger(__name__)

EPICFURY_KEYWORDS = [
    "iran", "iranian", "irgc", "epic fury", "operation epic fury",
    "israel", "israeli", "idf", "netanyahu",
    "centcom", "u.s. military", "us military", "pentagon",
    "airstrike", "strike", "missile", "f-35", "b-2", "carrier",
    "nuclear", "natanz", "fordow", "khamenei", "tehran",
    "middle east", "persian gulf", "hormuz", "drone", "munition",
    "war", "offensive", "military operation", "air campaign",
    "ceasefire", "cease-fire", "negotiations", "sanctions",
    "hezbollah", "hamas", "houthi", "houthis",
    "lebanon", "gaza", "west bank", "yemen", "baghdad", "beirut",
    "tel aviv", "jerusalem", "proxy",
]

_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(k) for k in EPICFURY_KEYWORDS),
    re.IGNORECASE,
)

_RSS_PATHS = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/feeds/all"]

_VIDEO_PATTERN = re.compile(
    r'<video\b|youtube\.com/embed|player\.vimeo\.com|youtu\.be/|brightcove\.net|jwplayer|dailymotion\.com/embed',
    re.IGNORECASE,
)

_HTML_TAG_RE = re.compile(r'<[^>]*>', re.DOTALL)


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace. Used for RSS description fields."""
    text = _HTML_TAG_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()

_LINK_RE = re.compile(r'<a\b[^>]+\bhref=["\']([^"\']+)["\']', re.IGNORECASE)

# Paths that look like articles (contain date segment or /article/ etc.)
_ARTICLE_PATH_RE = re.compile(
    r'/\d{4}/\d{2}/|/article/|/articles/|/news/|/story/|/post/|/analysis/|/commentary/',
    re.IGNORECASE,
)

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "accept-language": "en-US,en;q=0.9",
}


def _passes_keyword_filter(title: str, description: str = "") -> bool:
    return bool(_KEYWORD_PATTERN.search(title) or _KEYWORD_PATTERN.search(description))


def _has_video(html: str) -> bool:
    return bool(_VIDEO_PATTERN.search(html))


def _parse_feedparser_date(entry: dict) -> datetime | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if pub_struct:
        try:
            return datetime.utcfromtimestamp(calendar.timegm(pub_struct)).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


async def _try_rss(
    session: aiohttp.ClientSession,
    base_url: str,
) -> list[feedparser.FeedParserDict] | None:
    """Try common RSS paths; return entries list or None if not found."""
    parsed_base = urlparse(base_url)
    root = f"{parsed_base.scheme}://{parsed_base.netloc}"

    for path in _RSS_PATHS:
        url = root + path
        try:
            async with session.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, allow_redirects=True) as resp:
                if resp.status != 200:
                    continue
                content_type = resp.content_type or ""
                if not any(x in content_type for x in ("xml", "rss", "atom", "feed")):
                    # Also accept text/html for poorly-typed feeds
                    body = await resp.read()
                    # Quick check for feed markers
                    if b"<rss" not in body and b"<feed" not in body and b"<channel>" not in body:
                        continue
                else:
                    body = await resp.read()
                loop = asyncio.get_event_loop()
                parsed = await loop.run_in_executor(None, feedparser.parse, body)
                if parsed.entries:
                    logger.debug("Found RSS at %s (%d entries)", url, len(parsed.entries))
                    return parsed.entries
        except Exception:
            continue
    return None


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            return await resp.text(errors="replace")
    except Exception as e:
        logger.debug("Fetch failed for %s: %s", url, e)
        return None


def _extract_article_links(html: str, base_url: str, max_links: int) -> list[str]:
    """Extract likely article URLs from a homepage."""
    parsed_base = urlparse(base_url)
    root = f"{parsed_base.scheme}://{parsed_base.netloc}"
    seen: set[str] = set()
    links: list[str] = []

    for m in _LINK_RE.finditer(html):
        href = m.group(1).strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        # Resolve relative links
        if href.startswith("//"):
            href = parsed_base.scheme + ":" + href
        elif href.startswith("/"):
            href = root + href
        elif not href.startswith("http"):
            href = urljoin(base_url, href)

        # Must be same domain
        lp = urlparse(href)
        if lp.netloc != parsed_base.netloc:
            continue

        # Must look like an article path
        if not _ARTICLE_PATH_RE.search(lp.path):
            continue

        # Strip query/fragment for dedup
        clean = f"{lp.scheme}://{lp.netloc}{lp.path}"
        if clean in seen:
            continue
        seen.add(clean)
        links.append(href)

        if len(links) >= max_links:
            break

    return links


async def _extract_with_trafilatura(html: str, url: str) -> dict | None:
    """Run trafilatura in thread executor; return dict or None."""
    import trafilatura
    loop = asyncio.get_event_loop()

    def _extract():
        result = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            output_format="python",
        )
        return result

    try:
        data = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=15)
        if not data or not isinstance(data, dict):
            return None
        if not data.get("title") and not data.get("text"):
            return None
        return data
    except Exception as e:
        logger.debug("trafilatura failed for %s: %s", url, e)
        return None


class WebsiteAgent:
    def __init__(
        self,
        config: EpicFuryConfig,
        redis: RedisClient,
        session: aiohttp.ClientSession,
        redis_key_overrides: dict | None = None,
    ) -> None:
        self._config = config
        self._redis = redis
        self._session = session
        overrides = redis_key_overrides or {}
        self._url_hash_prefix = overrides.get(
            "url_hash_key_prefix",
            redis._config.url_hash_key_prefix,
        )
        self._url_hash_ttl = overrides.get(
            "url_hash_ttl_s",
            redis._config.url_hash_ttl_s,
        )

    async def run(
        self,
        x_handles: list[str] = None,  # ignored — WebsiteAgent only handles websites
        website_urls: list[str] = None,
    ) -> tuple[list[Article], int]:
        """
        Scrape websites → keyword filter → dedup → return Articles.
        Returns (articles_after_dedup, raw_count_before_redis_dedup).
        """
        if not website_urls:
            return [], 0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._config.filter_feed_hours)
        max_per_site = self._config.max_articles_per_website

        raw_articles: list[dict] = []
        tasks = [self._scrape_website(url, cutoff, max_per_site) for url in website_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning("WebsiteAgent: scrape error: %s", result)
                continue
            if result:
                raw_articles.extend(result)

        if not raw_articles:
            return [], 0

        raw_count = len(raw_articles)

        # Compute URL hashes
        for art in raw_articles:
            art["url_hash"] = sha256_url_hash(art["url"])

        # Redis SETNX batch dedup
        key_value_pairs = [
            (f"{self._url_hash_prefix}{art['url_hash']}", art["published_at"])
            for art in raw_articles
        ]
        is_new = await self._redis.batch_setnx_with_ttl(key_value_pairs, self._url_hash_ttl)

        seen_hashes: set[str] = set()
        articles: list[Article] = []

        for art, new_in_redis in zip(raw_articles, is_new):
            h = art["url_hash"]
            if not new_in_redis or h in seen_hashes:
                continue
            seen_hashes.add(h)
            try:
                articles.append(Article(
                    url=art["url"],
                    title=(art.get("title") or art["url"])[:500],
                    description=(art.get("description") or "")[:1000] or None,
                    author=art.get("author"),
                    publishedAt=art["published_at"],
                    urlToImage=art.get("url_to_image"),
                    source=art.get("source", "Web"),
                    url_hash=h,
                    has_video=art.get("has_video", False),
                ))
            except Exception as e:
                logger.debug("WebsiteAgent: skipping malformed article %s: %s", art.get("url"), e)

        logger.info(
            "WebsiteAgent: %d new articles after dedup (from %d raw, %d sites)",
            len(articles), raw_count, len(website_urls),
        )
        return articles, raw_count

    async def _scrape_website(
        self,
        base_url: str,
        cutoff: datetime,
        max_per_site: int,
    ) -> list[dict]:
        """Try RSS first, then homepage scraping. Returns raw article dicts."""
        parsed = urlparse(base_url)
        source_name = parsed.netloc.replace("www.", "")

        # Try RSS discovery
        entries = await _try_rss(self._session, base_url)
        if entries:
            return await self._process_rss_entries(entries, source_name, cutoff, max_per_site)

        # Fallback: homepage scraping
        return await self._process_homepage(base_url, source_name, cutoff, max_per_site)

    async def _process_rss_entries(
        self,
        entries: list,
        source_name: str,
        cutoff: datetime,
        max_per_site: int,
    ) -> list[dict]:
        results = []
        for entry in entries[:max_per_site * 2]:  # fetch more than needed for keyword filtering
            url = entry.get("link", "")
            if not url:
                continue

            title = entry.get("title", "")
            desc = entry.get("summary") or entry.get("content", [{}])[0].get("value", "")

            # Time filter
            pub_dt = _parse_feedparser_date(entry)
            if pub_dt is None or pub_dt < cutoff:
                continue

            # Keyword pre-filter
            if not _passes_keyword_filter(title, desc):
                continue

            # Try to get image from media tags
            url_to_image = None
            for mc in entry.get("media_content", []):
                url_to_image = mc.get("url")
                break
            if not url_to_image:
                for mt in entry.get("media_thumbnail", []):
                    url_to_image = mt.get("url")
                    break
            if not url_to_image and desc:
                m = re.search(r'<img\b[^>]+\bsrc=["\']([^"\']+)["\']', desc, re.IGNORECASE)
                if m and not m.group(1).startswith("data:"):
                    url_to_image = m.group(1)

            desc_clean = _strip_html(desc) if desc else ""
            results.append({
                "url": url,
                "title": title,
                "description": desc_clean[:1000],
                "source": source_name,
                "published_at": pub_dt.isoformat(),
                "url_to_image": url_to_image,
                "has_video": False,  # RSS entries rarely flag video
            })

            if len(results) >= max_per_site:
                break

        return results

    async def _process_homepage(
        self,
        base_url: str,
        source_name: str,
        cutoff: datetime,
        max_per_site: int,
    ) -> list[dict]:
        html = await _fetch_html(self._session, base_url)
        if not html:
            return []

        links = _extract_article_links(html, base_url, max_per_site * 3)
        if not links:
            return []

        # Fetch and extract articles concurrently (max 5 at a time)
        sem = asyncio.Semaphore(5)
        results = []

        async def _process_link(url: str) -> dict | None:
            async with sem:
                page_html = await _fetch_html(self._session, url)
                if not page_html:
                    return None
                data = await _extract_with_trafilatura(page_html, url)
                if not data:
                    return None

                title = data.get("title") or ""
                text = data.get("text") or ""
                description = text[:300] if text else ""

                # Keyword pre-filter
                if not _passes_keyword_filter(title, description):
                    return None

                # Time filter: try to get date from trafilatura
                date_str = data.get("date")
                pub_dt: datetime | None = None
                if date_str:
                    try:
                        pub_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                        pub_dt = pub_dt.astimezone(timezone.utc)
                    except Exception:
                        pub_dt = None

                if pub_dt is None:
                    # No date — use now (conservative: may include old articles)
                    pub_dt = datetime.now(timezone.utc)
                elif pub_dt < cutoff:
                    return None

                # Extract image from meta tags
                url_to_image = None
                og_img_m = re.search(
                    r'<meta\b[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                    page_html, re.IGNORECASE,
                )
                if og_img_m:
                    url_to_image = og_img_m.group(1)
                if not url_to_image:
                    og_img_m2 = re.search(
                        r'<meta\b[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                        page_html, re.IGNORECASE,
                    )
                    if og_img_m2:
                        url_to_image = og_img_m2.group(1)

                return {
                    "url": url,
                    "title": title,
                    "description": description,
                    "author": data.get("author"),
                    "source": source_name,
                    "published_at": pub_dt.isoformat(),
                    "url_to_image": url_to_image,
                    "has_video": _has_video(page_html),
                }

        tasks = [_process_link(url) for url in links[:max_per_site * 2]]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in raw_results:
            if isinstance(r, dict) and r is not None:
                results.append(r)
            if len(results) >= max_per_site:
                break

        return results
