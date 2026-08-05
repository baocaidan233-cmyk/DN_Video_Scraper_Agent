"""
X Agent — scrapes tweets from specified handles.

Primary:  twitterapi.io (paid API, reliable, returns full media including video URLs)
Fallback: Twitter Syndication API (public embed endpoint, no auth)
Last resort: Nitter RSS (public, but no video URLs — fetches via CDN tweet-result)

Flow per run:
  1. For each handle: fetch via twitterapi.io, fall back to Syndication/Nitter
  2. Keyword pre-filter (Epic Fury relevance)
  3. Redis SETNX dedup (epicfury:title_hash:{sha256_16}, TTL 3h)
  4. Build Article objects (has_video=True if tweet has video)
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import json
import logging
import math
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
import feedparser

from core.config import XConfig, TwitterApiConfig, SocialDataConfig
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

# Built from config keywords at XAgent init; falls back to EPICFURY_KEYWORDS
_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(k) for k in EPICFURY_KEYWORDS),
    re.IGNORECASE,
)


def _build_keyword_pattern(keywords: list[str]) -> re.Pattern:
    if not keywords:
        keywords = EPICFURY_KEYWORDS
    return re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)

# Public Nitter instances — tried in order, first success wins per handle
_NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.1d4.us",
    "https://nitter.space",
]

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
_TWITTERAPI_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Headers for the Syndication API — mimics a browser loading the embed widget
_SYNDICATION_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://platform.twitter.com/",
    "origin": "https://platform.twitter.com",
}

# Headers for Nitter RSS fallback
_NITTER_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "accept": "application/rss+xml,application/xml,text/xml,*/*",
}

_SYNDICATION_BASE = "https://syndication.twitter.com/srv/timeline-profile/screen-name"
_NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_CDN_TWEET_RESULT = "https://cdn.syndication.twimg.com/tweet-result"


def _tweet_embed_token(tweet_id: str) -> str:
    """Compute the embed token required by cdn.syndication.twimg.com.

    Replicates Twitter embed widget JS:
      Math.round(tweetId / 1e15 * Math.PI).toString(36)
    """
    try:
        n = round(int(tweet_id) / 1e15 * math.pi)
        if n == 0:
            return "0"
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        result = ""
        while n > 0:
            result = digits[n % 36] + result
            n //= 36
        return result
    except Exception:
        return "1"


async def _fetch_tweet_video_url(
    session: aiohttp.ClientSession,
    tweet_id: str,
) -> str | None:
    """Fetch the highest-bitrate MP4 URL for a tweet via the CDN tweet-result endpoint.

    This endpoint serves individual tweet embeds (Twitter's own widget uses it) and
    has a separate rate-limit bucket from the Syndication timeline API.  It returns
    JSON with a top-level `mediaDetails` array containing `video_info.variants`.
    """
    url = f"{_CDN_TWEET_RESULT}?id={tweet_id}&lang=en&token={_tweet_embed_token(tweet_id)}"
    try:
        async with session.get(url, headers=_SYNDICATION_HEADERS, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                logger.debug("tweet-result API %d for tweet %s", resp.status, tweet_id)
                return None
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.debug("tweet-result fetch failed for tweet %s: %s", tweet_id, e)
        return None

    for media in data.get("mediaDetails") or []:
        mtype = media.get("type", "")
        if mtype in ("video", "animated_gif"):
            variants = (media.get("video_info") or {}).get("variants", [])
            mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
            if mp4s:
                return max(mp4s, key=lambda v: v.get("bitrate") or 0).get("url")
    return None


def _gather_media_items(tweet: dict, rt: dict) -> list[dict]:
    """Collect all media items from a tweet across every entity source.

    Checks (in order, deduplicating by media_id_str / media_url_https):
      1. tweet extendedEntities / extended_entities — primary media
      2. retweeted_tweet / retweeted_status         — when the tweet is an RT
      3. quoted_tweet / quoted_status               — when the tweet quotes another
         tweet that carries its own media (e.g. outer tweet = video, quoted = photo)

    Handles both twitterapi.io (camelCase) and Syndication API (snake_case) keys.
    Returns a deduplicated flat list of media item dicts ready for
    _extract_media_from_extended_entities.
    """
    seen: set[str] = set()
    items: list[dict] = []

    def _add(obj: dict) -> None:
        # Accept camelCase (twitterapi.io) or snake_case (Syndication)
        entities = obj.get("extendedEntities") or obj.get("extended_entities") or {}
        for m in (entities.get("media") or []):
            key = m.get("id_str") or m.get("media_url_https") or m.get("media_url") or ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            items.append(m)

    _add(tweet)
    if rt:
        _add(rt)
    # Quote tweet: twitterapi.io uses "quoted_tweet", Syndication uses "quoted_status"
    qt = tweet.get("quoted_tweet") or tweet.get("quoted_status") or {}
    if qt:
        _add(qt)

    return items


def _extract_media_from_extended_entities(
    ext_entities: dict,
) -> tuple[str | None, str | None, list[str], bool]:
    """Extract (url_to_image, video_url, all_media_urls, has_video) from extendedEntities.

    Works for both twitterapi.io (camelCase extendedEntities) and Syndication API
    (snake_case extended_entities / entities) — both use the same media array structure.

    video_url      — first (highest-bitrate) video URL for backward compat
    all_media_urls — ALL uploadable URLs: videos first, then photos.  Stored in
                     Article.video_urls and returned by _build_media_list so every
                     media item (e.g. 1 video + 1 photo in the same tweet) gets
                     uploaded and embedded in the Gettr post.
    """
    media_items = ext_entities.get("media", [])
    has_video = False
    url_to_image: str | None = None
    video_url: str | None = None
    video_urls: list[str] = []
    photo_urls: list[str] = []

    for media in media_items:
        mtype = media.get("type", "")
        if mtype in ("video", "animated_gif"):
            has_video = True
            vi = media.get("video_info", {})
            variants = vi.get("variants", [])
            # Prefer explicit video/mp4 content_type; also catch variants where the URL
            # contains .mp4 but content_type is missing or non-standard.
            mp4s = [
                v for v in variants
                if "video/mp4" in v.get("content_type", "")
                or v.get("url", "").split("?")[0].lower().endswith(".mp4")
            ]
            if mp4s:
                best = max(mp4s, key=lambda v: v.get("bitrate") or 0).get("url")
            else:
                # Skip HLS (.m3u8 / application/x-mpegURL) — can't be uploaded to Gettr CDN.
                non_hls = [
                    v for v in variants
                    if "mpegURL" not in v.get("content_type", "")
                    and not v.get("url", "").split("?")[0].lower().endswith(".m3u8")
                ]
                best = non_hls[0].get("url") if non_hls else None
            if best:
                video_urls.append(best)
                if not video_url:
                    video_url = best
            # Video thumbnail as url_to_image (use first video's thumbnail)
            if not url_to_image:
                thumb = media.get("media_url_https", "") or media.get("media_url", "")
                # Reject relative Gettr CDN paths returned by some API providers
                if thumb and thumb.startswith("http"):
                    url_to_image = thumb
        elif mtype == "photo":
            img = media.get("media_url_https", "") or media.get("media_url", "")
            if img and not img.startswith("http"):
                logger.debug("Skipping non-HTTP photo URL: %s", img)
                continue
            if img:
                # Skip video thumbnails returned as photos — uploading them as images
                # produces a static frame instead of a video on Gettr.
                if "video_thumb" in img:
                    logger.debug("Skipping video thumbnail masquerading as photo: %s", img)
                    continue
                img = re.sub(r"([?&])name=\w+", r"\1name=orig", img)
                if "name=" not in img:
                    sep = "&" if "?" in img else "?"
                    img = f"{img}{sep}name=orig"
                photo_urls.append(img)
                # Use first photo as url_to_image only if no video thumbnail yet
                if not url_to_image:
                    url_to_image = img

    # Videos first so Gettr uses the first upload as the primary video slot;
    # photos follow and land in imgs[] (their GCP metadata has no m3u8 key).
    return url_to_image, video_url, video_urls + photo_urls, has_video


# ---------------------------------------------------------------------------
# Tweet text helpers
# ---------------------------------------------------------------------------

# Matches a bare t.co short-URL (possibly truncated, e.g. "https://t" or "https://t.co/…")
# at the end of tweet text after a word boundary / whitespace.
_TCO_TRAILING_RE = re.compile(r'\s*https?://t(?:\.co/\S*)?\s*$')


def _expand_tweet_text(tweet: dict) -> str:
    """Return the full, clean text of a tweet.

    1. Prefer ``full_text`` (extended tweets) over ``text`` (compat-truncated).
    2. Expand t.co short URLs to their original expanded forms using ``entities.urls``.
    3. Strip any remaining trailing t.co link (media attachments, self-referential tweet URL).
    """
    text = (tweet.get("full_text") or tweet.get("text") or "").strip()

    # Build t.co → expanded-URL map from entities
    url_map: dict[str, str] = {}
    for u in (tweet.get("entities") or {}).get("urls") or []:
        short = u.get("url") or ""
        expanded = u.get("expandedUrl") or u.get("expanded_url") or ""
        if short and expanded:
            url_map[short] = expanded

    # Replace t.co links with their expanded forms
    for short, expanded in url_map.items():
        text = text.replace(short, expanded)

    # Remove trailing t.co / pic.twitter.com links (media already handled separately)
    text = _TCO_TRAILING_RE.sub("", text)
    # Also strip trailing pic.twitter.com links
    text = re.sub(r'\s*https?://pic\.twitter\.com/\S*\s*$', "", text)

    return text.strip()


# ---------------------------------------------------------------------------
# twitterapi.io primary fetcher
# ---------------------------------------------------------------------------

def _parse_twitter_date(date_str: str) -> datetime:
    """Parse Twitter v1.1 date format: "Sun Mar 22 16:31:43 +0000 2026"."""
    try:
        return datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return datetime.now(timezone.utc)


async def _fetch_twitterapi(
    session: aiohttp.ClientSession,
    api_key: str,
    base_url: str,
    handle: str,
    cutoff: datetime,
    limit: int,
    keyword_pattern: re.Pattern = _KEYWORD_PATTERN,
) -> list[dict]:
    """Fetch tweets for a handle via twitterapi.io.

    Returns raw article dicts. Returns [] on error (caller falls back to Syndication).
    """
    handle = handle.lstrip("@")
    headers = {"X-API-Key": api_key}
    url = f"{base_url}/twitter/user/last_tweets"
    params = {"userName": handle, "cursor": "", "includeReplies": "false"}

    try:
        async with session.get(
            url, headers=headers, params=params, timeout=_TWITTERAPI_TIMEOUT
        ) as resp:
            if resp.status != 200:
                logger.warning("twitterapi.io %d for @%s", resp.status, handle)
                return []
            raw = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("twitterapi.io fetch failed for @%s: %s", handle, e)
        return []

    if raw.get("status") != "success" and raw.get("code") != 0:
        logger.warning("twitterapi.io non-success for @%s: %s", handle, raw.get("msg"))
        return []

    tweets = raw.get("data", {}).get("tweets", [])
    logger.debug("twitterapi.io @%s: %d tweets returned", handle, len(tweets))

    articles: list[dict] = []
    for tweet in tweets[:limit]:
        tweet_dt = _parse_twitter_date(tweet.get("createdAt", ""))
        if tweet_dt.tzinfo is None:
            tweet_dt = tweet_dt.replace(tzinfo=timezone.utc)
        if tweet_dt < cutoff:
            continue

        # Use full_text (extended tweets) with t.co URLs expanded; fall back to text.
        # For retweets, prefer the original tweet's full text over the "RT @…: …" prefix.
        rt = tweet.get("retweeted_tweet") or {}
        if rt:
            text = _expand_tweet_text(rt) or _expand_tweet_text(tweet)
        else:
            text = _expand_tweet_text(tweet)
        if not text:
            continue

        # For keyword filtering also check the raw compat text of a retweet
        rt_text = rt.get("full_text") or rt.get("text", "")
        if not _passes_keyword_filter(text, rt_text, keyword_pattern):
            continue

        tweet_id = tweet.get("id", "")
        tweet_url = tweet.get("url") or f"https://x.com/{handle}/status/{tweet_id}"
        # Normalize to x.com
        tweet_url = tweet_url.replace("twitter.com/", "x.com/")

        screen_name = (tweet.get("author") or {}).get("userName") or handle

        # Collect media from tweet + retweeted_tweet + quoted_tweet (combined, deduped)
        combined_ext = {"media": _gather_media_items(tweet, rt)}
        url_to_image, video_url, video_urls, has_video = _extract_media_from_extended_entities(combined_ext)

        logger.debug(
            "twitterapi.io @%s tweet %s: image=%s video=%s media=%d has_video=%s",
            screen_name, tweet_id, url_to_image, video_url, len(video_urls), has_video,
        )

        articles.append({
            "url": tweet_url,
            "title": text,
            "description": "",
            "source": f"@{screen_name}",
            "published_at": tweet_dt.isoformat(),
            "url_to_image": url_to_image,
            "video_url": video_url,
            "video_urls": video_urls,
            "has_video": has_video,
        })

    return articles


# ---------------------------------------------------------------------------
# Syndication API fallback
# ---------------------------------------------------------------------------

def _decode_nitter_media_url(url: str) -> str:
    """Convert a Nitter-proxied media URL to a direct pbs.twimg.com / video.twimg.com URL."""
    if not url or "nitter" not in url:
        return url

    m = re.search(r"/pic/enc/([A-Za-z0-9_=-]+)", url)
    if m:
        try:
            padded = m.group(1) + "=" * (-len(m.group(1)) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass

    m = re.search(r"/pic/(?:orig/)?(.+?)(?:\?|$)", url)
    if m:
        decoded = urllib.parse.unquote(m.group(1))
        decoded = re.sub(r":(orig|large|medium|small|thumb)$", "", decoded)
        if not decoded.startswith("http"):
            cdn_url = f"https://pbs.twimg.com/{decoded}"
            if not re.search(r"\.[a-z0-9]{2,5}$", cdn_url.split("?")[0], re.IGNORECASE):
                sep = "&" if "?" in cdn_url else "?"
                cdn_url = f"{cdn_url}{sep}format=jpg&name=orig"
            return cdn_url
        return decoded

    return url


def _passes_keyword_filter(title: str, description: str = "", pattern: re.Pattern = _KEYWORD_PATTERN) -> bool:
    return bool(pattern.search(title) or pattern.search(description))


def _parse_feedparser_date(entry) -> datetime | None:
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if pub_struct:
        try:
            return datetime.utcfromtimestamp(calendar.timegm(pub_struct)).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _collect_tweets(obj, results: list, depth: int = 0) -> None:
    """Recursively walk the __NEXT_DATA__ JSON to find tweet objects."""
    if depth > 15:
        return
    if isinstance(obj, dict):
        if "id_str" in obj and ("full_text" in obj or "text" in obj):
            results.append(obj)
            return
        for v in obj.values():
            _collect_tweets(v, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_tweets(item, results, depth + 1)


async def _fetch_syndication(
    session: aiohttp.ClientSession,
    handle: str,
    cutoff: datetime,
    limit: int,
    keyword_pattern: re.Pattern = _KEYWORD_PATTERN,
) -> list[dict]:
    """Fetch tweets for a handle via the Twitter Syndication API (no auth required)."""
    handle = handle.lstrip("@")
    url = f"{_SYNDICATION_BASE}/{handle}?count={limit}&showReplies=false"

    try:
        async with session.get(url, headers=_SYNDICATION_HEADERS, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status == 429:
                logger.debug("Syndication 429 (rate limited) for @%s — falling back to Nitter", handle)
                return []
            if resp.status != 200:
                logger.debug("Syndication API %d for @%s", resp.status, handle)
                return []
            html = await resp.text()
    except Exception as e:
        logger.debug("Syndication fetch failed for @%s: %s", handle, e)
        return []

    m = _NEXT_DATA_RE.search(html)
    if not m:
        logger.debug("Syndication: no __NEXT_DATA__ for @%s", handle)
        return []

    try:
        data = json.loads(m.group(1))
    except Exception as e:
        logger.debug("Syndication: JSON parse error for @%s: %s", handle, e)
        return []

    raw_tweets: list[dict] = []
    props = data.get("props", {}).get("pageProps", {})
    _collect_tweets(props, raw_tweets)

    logger.debug("Syndication @%s: %d raw tweet objects", handle, len(raw_tweets))

    articles: list[dict] = []
    for tweet in raw_tweets:
        try:
            tweet_dt = datetime.strptime(tweet["created_at"], "%a %b %d %H:%M:%S %z %Y")
        except Exception:
            tweet_dt = datetime.now(timezone.utc)
        if tweet_dt.tzinfo is None:
            tweet_dt = tweet_dt.replace(tzinfo=timezone.utc)
        if tweet_dt < cutoff:
            continue

        text = _expand_tweet_text(tweet)
        if not text:
            continue
        if not _passes_keyword_filter(text, pattern=keyword_pattern):
            continue

        user = tweet.get("user", {})
        screen_name = user.get("screen_name", handle)

        tweet_id = tweet.get("id_str", "")
        tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

        # Collect media from tweet + quoted_status (combined, deduped)
        syn_rt = tweet.get("retweeted_status") or {}
        combined_ext = {"media": _gather_media_items(tweet, syn_rt)}
        url_to_image, video_url, video_urls, has_video = _extract_media_from_extended_entities(combined_ext)

        logger.debug(
            "Syndication @%s tweet %s: image=%s video=%s media=%d has_video=%s",
            screen_name, tweet_id, url_to_image, video_url, len(video_urls), has_video,
        )
        articles.append({
            "url": tweet_url,
            "title": text,
            "description": "",
            "source": f"@{screen_name}",
            "published_at": tweet_dt.isoformat(),
            "url_to_image": url_to_image,
            "video_url": video_url,
            "video_urls": video_urls,
            "has_video": has_video,
        })

    return articles


def _extract_nitter_media(html: str) -> tuple[str | None, str | None]:
    """Extract (video_url, url_to_image) from Nitter RSS summary HTML."""
    video_url: str | None = None
    image_url: str | None = None

    for video_block in re.finditer(r"<video[^>]*>(.*?)</video>", html, re.DOTALL | re.IGNORECASE):
        block = video_block.group(1)
        for src_m in re.finditer(r'<source\s[^>]*src="([^"]+)"[^>]*>', block, re.IGNORECASE):
            src = src_m.group(1)
            type_m = re.search(r'type="([^"]+)"', src_m.group(0), re.IGNORECASE)
            ctype = type_m.group(1) if type_m else ""
            if "mp4" in ctype or src.endswith(".mp4"):
                video_url = video_url or src
                break
        if not video_url:
            src_m = re.search(r'<source\s[^>]*src="([^"]+)"', block, re.IGNORECASE)
            if src_m:
                video_url = video_url or src_m.group(1)

    if not image_url:
        img_m = re.search(r'<img\s[^>]*src="([^"]+)"', html, re.IGNORECASE)
        if img_m:
            raw = img_m.group(1)
            image_url = _decode_nitter_media_url(raw) if "nitter" in raw else raw

    return video_url, image_url


def _clean_nitter_summary(html: str) -> str:
    """Strip Nitter RSS summary HTML into plain text."""
    html = re.sub(
        r"<(video|audio|picture|figure)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<(img|source|track)[^>]*/?>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_nitter_rss(
    session: aiohttp.ClientSession,
    handle: str,
    cutoff: datetime,
    limit: int,
    keyword_pattern: re.Pattern = _KEYWORD_PATTERN,
) -> list[dict]:
    """Try each Nitter instance until one returns RSS entries for the handle."""
    handle = handle.lstrip("@")

    for base in _NITTER_INSTANCES:
        url = f"{base}/{handle}/rss"
        try:
            async with session.get(url, headers=_NITTER_HEADERS, timeout=_FETCH_TIMEOUT, allow_redirects=True) as resp:
                if resp.status != 200:
                    continue
                body = await resp.read()
                if b"<rss" not in body and b"<feed" not in body:
                    continue

                loop = asyncio.get_event_loop()
                parsed = await loop.run_in_executor(None, feedparser.parse, body)
                if not parsed.entries:
                    continue

                logger.debug("Nitter RSS for @%s via %s: %d entries", handle, base, len(parsed.entries))
                articles = []
                for entry in parsed.entries[:limit]:
                    pub_dt = _parse_feedparser_date(entry)
                    if pub_dt and pub_dt < cutoff:
                        continue

                    title = entry.get("title", "").strip()
                    summary = entry.get("summary", "")

                    if not _passes_keyword_filter(title, summary, keyword_pattern):
                        continue

                    link = entry.get("link", "")
                    link = re.sub(
                        r"https?://[^/]+/([^/]+)/status/(\d+)[^?\s]*",
                        r"https://x.com/\1/status/\2",
                        link,
                    )

                    html_video_url, html_image_url = _extract_nitter_media(summary)
                    video_url = html_video_url
                    url_to_image = html_image_url

                    for mc in entry.get("media_content", []):
                        mc_url = mc.get("url", "")
                        mc_type = mc.get("type", "")
                        if "video" in mc_type or mc_url.endswith(".mp4"):
                            video_url = video_url or mc_url
                        elif not url_to_image:
                            url_to_image = _decode_nitter_media_url(mc_url)
                    for enc in entry.get("enclosures", []):
                        enc_url = enc.get("url", "")
                        enc_type = enc.get("type", "")
                        if "video" in enc_type or enc_url.endswith(".mp4"):
                            video_url = video_url or enc_url
                        elif not url_to_image and "image" in enc_type:
                            url_to_image = _decode_nitter_media_url(enc_url)

                    has_video = bool(
                        video_url
                        or re.search(r'<video\b|\.mp4', summary, re.IGNORECASE)
                        or (url_to_image and re.search(r'video_thumb', url_to_image))
                    )

                    if has_video and not video_url:
                        tweet_id_m = re.search(r"/status/(\d+)", link)
                        if tweet_id_m:
                            fetched_vid = await _fetch_tweet_video_url(session, tweet_id_m.group(1))
                            if fetched_vid:
                                video_url = fetched_vid
                                logger.debug(
                                    "Nitter @%s: got video URL via tweet-result for %s",
                                    handle, link,
                                )

                    logger.debug(
                        "Nitter @%s: video_url=%s url_to_image=%s",
                        handle, video_url, url_to_image,
                    )

                    articles.append({
                        "url": link or f"https://x.com/{handle}",
                        "title": title,
                        "description": _clean_nitter_summary(summary)[:500],
                        "source": f"@{handle}",
                        "published_at": (pub_dt or datetime.now(timezone.utc)).isoformat(),
                        "url_to_image": url_to_image,
                        "video_url": video_url,
                        "has_video": has_video,
                    })

                return articles

        except Exception as e:
            logger.debug("Nitter %s failed for @%s: %s", base, handle, e)
            continue

    logger.warning("XAgent: all Nitter instances failed for @%s", handle)
    return []


# ---------------------------------------------------------------------------
# socialdata.tools primary fetcher
# ---------------------------------------------------------------------------

_SOCIALDATA_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _fetch_socialdata(
    session: aiohttp.ClientSession,
    api_key: str,
    base_url: str,
    handle: str,
    cutoff: datetime,
    limit: int,
    keyword_pattern: re.Pattern = _KEYWORD_PATTERN,
) -> list[dict]:
    """Fetch tweets for a handle via socialdata.tools.

    Endpoint: GET /twitter/search?query=from:<handle>&tweet_type=Latest
    Auth:     Authorization: Bearer <api_key>
    Dates:    ISO 8601 in tweet_created_at ("2026-04-14T00:13:52.000000Z")
    Media:    in entities.media (flat list), extended_entities is None
    """
    handle = handle.lstrip("@")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    url = f"{base_url}/twitter/search"
    params = {
        "query": f"from:{handle}",
        "tweet_type": "Latest",
    }

    try:
        async with session.get(
            url, headers=headers, params=params, timeout=_SOCIALDATA_TIMEOUT
        ) as resp:
            if resp.status == 402:
                logger.warning("socialdata.tools 402 (quota/payment) for @%s", handle)
                return []
            if resp.status != 200:
                logger.warning("socialdata.tools %d for @%s", resp.status, handle)
                return []
            raw = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("socialdata.tools fetch failed for @%s: %s", handle, e)
        return []

    tweets = raw.get("tweets") or []
    logger.debug("socialdata.tools @%s: %d tweets returned", handle, len(tweets))

    articles: list[dict] = []
    for tweet in tweets[:limit]:
        # socialdata.tools uses ISO 8601: "2026-04-14T00:13:52.000000Z"
        try:
            tweet_dt = datetime.fromisoformat(
                (tweet.get("tweet_created_at") or "").replace("Z", "+00:00")
            )
        except Exception:
            tweet_dt = datetime.now(timezone.utc)
        if tweet_dt.tzinfo is None:
            tweet_dt = tweet_dt.replace(tzinfo=timezone.utc)
        if tweet_dt < cutoff:
            continue

        # For retweets, prefer the original tweet's text
        rt = tweet.get("retweeted_status") or {}
        if rt:
            text = _expand_tweet_text(rt) or _expand_tweet_text(tweet)
        else:
            text = _expand_tweet_text(tweet)
        if not text:
            continue

        rt_text = rt.get("full_text") or rt.get("text", "")
        if not _passes_keyword_filter(text, rt_text, keyword_pattern):
            continue

        user = tweet.get("user") or {}
        screen_name = user.get("screen_name") or handle
        tweet_id = tweet.get("id_str") or str(tweet.get("id", ""))
        tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

        # socialdata puts media in entities.media (flat list); extended_entities is None
        raw_media = (tweet.get("entities") or {}).get("media") or []
        rt_media = (rt.get("entities") or {}).get("media") or []
        # Deduplicate by id_str
        seen_ids: set[str] = set()
        combined_media: list[dict] = []
        for m in raw_media + rt_media:
            key = m.get("id_str") or m.get("media_url_https") or ""
            if key not in seen_ids:
                seen_ids.add(key)
                combined_media.append(m)
        url_to_image, video_url, video_urls, has_video = _extract_media_from_extended_entities(
            {"media": combined_media}
        )

        logger.debug(
            "socialdata.tools @%s tweet %s: image=%s video=%s media=%d has_video=%s",
            screen_name, tweet_id, url_to_image, video_url, len(video_urls), has_video,
        )

        articles.append({
            "url": tweet_url,
            "title": text,
            "description": "",
            "source": f"@{screen_name}",
            "published_at": tweet_dt.isoformat(),
            "url_to_image": url_to_image,
            "video_url": video_url,
            "video_urls": video_urls,
            "has_video": has_video,
        })

    return articles


# ---------------------------------------------------------------------------
# XAgent class
# ---------------------------------------------------------------------------

class XAgent:
    def __init__(
        self,
        config: XConfig,
        redis: RedisClient,
        redis_key_overrides: dict | None = None,
        session: aiohttp.ClientSession | None = None,
        twitterapi_config: TwitterApiConfig | None = None,
        socialdata_config: SocialDataConfig | None = None,
        x_scraper: str = "twitterapi",
        keywords: list[str] | None = None,
        state=None,
    ) -> None:
        self._config = config
        self._redis = redis
        self._session = session
        self._twitterapi = twitterapi_config
        self._socialdata = socialdata_config
        self._x_scraper = x_scraper  # "twitterapi" | "socialdata"
        self._state = state          # DashboardState — used to read live interval
        self._keyword_pattern = _build_keyword_pattern(keywords or [])
        overrides = redis_key_overrides or {}
        self._url_hash_prefix = overrides.get(
            "url_hash_key_prefix",
            redis._config.url_hash_key_prefix,
        )
        self._url_hash_ttl = overrides.get(
            "url_hash_ttl_s",
            redis._config.url_hash_ttl_s,
        )

    def set_scraper(self, scraper: str) -> None:
        """Hot-switch the primary scraper: 'twitterapi' or 'socialdata'."""
        if scraper not in ("twitterapi", "socialdata"):
            raise ValueError(f"Unknown scraper: {scraper!r}")
        self._x_scraper = scraper
        logger.info("XAgent: switched primary scraper to %s", scraper)

    async def setup(self) -> None:
        if self._x_scraper == "socialdata" and self._socialdata and self._socialdata.api_key:
            logger.info("XAgent: using socialdata.tools as primary source (with Syndication/Nitter fallback)")
        elif self._twitterapi and self._twitterapi.api_key:
            logger.info("XAgent: using twitterapi.io as primary source (with Syndication/Nitter fallback)")
        else:
            logger.info("XAgent: no paid API configured — using Syndication API / Nitter fallback")

    async def run(
        self,
        x_handles: list[str],
        website_urls: list[str] = None,  # ignored
    ) -> tuple[list[Article], int]:
        if not x_handles:
            return [], 0

        # Auto-adjust limit based on the live scraping interval:
        # assume at most 1 tweet per minute per handle, with a floor of 5.
        # This keeps fetch volume proportional to how often we scrape and
        # automatically reduces calls when the interval is shorter.
        if self._state is not None:
            limit = max(5, math.ceil(self._state.rss_interval_s / 60))
        else:
            limit = self._config.tweets_per_account
        logger.debug("XAgent: tweets_per_account limit=%d (interval=%ds)",
                     limit, self._state.rss_interval_s if self._state else -1)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=3)

        raw_articles = await self._fetch_all(x_handles, limit, cutoff)

        if not raw_articles:
            return [], 0

        raw_count = len(raw_articles)

        # URL hash + Redis SETNX dedup
        for art in raw_articles:
            art["url_hash"] = sha256_url_hash(art["url"])

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
                    title=art["title"],
                    description=art.get("description") or None,
                    publishedAt=art["published_at"],
                    urlToImage=art.get("url_to_image"),
                    source=art["source"],
                    url_hash=h,
                    has_video=art.get("has_video", False),
                    video_url=art.get("video_url"),
                    video_urls=art.get("video_urls") or [],
                ))
            except Exception as e:
                logger.debug("XAgent: skipping malformed entry %s: %s", art.get("url"), e)

        logger.info(
            "XAgent: %d new articles after dedup (from %d raw, %d handles)",
            len(articles), raw_count, len(x_handles),
        )
        return articles, raw_count

    async def _fetch_all(
        self,
        handles: list[str],
        limit: int,
        cutoff: datetime,
    ) -> list[dict]:
        if self._session is None:
            logger.error("XAgent: no aiohttp session — pass session= to XAgent()")
            return []

        raw_articles: list[dict] = []
        for i, handle in enumerate(handles):
            if i > 0:
                await asyncio.sleep(1.0)  # twitterapi.io has higher rate limits than Syndication
            try:
                articles = await self._fetch_handle(handle, limit, cutoff)
                raw_articles.extend(articles)
            except Exception as e:
                logger.warning("XAgent: error fetching @%s: %s", handle.lstrip("@"), e)
        return raw_articles

    async def _fetch_handle(
        self,
        handle: str,
        limit: int,
        cutoff: datetime,
    ) -> list[dict]:
        """Fetch tweets for one handle: try configured primary API first, then Syndication, then Nitter."""
        # Primary: socialdata.tools
        if self._x_scraper == "socialdata" and self._socialdata and self._socialdata.api_key:
            sd_limit = self._socialdata.tweets_per_account or limit
            articles = await _fetch_socialdata(
                self._session, self._socialdata.api_key, self._socialdata.base_url,
                handle, cutoff, sd_limit, self._keyword_pattern,
            )
            if articles:
                logger.debug("XAgent @%s: got %d tweets via socialdata.tools", handle.lstrip("@"), len(articles))
                return articles
            logger.debug("XAgent @%s: socialdata.tools returned nothing, trying Syndication", handle.lstrip("@"))

        # Primary: twitterapi.io
        elif self._x_scraper != "socialdata" and self._twitterapi and self._twitterapi.api_key:
            api_limit = self._twitterapi.tweets_per_account or limit
            articles = await _fetch_twitterapi(
                self._session, self._twitterapi.api_key, self._twitterapi.base_url,
                handle, cutoff, api_limit, self._keyword_pattern,
            )
            if articles is not None and len(articles) > 0:
                logger.debug("XAgent @%s: got %d tweets via twitterapi.io", handle.lstrip("@"), len(articles))
                return articles
            logger.debug("XAgent @%s: twitterapi.io returned nothing, trying Syndication", handle.lstrip("@"))

        # Fallback 1: Syndication API
        articles = await _fetch_syndication(self._session, handle, cutoff, limit, self._keyword_pattern)
        if articles:
            logger.debug("XAgent @%s: got %d tweets via Syndication", handle.lstrip("@"), len(articles))
            return articles

        # Fallback 2: Nitter RSS
        logger.debug("XAgent @%s: Syndication returned nothing, trying Nitter RSS", handle.lstrip("@"))
        return await _fetch_nitter_rss(self._session, handle, cutoff, limit, self._keyword_pattern)
