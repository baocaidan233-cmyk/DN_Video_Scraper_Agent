"""
Publish Agent — faithful port of v1.0_subflow_dailynews_to_gettr.json
and retrieve_preview_metadata.json sub-workflow.

Runs hourly at :20 (driven by publish_loop in main.py).

Flow per approved article:
  1. Pop article_id from publish:queue (Redis)
  2. Read full article data from review:pending:<id>
  3. SHA1 post hash dedup (content[:500] + media_url) → Redis SETNX
  4. WITH media URLs:
       - Upload each media URL via GcpClient
       - Aggregate upload results
       - POST to Gettr with media
  5. WITHOUT media:
       - Extract first URL from post_content
       - Fetch OG metadata (title, desc, image, source)
       - clean_control_chars on all text fields
       - POST to Gettr with preview
  6. On success: SET dedup key TTL 864000s, DEL review:pending:<id>
"""

from __future__ import annotations

import asyncio
import json
import logging
import base64
import html as html_module
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from services.notion_topical_dedup import NotionTopicalDedupChecker

import io

import aiohttp
from PIL import Image

from core.config import RedisConfig, MetadataApiConfig
from core.redis_client import RedisClient
from services.gcp_client import GcpClient, detect_media_type
from services.gettr_client import GettrClient
from services.metadata_client import MetadataClient
from utils.hashing import sha1_post_hash
from utils.logo_detect import is_source_logo
from utils.text_cleaner import clean_control_chars

logger = logging.getLogger(__name__)

_URL_IN_TEXT_RE = re.compile(r'https?://[^\s"\'<>]+', re.IGNORECASE)
_META_TAG_RE = re.compile(r'<meta\b[^>]+>', re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r'\b(property|name|content)\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_NEXT_DATA_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)
_IMG_TAG_RE = re.compile(r'<img\b[^>]+\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'<[^>]+>', re.DOTALL)

_MIN_IMAGE_AREA = 120_000   # px² — width × height must meet this
_MIN_IMAGE_SHORT_SIDE = 120  # pixels — min(width, height) must meet this
_DN_MIN_WORDS = 55  # DailyNews hard floor: never post fewer than 55 words (user rule)
_VIDEO_WINDOW_S = 86400  # AI video quota window — rolling 24h, per pipeline


async def _check_image_size(
    session: aiohttp.ClientSession,
    url: str,
    user_agent: str,
    cookie: Optional[str] = None,
    min_short_side: int = _MIN_IMAGE_SHORT_SIDE,
) -> bool:
    """Fetch the first 4 KB of an image and verify shortest side >= min_short_side.

    Pillow reads dimensions from the file header without needing pixel data.
    Returns True when dimensions cannot be determined or meet the minimum.
    Returns False when a dimension is below the minimum.
    """
    headers: dict[str, str] = {
        "user-agent": user_agent,
        "accept": "*/*",
        "range": "bytes=0-4095",
    }
    if cookie:
        headers["cookie"] = cookie
    if "twimg.com" in url:
        headers["referer"] = "https://x.com/"
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 206):
                return True  # cannot determine — allow
            data = await resp.content.read(4096)
    except Exception:
        return True  # network error — allow and let upload decide

    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
    except Exception:
        return True  # format not parseable — allow
    area = w * h
    short = min(w, h)
    ok = area >= _MIN_IMAGE_AREA and short >= min_short_side
    if not ok:
        logger.debug(
            "Image rejected (%dx%d, area=%d, short=%d) for %s",
            w, h, area, short, url,
        )
    return ok


def _extract_first_url(text: str) -> Optional[str]:
    """Extract the first URL from post content text."""
    match = _URL_IN_TEXT_RE.search(text)
    return match.group(0) if match else None


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace. Used to sanitise OG meta values
    that some sites (e.g. live blogs) mistakenly include as HTML markup."""
    return re.sub(r'\s+', ' ', _HTML_TAG_RE.sub(' ', text)).strip()


def _extract_og(html: str, prop_name: str, name_variants: tuple = ()) -> Optional[str]:
    """
    Extract a meta tag value by property or name, regardless of attribute order.
    Scans all <meta> tags, matches by key, HTML-unescapes and strips HTML from
    the content value (some sites embed HTML markup inside og:description).
    """
    for tag in _META_TAG_RE.finditer(html):
        attrs = dict(_ATTR_RE.findall(tag.group(0)))
        key = attrs.get("property") or attrs.get("name") or ""
        if key.lower() == prop_name.lower() or key.lower() in name_variants:
            val = _strip_html(html_module.unescape(attrs.get("content", "")))
            if val:
                return val
    return None


def _extract_next_data_image(html: str) -> Optional[str]:
    """Parse __NEXT_DATA__ JSON and find the first article image."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        j = json.loads(m.group(1))
        # cls.cn: props.initialState.detail.articleDetail.images[0]
        state = j.get("props", {}).get("initialState", {})
        detail = state.get("detail", {}).get("articleDetail", {})
        images = detail.get("images") or detail.get("imgs") or []
        if images and isinstance(images[0], str):
            return images[0]
        if images and isinstance(images[0], dict):
            return images[0].get("url") or images[0].get("img")
        # Generic fallback: pageProps.article.image
        page_props = j.get("props", {}).get("pageProps", {})
        return _json_get(page_props, "article", "image") or _json_get(page_props, "image")
    except Exception:
        return None


def _resolve_google_news_url_sync(google_url: str) -> str | None:
    """Decode real article URL from a news.google.com link via base64 decode."""
    m = re.search(r'/articles/([A-Za-z0-9_=-]+)', google_url)
    if not m:
        return None
    padded = m.group(1) + '=' * (-len(m.group(1)) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        m2 = re.search(rb'https?://[^\x00-\x1f\x7f\s]+', decoded)
        if m2:
            real = m2.group(0).decode('utf-8', errors='ignore').rstrip('.')
            if 'google.com' not in real:
                return real
    except Exception:
        pass
    return None


_SELF_HOSTED_RESOLVER_URL = "http://n8n-svr.gettr.fyi:7771/api/v1/url/final"
_SELF_HOSTED_API_KEY = "9277311724445fa26f0172a701150da4743bf4b8b0257cf33a39a4c6445204a4"


async def _resolve_google_news_url_async(
    session: aiohttp.ClientSession,
    google_url: str,
    user_agent: str,
    resolver_url: str = _SELF_HOSTED_RESOLVER_URL,
    resolver_api_key: str = _SELF_HOSTED_API_KEY,
) -> str:
    """Resolve a news.google.com URL.

    Priority order (mirrors n8n 'get final url' node):
      1. Self-hosted URL resolver service (fastest, handles all redirect types)
      2. Base64 decode of the opaque Google News article ID
      3. HTTP redirect follow
    """
    # 1. Self-hosted URL resolver (mirrors n8n 'get final url')
    if resolver_url:
        try:
            async with session.post(
                resolver_url,
                json={"url": google_url},
                headers={"X-API-Key": resolver_api_key or ""},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    final = data.get("final_url") or ""
                    if final and "google.com" not in final:
                        logger.debug(
                            "Google News self-hosted resolved: %s → %s",
                            google_url[:60], final[:60],
                        )
                        return final
        except Exception as e:
            logger.debug("Google News self-hosted resolver failed for %s: %s", google_url, e)

    # 2. Base64 decode
    real = _resolve_google_news_url_sync(google_url)
    if real:
        logger.debug("Google News decoded: %s → %s", google_url[:60], real[:60])
        return real

    # 3. HTTP redirect follow
    headers = {"user-agent": user_agent, "accept": "text/html,*/*"}
    try:
        async with session.get(
            google_url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=True,
        ) as resp:
            final_url = str(resp.url)
            if 'google.com' not in final_url and final_url != google_url:
                logger.debug("Google News redirect: %s → %s", google_url[:60], final_url[:60])
                return final_url
    except Exception as e:
        logger.debug("Google News redirect failed for %s: %s", google_url, e)

    return google_url


def _json_get(obj, *keys):
    """Safely traverse nested dict by keys, return None if missing."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj if isinstance(obj, str) else None


def _cls_web_url(api_url: str) -> Optional[str]:
    """Transform api3.cls.cn/share/article/{id}?... → https://www.cls.cn/detail/{id}"""
    m = re.search(r'/(?:share/)?article/(\d+)', api_url)
    return f"https://www.cls.cn/detail/{m.group(1)}" if m else None


async def _fetch_og_metadata(
    session: aiohttp.ClientSession,
    url: str,
    user_agent: str,
) -> dict:
    """
    Fetch a URL and extract OG metadata.
    Handles: standard HTML OG tags (any attribute order), JSON APIs, __NEXT_DATA__.
    For api3.cls.cn, rewrites to the www.cls.cn web page automatically.
    """
    headers = {
        "user-agent": user_agent,
        "accept": "text/html,application/xhtml+xml,*/*",
        "accept-language": "en-US,zh-CN;q=0.9,zh;q=0.8",
        "referer": "https://www.cls.cn/",
    }
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.debug("OG fetch got HTTP %d for %s", resp.status, url)
                return {}
            content_type = resp.content_type or ""
            body = await resp.text(errors="replace")
    except Exception as e:
        logger.debug("OG fetch failed for %s: %s", url, e)
        return {}

    # JSON API — extract image from common fields
    if "json" in content_type or body.lstrip().startswith("{"):
        try:
            j = json.loads(body)
            img = (
                _json_get(j, "data", "article", "img")
                or _json_get(j, "data", "article", "shareImg")
                or _json_get(j, "data", "article", "image")
                or _json_get(j, "data", "img")
                or _json_get(j, "data", "image")
                or _json_get(j, "result", "img")
                or _json_get(j, "result", "image")
            )
            title = _json_get(j, "data", "article", "title") or _json_get(j, "data", "title")
            desc = (
                _json_get(j, "data", "article", "brief")
                or _json_get(j, "data", "article", "content")
                or _json_get(j, "data", "brief")
            )
            return {"prev_img": img, "prev_ttl": title, "prev_desc": desc, "prev_src_link": url}
        except Exception:
            pass

    # Standard HTML: OG tags (attribute-order independent)
    prev_img = _extract_og(body, "og:image", ("twitter:image", "twitter:image:src"))
    prev_ttl = _extract_og(body, "og:title", ("twitter:title",))
    prev_desc = _extract_og(body, "og:description", ("twitter:description",))
    prev_src = _extract_og(body, "og:url") or url

    # __NEXT_DATA__ fallback (Next.js SSR apps)
    if not prev_img:
        prev_img = _extract_next_data_image(body)

    # <img src> fallback — picks up brand/logo images when no OG image exists
    # Skip tiny icons and data URIs; prefer images from the same domain
    if not prev_img:
        for m in _IMG_TAG_RE.finditer(body):
            src = m.group(1)
            if src.startswith("data:") or not src.startswith("http"):
                continue
            # Skip obvious UI chrome (icons, avatars, spinners)
            low = src.lower()
            if any(x in low for x in ("icon", "avatar", "logo-small", "spinner", "loading", "favicon")):
                continue
            prev_img = src
            break

    return {"prev_img": prev_img, "prev_ttl": prev_ttl, "prev_desc": prev_desc, "prev_src_link": prev_src}


async def _fetch_og_via_caps_gettr(
    session: aiohttp.ClientSession,
    url: str,
) -> dict:
    """Fetch OG metadata via Gettr's caps scraping proxy (mirrors n8n 'Get Metadata - HTTP Request').

    Tries https://caps.gettr.com/<url> — Gettr's own proxy bypasses bot-detection on
    many news sites. Returns same dict format as _fetch_og_metadata, or {} if no
    og:image was found (caller should fall back to direct fetch in that case).
    """
    try:
        async with session.get(
            f"https://caps.gettr.com/{url}",
            headers={"origin": "https://gettr.com", "referer": "https://gettr.com/"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                logger.debug("caps.gettr.com returned %d for %s", resp.status, url)
                return {}
            body = await resp.text(errors="replace")
    except Exception as e:
        logger.debug("caps.gettr.com failed for %s: %s", url, e)
        return {}

    prev_img = _extract_og(body, "og:image", ("twitter:image", "twitter:image:src"))
    if not prev_img:
        prev_img = _extract_next_data_image(body)
    if not prev_img:
        return {}  # no image — caller falls back to direct fetch

    prev_ttl = _extract_og(body, "og:title", ("twitter:title",))
    prev_desc = _extract_og(body, "og:description", ("twitter:description",))
    prev_src = _extract_og(body, "og:url") or url
    return {"prev_img": prev_img, "prev_ttl": prev_ttl, "prev_desc": prev_desc, "prev_src_link": prev_src}


class PublishAgent:
    def __init__(
        self,
        redis: RedisClient,
        gcp: GcpClient,
        gettr: GettrClient,
        session: aiohttp.ClientSession,
        user_agent: str,
        state=None,  # DashboardState (optional)
        redis_key_overrides: dict | None = None,
        pipeline_type: str = "rss",  # "rss" | "epicfury" — behaviour mode (drop vs OG fallback, callbacks)
        metadata_api_config: Optional[MetadataApiConfig] = None,
        publish_run_type: Optional[str] = None,  # history DB run_type; None → derived from pipeline_type
        gettr_test: Optional[GettrClient] = None,  # DailyNews A/B: editor-variant account
        gcp_test: Optional[GcpClient] = None,      # CDN uploader bound to gettr_test
        video_client=None,          # VideoClient — AI video fallback for image-less posts
        video_brand_slug: str = "dn",  # which video/brand/<slug> this pipeline publishes under
        topical_dedup: Optional["NotionTopicalDedupChecker"] = None,  # DailyNews only
    ) -> None:
        self._redis = redis
        self._gcp = gcp
        self._gettr = gettr
        self._gettr_test = gettr_test
        self._gcp_test = gcp_test
        self._session = session
        self._user_agent = user_agent
        self._state = state
        self._video = video_client
        self._video_brand_slug = video_brand_slug
        self._topical_dedup = topical_dedup
        self._metadata_client = MetadataClient(
            metadata_api_config or MetadataApiConfig(), session
        )
        overrides = redis_key_overrides or {}
        self._publish_queue_key: str = overrides.get(
            "publish_queue_key", redis._config.publish_queue_key
        )
        self._review_pending_prefix: str = overrides.get(
            "review_pending_prefix", redis._config.review_pending_prefix
        )
        self._post_hash_key_prefix: str = overrides.get(
            "post_hash_key_prefix", redis._config.post_hash_key_prefix
        )
        self._post_hash_ttl_s: int = overrides.get(
            "post_hash_ttl_s", redis._config.post_hash_ttl_s
        )
        self._pipeline_type: str = pipeline_type
        self._publish_run_type: str = publish_run_type or (
            "ef_publish" if pipeline_type == "epicfury" else "publish"
        )
        self._on_posted = None      # async callback(notion_page_id: str) — set by main after both agents init
        self._on_dropped = None     # async callback(notion_page_id: str) — called when article is silently dropped
        self._notion_fallback = None  # async callback() → list[str] — set by main for DailyNews stock fallback
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def _emit(self, event: dict) -> None:
        if self._state:
            await self._state.emit(event)

    async def _notify_dropped(self, notion_page_id: Optional[str]) -> None:
        if notion_page_id and self._on_dropped and self._pipeline_type != "epicfury":
            try:
                await self._on_dropped(notion_page_id)
            except Exception as e:
                logger.warning("Notion drop update failed for page %s: %s", notion_page_id, e)

    # ------------------------------------------------------------------ #
    # AI video fallback for posts with no usable image                   #
    # ------------------------------------------------------------------ #
    @property
    def _video_quota_key(self) -> str:
        return f"{self._post_hash_key_prefix}videogen:24h"

    async def video_quota_used(self) -> int:
        """Videos generated in the last rolling 24h for THIS pipeline."""
        try:
            cutoff = time.time() - _VIDEO_WINDOW_S
            await self._redis.zremrangebyscore(self._video_quota_key, 0, cutoff)
            return await self._redis.zcard(self._video_quota_key)
        except Exception as e:
            logger.warning("Video quota read failed: %s", e)
            return 0

    async def _video_quota_record(self, article_id: str) -> None:
        """Charge one video against the window. Called after a successful render:
        the CPU is what we ration, so a later Gettr failure does not refund it."""
        try:
            now = time.time()
            await self._redis.zadd(self._video_quota_key, {article_id: now})
            await self._redis.expire(self._video_quota_key, _VIDEO_WINDOW_S * 2)
        except Exception as e:
            logger.warning("Video quota record failed for %s: %s", article_id, e)

    async def _try_video(
        self,
        article_id: str,
        data: dict,
        post_content: str,
        reason: str,
    ) -> Optional[dict]:
        """Generate + upload a video for an image-less post.

        Returns Gettr media metadata ready for post_with_media, or None — in which
        case the caller MUST fall through to its existing behavior. This never
        raises: a broken generator can only cost the article the video, never the
        pipeline.
        """
        if self._video is None:
            return None
        state = self._state
        if not getattr(state, "video_gen_enabled", False):
            return None
        max_24h = int(getattr(state, "video_gen_max_24h", 0) or 0)
        if max_24h <= 0:
            return None

        used = await self.video_quota_used()
        if used >= max_24h:
            logger.info(
                "Video quota exhausted (%d/%d in last 24h) — not generating for %s (%s)",
                used, max_24h, article_id, reason,
            )
            return None

        logger.info("No usable image for %s (%s) — generating video [%d/%d used]",
                    article_id, reason, used, max_24h)
        try:
            result = await self._video.generate(
                article_id=article_id,
                title=data.get("title") or "",
                post_content=post_content,
                brand_slug=self._video_brand_slug,
            )
        except Exception as e:                      # defence in depth
            logger.error("Video generation raised for %s: %s", article_id, e)
            return None
        if result is None:
            return None

        await self._video_quota_record(article_id)

        try:
            return await self._gcp.upload_bytes(
                result.data,
                content_type="video/mp4",
                filename=f"{article_id[:16]}.mp4",
                media_type="video",
                extra_meta=result.upload_meta,
            )
        except Exception as e:
            logger.error("Video upload failed for %s: %s", article_id, e)
            return None

    async def _post_editor_twin(
        self,
        article_id: str,
        editor_post: Optional[str],
        media_urls: list[str],
        cookie: Optional[str] = None,
    ) -> None:
        """
        DailyNews A/B: post the editor-revised variant of an article that was just
        published to the live account, to the comparison (test) Gettr account.

        Best-effort by design — this runs *after* the live post has already succeeded,
        so any failure here is logged and swallowed. It never changes the main outcome,
        the Notion state, or the dedup keys.

        The image is re-uploaded rather than reused: GcpClient obtains its upload channel
        with the account's own Gettr auth, so the live account's CDN metadata is not
        valid for the test account.

        Deliberately exempt from the _DN_MIN_WORDS floor — that gate protects the live
        channel. Word count is logged so short variants are visible in the logs.
        """
        if self._pipeline_type == "epicfury" or not self._gettr_test or not self._gcp_test:
            return
        if not editor_post or not editor_post.strip():
            return

        try:
            uploaded = []
            for url in media_urls:
                if not url:
                    continue
                if not url.startswith("http"):
                    # Already a Gettr CDN path — reuse as-is (same rule as _publish_with_media)
                    uploaded.append({"ori": url, "screen": url, "media_type": "image"})
                    continue
                meta = await self._gcp_test.upload_media(url, download_cookie=cookie)
                if meta:
                    uploaded.append(meta)
            if not uploaded:
                logger.warning(
                    "Editor twin: no media uploaded for %s — skipping test post", article_id
                )
                return
            result = await self._gettr_test.post_with_media(
                clean_control_chars(editor_post), uploaded
            )
            post_id = result.get("result", {}).get("data", {}).get("_id", "")
            logger.info(
                "Posted editor variant to test Gettr: article=%s post_id=%s words=%d",
                article_id, post_id, len(editor_post.split()),
            )
        except Exception as e:
            logger.warning(
                "Editor twin post failed for %s: %s — main post unaffected", article_id, e
            )

    async def _sort_by_score(self, article_ids: list[str]) -> list[str]:
        """Return article_ids sorted by llm_score desc (highest first)."""
        scored: list[tuple[float, str]] = []
        for aid in article_ids:
            redis_key = f"{self._review_pending_prefix}{aid}"
            data = await self._redis.hgetall(redis_key)
            score = 0.0
            if data:
                try:
                    score = float(data.get("llm_score") or 0)
                except (ValueError, TypeError):
                    score = 0.0
            scored.append((score, aid))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [aid for _, aid in scored]

    async def run(self, run_id: Optional[str] = None) -> None:
        """Process all articles in publish:queue.

        DailyNews: sorted by score, stop after first success, no requeue.
                   If queue empty or nothing posted, fall back to Notion stocked articles.
        EpicFury:  FIFO order, try all, requeue failures up to 3 times.
        """
        if self._lock.locked():
            logger.warning("PublishAgent: run() called while already running — skipping")
            return

        async with self._lock:
            await self._run_inner(run_id)

    async def _run_inner(self, run_id: Optional[str] = None) -> None:
        from dashboard import db

        if run_id is None:
            run_id = f"publish-{uuid.uuid4().hex[:12]}"

        started_at = datetime.now(timezone.utc).isoformat()
        t_total = time.monotonic()

        queue_key = self._publish_queue_key
        article_ids = await self._redis.lrange(queue_key, 0, -1)
        if article_ids:
            await self._redis.ltrim(queue_key, len(article_ids), -1)
        else:
            article_ids = []

        # Fast path: nothing in queue
        if not article_ids:
            if self._pipeline_type == "epicfury" or not self._notion_fallback:
                return
            # DailyNews: try Notion stock fallback before bailing
            try:
                article_ids = await self._notion_fallback()
            except Exception as e:
                logger.warning("PublishAgent: notion_fallback error: %s", e)
                return
            if not article_ids:
                return
            fallback_already_used = True
        else:
            fallback_already_used = False

        await self._emit({"type": "run_start", "run_id": run_id, "run_type": self._publish_run_type})
        try:
            await db.save_run_start(run_id, self._publish_run_type, started_at)
        except Exception:
            pass

        logger.info("PublishAgent: processing %d articles", len(article_ids))
        posted = 0
        skipped = 0
        failed = 0

        if self._pipeline_type != "epicfury":
            # DailyNews: best-score first, stop at first success, no requeue
            sorted_ids = await self._sort_by_score(article_ids)
            for article_id in sorted_ids:
                try:
                    result = await self._process_one(article_id, run_id=run_id)
                    if result == "posted":
                        posted += 1
                        break
                    elif result == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error("Failed to publish article %s: %s", article_id, e)
                    failed += 1

            # If nothing posted from queue, try Notion stock fallback (once)
            if posted == 0 and not fallback_already_used and self._notion_fallback:
                try:
                    fallback_ids = await self._notion_fallback()
                    if fallback_ids:
                        logger.info(
                            "PublishAgent: queue exhausted — trying %d Notion fallback article(s)",
                            len(fallback_ids),
                        )
                        sorted_fallback = await self._sort_by_score(fallback_ids)
                        for article_id in sorted_fallback:
                            try:
                                result = await self._process_one(article_id, run_id=run_id)
                                if result == "posted":
                                    posted += 1
                                    break
                                elif result == "skipped":
                                    skipped += 1
                                else:
                                    failed += 1
                            except Exception as e:
                                logger.error("Failed to publish fallback article %s: %s", article_id, e)
                                failed += 1
                except Exception as e:
                    logger.warning("PublishAgent: notion_fallback error: %s", e)
        else:
            # EpicFury: FIFO order (LPUSH → reversed), try all, requeue failures
            for article_id in reversed(article_ids):
                try:
                    result = await self._process_one(article_id, run_id=run_id)
                    if result == "posted":
                        posted += 1
                    elif result == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                        try:
                            await self._maybe_requeue(article_id)
                        except Exception as re:
                            logger.error("Requeue failed for %s: %s", article_id, re)
                except Exception as e:
                    logger.error("Failed to publish article %s: %s", article_id, e)
                    failed += 1
                    try:
                        await self._maybe_requeue(article_id)
                    except Exception as re:
                        logger.error("Requeue failed for %s: %s", article_id, re)

        logger.info("PublishAgent: done — posted=%d skipped(dedup)=%d failed=%d", posted, skipped, failed)

        total_ms = int((time.monotonic() - t_total) * 1000)
        await self._emit({"type": "run_done", "run_id": run_id, "status": "success",
                          "total_duration_ms": total_ms, "articles_posted": posted})
        try:
            await db.update_run(run_id, "success", datetime.now(timezone.utc).isoformat())
        except Exception:
            pass

    async def _process_one(self, article_id: str, run_id: str = "") -> str:
        redis_key = f"{self._review_pending_prefix}{article_id}"
        data = await self._redis.hgetall(redis_key)
        if not data:
            logger.warning("Article %s not found in Redis — key expired or already cleaned up", article_id)
            return "failed"

        post_content = data.get("post_content") or data.get("title") or ""
        article_url_raw = data.get("url") or ""

        # For X/Twitter articles: strip x.com and t.co URLs from post text so
        # Gettr doesn't render a tweet embed / preview box — the video or content
        # itself is the post; the link back to X is unwanted.
        if "x.com" in article_url_raw or "twitter.com" in article_url_raw:
            post_content = re.sub(r'https?://(?:t\.co|x\.com|twitter\.com)/\S+', '', post_content).strip()

        media_json = data.get("media", "[]")
        try:
            media_urls: list[str] = json.loads(media_json) if isinstance(media_json, str) else []
        except Exception:
            media_urls = []

        cookie = data.get("cookie")
        notion_page_id: Optional[str] = data.get("notion_page_id") or None
        editor_post: Optional[str] = data.get("editor_post") or None

        # DailyNews hard floor: never post fewer than _DN_MIN_WORDS words.
        # Last line of defense — catches short LLM output, a None post falling back
        # to the raw title (line above), or any other too-thin content, regardless of
        # what happened upstream. Drop and let run() move on to the next best article.
        # EpicFury is intentionally exempt (it may post short items / OG previews).
        if self._pipeline_type != "epicfury":
            word_count = len(post_content.split())
            if word_count < _DN_MIN_WORDS:
                logger.info(
                    "DailyNews: dropping article %s — post is %d words (< %d-word minimum)",
                    article_id, word_count, _DN_MIN_WORDS,
                )
                await self._cleanup(redis_key)
                await self._notify_dropped(notion_page_id)
                return "skipped"

        # Article-ID dedup: stable check that survives LLM post content regeneration.
        # Set after a successful post; prevents re-posting the same URL even if the
        # article re-enters the pipeline with freshly generated (different) post text.
        article_id_key = f"{self._post_hash_key_prefix}article:{article_id}"
        await self._emit({"type": "step_start", "run_id": run_id, "step": "sha1_dedup"})
        if await self._redis.exists(article_id_key):
            logger.info("Article %s already posted (article-ID dedup) — skipping", article_id)
            await self._emit({"type": "step_done", "run_id": run_id, "step": "sha1_dedup",
                              "articles_in": 1, "articles_out": 0, "articles_dropped": 1, "duration_ms": 0})
            await self._cleanup(redis_key)
            await self._notify_dropped(notion_page_id)
            return "skipped"

        # SHA1 post hash (content[:500] + first media URL) — kept as a write-only signal
        # for the legacy n8n "waiting for post" publish workflow, which checks this same
        # Redis key/namespace before it publishes (see utils/hashing.py docstring). No
        # longer used to gate this bot's own publish decision: exact-500-char matching
        # missed same-story-different-wording duplicates, which the topical dedup check
        # below (embedding similarity against the human-editor Notion board) catches.
        first_media = media_urls[0] if media_urls else ""
        post_hash = sha1_post_hash(post_content, first_media)
        hash_key = f"{self._post_hash_key_prefix}{post_hash}"
        await self._emit({"type": "step_done", "run_id": run_id, "step": "sha1_dedup",
                          "articles_in": 1, "articles_out": 1, "articles_dropped": 0, "duration_ms": 0})

        # Topical dedup: skip if a human editor already published the same story
        # recently (send_status=True in daily_news within recent_lookback_hours).
        # enforce_recent_skip defaults off — see NotionTopicalDedupChecker docstring.
        if self._topical_dedup is not None and self._pipeline_type != "epicfury":
            await self._emit({"type": "step_start", "run_id": run_id, "step": "topical_dedup"})
            should_skip = await self._topical_dedup.before_publish(post_content)
            await self._emit({"type": "step_done", "run_id": run_id, "step": "topical_dedup",
                              "articles_in": 1, "articles_out": 0 if should_skip else 1,
                              "articles_dropped": 1 if should_skip else 0, "duration_ms": 0})
            if should_skip:
                logger.info("Article %s duplicate (topical dedup) — skipping", article_id)
                await self._cleanup(redis_key)
                await self._notify_dropped(notion_page_id)
                return "skipped"

        t0 = time.monotonic()
        if self._pipeline_type != "epicfury":
            # DailyNews: always embed media, never show source preview box or URL.
            if media_urls:
                success = await self._publish_with_media(article_id, post_content, media_urls, cookie, hash_key, redis_key, notion_page_id, editor_post)
            else:
                success = await self._publish_dailynews(article_id, data, post_content, hash_key, redis_key, notion_page_id, editor_post)
            step = "media_upload"
        else:
            # EpicFury: post with media if available, otherwise use OG preview.
            if media_urls:
                success = await self._publish_with_media(article_id, post_content, media_urls, cookie, hash_key, redis_key)
            else:
                success = await self._publish_without_media(article_id, data, post_content, hash_key, redis_key)
            step = "media_upload" if media_urls else "gettr_post"

        # None = dropped (no image); True = posted; False = Gettr error
        outcome = "skipped" if success is None else ("posted" if success else "failed")
        await self._emit({"type": "step_done", "run_id": run_id, "step": step,
                          "articles_in": 1, "articles_out": 1 if success is True else 0,
                          "duration_ms": int((time.monotonic() - t0) * 1000)})
        return outcome

    async def _publish_dailynews(
        self,
        article_id: str,
        data: dict,
        post_content: str,
        hash_key: str,
        redis_key: str,
        notion_page_id: Optional[str] = None,
        editor_post: Optional[str] = None,
    ) -> Optional[bool]:
        """DailyNews publish path: always embed image, never show source URL or preview box.

        Mirrors n8n retrieve_preview_metadata + 'If no preview link? → No media No post' gate:
          1. Use url_to_image already stored from ingestion (fastest, no extra API call).
          2. If absent, resolve Google News redirect then call MetadataClient (urlmeta.org →
             self-hosted fallback for articles; YouTube Data API for YouTube URLs).
          3. Upload image to GCP CDN → post_with_media (no prevsrc, no source link).
          4. No usable image → try the AI video fallback; if that is off, out of
             quota or fails, drop the article (return None). No text-only fallback.
        Returns True on success, False on Gettr error, None if dropped (no image).

        The three ways an image can be unusable — absent, a source logo, too small
        — all converge on the single `if not img_url` exit below, so the video
        fallback and the drop each live in exactly one place.
        """
        article_url = data.get("url") or ""
        img_url: Optional[str] = data.get("url_to_image") or None
        # Reject relative Gettr CDN paths (e.g. "group6/getter/...") — not downloadable URLs
        if img_url and not img_url.startswith("http"):
            img_url = None

        if not img_url:
            # Resolve Google News redirect before metadata fetch
            if article_url and "news.google.com" in article_url:
                resolved = await _resolve_google_news_url_async(
                    self._session, article_url, self._user_agent
                )
                article_url = "" if "news.google.com" in resolved else resolved
            if article_url:
                img_url = await self._metadata_client.fetch_image_url(article_url)

        reason = "no image found"
        # Source-logo gate: a masthead-only image is worth no more than no image
        # at all — IF we can put a video there instead. Runs before the size gate
        # because logos are often large enough to pass it.
        #
        # `logo_fallback` preserves the old behavior exactly: before this feature,
        # a logo image was posted as-is. So if the video declines for any reason
        # (switch off, quota spent, render failed) we post the logo rather than
        # dropping an article that previously would have gone out.
        logo_fallback: Optional[str] = None
        if img_url and await is_source_logo(
            self._redis, self._post_hash_key_prefix, img_url, article_url
        ):
            logo_fallback, img_url, reason = img_url, None, "image is a source logo"

        # Size gate: an image that fails area or short-side requirements
        if img_url and not await _check_image_size(
            self._session, img_url, self._user_agent
        ):
            logger.info(
                "DailyNews: image too small for article %s (area<%d or short<%dpx)",
                article_id, _MIN_IMAGE_AREA, _MIN_IMAGE_SHORT_SIDE,
            )
            img_url, reason = None, "image too small"

        clean_content = clean_control_chars(post_content)

        if not img_url:
            video_meta = await self._try_video(article_id, data, post_content, reason)
            if video_meta:
                try:
                    result = await self._gettr.post_with_media(clean_content, [video_meta])
                    post_id = result.get("result", {}).get("data", {}).get("_id", "")
                    logger.info("Posted to Gettr (DailyNews, AI video): article=%s post_id=%s",
                                article_id, post_id)
                    # No editor twin: the A/B measures editorial voice, and the twin
                    # can only re-upload from source URLs, which a generated video
                    # does not have.
                    await self._mark_posted(hash_key, redis_key, notion_page_id,
                                            post_content=clean_content, gettr_post_id=post_id)
                    return True
                except Exception as e:
                    logger.error("Gettr post failed (DailyNews, AI video) for %s: %s",
                                 article_id, e)
                    return False
            if logo_fallback:
                logger.info("No video for %s — posting the source-logo image as before",
                            article_id)
                img_url = logo_fallback
            else:
                # Hard gate: no image = no post (mirrors n8n 'No media No post' noOp)
                logger.info("DailyNews: dropping article %s — %s (no image = no post)",
                            article_id, reason)
                await self._cleanup(redis_key)
                await self._notify_dropped(notion_page_id)
                return None

        try:
            meta = await self._gcp.upload_media(img_url)
            if not meta:
                raise ValueError("GCP upload returned empty metadata")
            result = await self._gettr.post_with_media(clean_content, [meta])
            post_id = result.get("result", {}).get("data", {}).get("_id", "")
            logger.info("Posted to Gettr (DailyNews): article=%s post_id=%s", article_id, post_id)
            await self._post_editor_twin(article_id, editor_post, [img_url])
            await self._mark_posted(hash_key, redis_key, notion_page_id,
                                    post_content=clean_content, gettr_post_id=post_id)
            return True
        except Exception as e:
            logger.error("Gettr post failed (DailyNews) for %s: %s", article_id, e)
            return False

    async def _publish_with_media(
        self,
        article_id: str,
        post_content: str,
        media_urls: list[str],
        cookie: Optional[str],
        hash_key: str,
        redis_key: str,
        notion_page_id: Optional[str] = None,
        editor_post: Optional[str] = None,
    ) -> bool:
        """Upload all media files then post to Gettr."""
        uploaded = []
        # Source-logo images are set aside rather than discarded: if no video is
        # generated they are uploaded after all, which is exactly what happened
        # before the video fallback existed.
        logo_skipped: list[str] = []
        for url in media_urls:
            # Relative Gettr CDN paths (e.g. "group7/getter/...") are already uploaded —
            # use them directly as pre-built metadata without re-downloading or re-uploading.
            if not url.startswith("http"):
                uploaded.append({"ori": url, "screen": url, "media_type": "image"})
                logger.debug("Using pre-uploaded CDN asset: %s", url)
                continue
            ct, mtype, _ = detect_media_type(url)
            if mtype == "image" and await is_source_logo(
                self._redis, self._post_hash_key_prefix, url
            ):
                logger.info("Media image is a source logo — set aside %s", url)
                logo_skipped.append(url)
                continue
            if mtype == "image" and not await _check_image_size(
                self._session, url, self._user_agent, cookie=cookie
            ):
                logger.info("Media image too small — skipping %s (area<%d or short<%dpx)", url, _MIN_IMAGE_AREA, _MIN_IMAGE_SHORT_SIDE)
                continue
            try:
                meta = await self._gcp.upload_media(url, download_cookie=cookie)
                uploaded.append(meta)
                logger.info("Media uploaded to CDN: %s", url)
            except Exception as e:
                logger.warning("Media upload failed for %s: %s", url, e)

        if not uploaded:
            data_for_fallback = await self._redis.hgetall(redis_key)
            video_meta = await self._try_video(
                article_id, data_for_fallback, post_content,
                f"all {len(media_urls)} media rejected/failed",
            )
            if video_meta:
                uploaded = [video_meta]
                # No editor twin for a generated video — see _publish_dailynews.
                # _post_editor_twin() returns immediately on a falsy editor_post.
                editor_post = None
            elif logo_skipped:
                # No video: fall back to the logo images, as before this feature.
                logger.info("No video for %s — uploading the %d set-aside logo image(s)",
                            article_id, len(logo_skipped))
                for url in logo_skipped:
                    try:
                        uploaded.append(await self._gcp.upload_media(url, download_cookie=cookie))
                    except Exception as e:
                        logger.warning("Media upload failed for %s: %s", url, e)

        if not uploaded:
            if self._pipeline_type != "epicfury":
                # DailyNews: no image = no post (same rule as _publish_dailynews)
                logger.info(
                    "DailyNews: all %d media rejected/failed for article %s — dropping (no image = no post)",
                    len(media_urls), article_id,
                )
                await self._cleanup(redis_key)
                await self._notify_dropped(notion_page_id)
                return None
            # EpicFury: fall back to OG preview post
            logger.warning(
                "All %d media upload(s) failed for article %s — falling back to no-media post",
                len(media_urls), article_id,
            )
            return await self._publish_without_media(article_id, data_for_fallback, post_content, hash_key, redis_key, notion_page_id)

        clean_content = clean_control_chars(post_content)

        try:
            result = await self._gettr.post_with_media(clean_content, uploaded)
            post_id = result.get("result", {}).get("data", {}).get("_id", "")
            logger.info("Posted to Gettr (with media): article=%s post_id=%s media_count=%d", article_id, post_id, len(uploaded))
            await self._post_editor_twin(article_id, editor_post, media_urls, cookie)
            await self._mark_posted(hash_key, redis_key, notion_page_id,
                                    post_content=clean_content, gettr_post_id=post_id)
            return True
        except Exception as e:
            logger.error("Gettr post failed (with media) for %s: %s", article_id, e)
            return False

    async def _publish_without_media(
        self,
        article_id: str,
        data: dict,
        post_content: str,
        hash_key: str,
        redis_key: str,
        notion_page_id: Optional[str] = None,
    ) -> bool:
        """Fetch OG metadata from article URL then post to Gettr.

        If the AI video switch is on for this pipeline and quota remains, a video
        replaces the OG link-preview post. EpicFury and extra channels default the
        switch OFF, so in practice this is a DailyNews-only path today.
        """
        video_meta = await self._try_video(
            article_id, data, post_content, "no media, OG preview only"
        )
        if video_meta:
            try:
                result = await self._gettr.post_with_media(
                    clean_control_chars(post_content), [video_meta])
                post_id = result.get("result", {}).get("data", {}).get("_id", "")
                logger.info("Posted to Gettr (AI video, no source media): article=%s post_id=%s",
                            article_id, post_id)
                await self._mark_posted(hash_key, redis_key, notion_page_id,
                                        post_content=post_content, gettr_post_id=post_id)
                return True
            except Exception as e:
                logger.error("Gettr post failed (AI video) for %s: %s — falling back to OG preview",
                             article_id, e)

        article_url = data.get("url") or _extract_first_url(post_content) or ""

        # X/Twitter articles: don't use x.com as preview link — no OG metadata
        # available without auth, and the tweet embed is unwanted on Gettr.
        if "x.com" in article_url or "twitter.com" in article_url:
            article_url = ""

        # Resolve Google News URLs to the real article URL before OG fetch
        if article_url and "news.google.com" in article_url:
            resolved = await _resolve_google_news_url_async(
                self._session, article_url, self._user_agent
            )
            # If still a Google News URL (opaque ID — unresolvable), clear it so
            # Gettr doesn't fetch it and show Google News logo/slogan as preview
            article_url = "" if "news.google.com" in resolved else resolved

        og = {}
        if article_url:
            og = await _fetch_og_via_caps_gettr(self._session, article_url)
            if not og:
                og = await _fetch_og_metadata(self._session, article_url, self._user_agent)

        clean_content = clean_control_chars(post_content)
        clean_ttl = clean_control_chars(og.get("prev_ttl") or data.get("title"))
        raw_desc = og.get("prev_desc") or data.get("description") or ""
        clean_desc = clean_control_chars(_strip_html(raw_desc)) if raw_desc else None
        prev_img = data.get("url_to_image") or og.get("prev_img")
        # Reject relative Gettr CDN paths stored by old pipeline runs
        if prev_img and not prev_img.startswith("http"):
            prev_img = None

        try:
            result = await self._gettr.post_without_media(
                post_content=clean_content,
                prev_desc=clean_desc,
                prev_img=prev_img,
                prev_src_link=og.get("prev_src_link") or article_url or "",
                prev_ttl=clean_ttl,
            )
            post_id = result.get("result", {}).get("data", {}).get("_id", "")
            logger.info("Posted to Gettr (no media): article=%s post_id=%s", article_id, post_id)
            await self._mark_posted(hash_key, redis_key, notion_page_id,
                                    post_content=clean_content, gettr_post_id=post_id)
            return True
        except Exception as e:
            logger.error("Gettr post failed (no media) for %s: %s", article_id, e)
            return False

    async def publish_one(self, article_id: str) -> bool:
        """Immediately publish a single article (called from Telegram 🚀 Publish Now button).

        Bypasses the publish queue — reads directly from review:pending, runs the full
        upload + post flow, and marks the article as posted.  Returns True on success.
        """
        run_id = f"publish-now-{article_id[:12]}"
        result = await self._process_one(article_id, run_id=run_id)
        return result == "posted"

    async def _maybe_requeue(self, article_id: str) -> None:
        """Re-push a failed article to the publish queue for retry (max 3 total attempts)."""
        redis_key = f"{self._review_pending_prefix}{article_id}"
        data = await self._redis.hgetall(redis_key)
        if not data:
            logger.warning("Article %s: pending key gone — cannot requeue", article_id)
            return
        attempts = int(data.get("publish_attempts", "0")) + 1
        if attempts >= 3:
            logger.warning("Article %s: failed %d time(s) — abandoning", article_id, attempts)
            await self._redis.delete(redis_key)
            return
        await self._redis.hset(redis_key, {"publish_attempts": str(attempts)})
        await self._redis.lpush(self._publish_queue_key, article_id)
        logger.info("Article %s: requeued for retry (attempt %d/3)", article_id, attempts)

    async def _mark_posted(
        self,
        hash_key: str,
        redis_key: str,
        notion_page_id: Optional[str] = None,
        post_content: str = "",
        gettr_post_id: str = "",
    ) -> None:
        """Set dedup key with 10-day TTL, clean up pending entry, and notify Notion on success."""
        now = datetime.now(timezone.utc).isoformat()
        await self._redis.set(hash_key, now, ex=self._post_hash_ttl_s)
        # Also set a stable article-ID dedup key (same TTL) so re-ingested articles with
        # regenerated post content are still caught at publish time.
        article_id = redis_key.split(":")[-1]
        article_id_key = f"{self._post_hash_key_prefix}article:{article_id}"
        await self._redis.set(article_id_key, now, ex=self._post_hash_ttl_s)
        if self._topical_dedup is not None and self._pipeline_type != "epicfury" and post_content and gettr_post_id:
            try:
                await self._topical_dedup.after_publish(post_content, gettr_post_id)
            except Exception as e:
                logger.warning("Topical dedup after_publish failed: %s", e)
        await self._cleanup(redis_key)
        if notion_page_id and self._on_posted and self._pipeline_type != "epicfury":
            try:
                await self._on_posted(notion_page_id)
            except Exception as e:
                logger.warning("Notion post-publish update failed for page %s: %s", notion_page_id, e)

    async def _cleanup(self, redis_key: str) -> None:
        await self._redis.delete(redis_key)
