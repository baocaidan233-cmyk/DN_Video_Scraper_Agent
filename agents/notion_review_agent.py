"""
Notion Review Agent — replaces Telegram ReviewAgent.

Articles pending review are created as pages in a Notion database.
A polling loop detects when the user changes the Decision property and acts:
  Approved      → push article_id to publish queue
  Publish Now   → call publish_agent.publish_one() immediately (bypass queue)
  Rejected      → delete from Redis

Flow:
  1. Pipeline calls enqueue_article(article) → stored in Redis + added to asyncio.Queue
  2. _card_sender_loop() drains Queue → creates Notion page with all article fields
     (image block in the page body so it's visible when opened)
  3. User opens Notion, reads Post Content, optionally edits Edit Override field,
     then sets Decision to Approved / Rejected / Publish Now
  4. _poll_loop() detects the change → acts on Redis/publish_agent
  5. Page Decision is updated to Published / Discarded so it won't be re-processed
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from core.config import NotionReviewConfig, ClaudeConfig
from core.models import Article, ReviewItem
from core.redis_client import RedisClient

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Copied from review_agent — drops cards whose LLM output is a failure message
_LLM_FAILURE_PATTERNS = re.compile(
    r"javascript\s*(error|required|disabled|is not enabled|must be enabled)"
    r"|does not contain sufficient information"
    r"|not enough information"
    r"|unable to (generate|summarize|rewrite|create)"
    r"|cannot (generate|summarize|rewrite|create|write)"
    r"|no (content|article|information|text) (was |is |found |available |provided|to )"
    r"|article (is |was )?(empty|unavailable|not found|inaccessible|behind a paywall)"
    r"|please provide a different article"
    r"|insufficient (content|information|data)"
    r"|does not meet the minimum word count"
    r"|minimum word count requirement",
    re.IGNORECASE,
)


def _score_label(score: float) -> str:
    if score >= 8.5: return "🔵 Excellent"
    if score >= 7.0: return "🟢 Good"
    if score >= 6.0: return "🟡 OK"
    if score >= 5.0: return "🟠 Weak"
    return "🔴 Poor"


def _rt(text: str, limit: int = 2000) -> list[dict]:
    """Wrap a string as a Notion rich_text array, truncated to Notion's limit."""
    return [{"text": {"content": str(text)[:limit]}}]


def _build_media_list(article: Article) -> list[str]:
    """Mirror of review_agent._build_media_list — unchanged logic."""
    video_urls = getattr(article, "video_urls", None) or []
    if video_urls:
        return list(video_urls)
    if article.video_url:
        return [article.video_url]
    is_x_source = (
        "x.com" in (article.url or "")
        or "twitter.com" in (article.url or "")
        or (article.source or "").startswith("@")
    )
    if is_x_source and article.url_to_image:
        return [article.url_to_image]
    return []


class NotionReviewAgent:
    def __init__(
        self,
        config: NotionReviewConfig,
        redis: RedisClient,
        session: aiohttp.ClientSession,
        claude_config: ClaudeConfig | None = None,
        redis_key_overrides: dict | None = None,
        publish_agent=None,
    ) -> None:
        self._config = config
        self._redis = redis
        self._session = session
        self._min_score: float = claude_config.filter_score_threshold if claude_config else 6.0
        self._publish_agent = publish_agent

        overrides = redis_key_overrides or {}
        self._review_pending_prefix: str = overrides.get(
            "review_pending_prefix", redis._config.review_pending_prefix
        )
        self._review_queue_key: str = overrides.get(
            "review_queue_key", redis._config.review_queue_key
        )
        self._publish_queue_key: str = overrides.get(
            "publish_queue_key", redis._config.publish_queue_key
        )

        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._db_id = config.review_database_id
        self._poll_interval_s: int = config.poll_interval_s

        self._queue: asyncio.Queue[tuple[ReviewItem, bool]] = asyncio.Queue(maxsize=500)
        self.autopilot: bool = False

    # ------------------------------------------------------------------ #
    # Public API (mirrors ReviewAgent)                                     #
    # ------------------------------------------------------------------ #

    async def enqueue_article(self, article: Article) -> None:
        """Store article in Redis and queue a Notion card for creation."""
        if (article.llm_score or 0) < self._min_score:
            logger.debug(
                "Skipping card — score %.1f < %.1f: %s",
                article.llm_score or 0, self._min_score, article.title[:60],
            )
            return

        if article.llm_post and _LLM_FAILURE_PATTERNS.search(article.llm_post):
            logger.warning(
                "Skipping card — LLM post failure message for %s", article.title[:60]
            )
            return

        article_id = article.url_hash or str(uuid.uuid4())[:12]

        item = ReviewItem(
            article_id=article_id,
            url=article.url,
            title=article.title,
            description=article.description,
            source=article.source,
            published_at=(
                article.published_at.isoformat()
                if isinstance(article.published_at, datetime)
                else str(article.published_at)
            ),
            url_to_image=article.url_to_image,
            url_hash=article.url_hash,
            llm_score=article.llm_score,
            llm_comment=article.llm_comment,
            post_content=article.llm_post,
            editor_post=article.editor_post,
            media=_build_media_list(article),
        )

        # Persist to Redis (TTL 24 h)
        redis_key = f"{self._review_pending_prefix}{article_id}"
        mapping = {
            k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v))
            for k, v in item.model_dump(mode="json").items()
            if v is not None
        }
        await self._redis.hset(redis_key, mapping)
        await self._redis.expire(redis_key, 86400)

        if self.autopilot:
            await self._redis.lpush(self._publish_queue_key, article_id)
            logger.info(
                "Auto-pilot: approved %s (score=%.1f) → publish queue",
                article_id, item.llm_score or 0,
            )
        else:
            await self._redis.lpush(self._review_queue_key, article_id)

        # Always create a Notion page.
        # Autopilot → Decision="Queued" (already in publish queue above; poll ignores "Queued").
        # Manual    → Decision="Pending" → human approves → poll detects "Approved" → pushes to publish queue.
        try:
            self._queue.put_nowait((item, self.autopilot))
        except asyncio.QueueFull:
            logger.warning("Review queue full — dropping card for %s", article_id)

    async def start_card_sender(self) -> None:
        """Run card-sender and decision-poller concurrently (called from main)."""
        await asyncio.gather(
            self._card_sender_loop(),
            self._poll_loop(),
        )

    # ------------------------------------------------------------------ #
    # Card creation                                                        #
    # ------------------------------------------------------------------ #

    async def _card_sender_loop(self) -> None:
        """Drain the in-memory queue and create Notion pages."""
        while True:
            try:
                item, is_autopilot = await self._queue.get()
                await self._create_notion_page(item, is_autopilot)
                self._queue.task_done()
            except Exception as e:
                logger.error("Notion card sender error: %s", e)
            await asyncio.sleep(0.5)

    async def _create_notion_page(self, item: ReviewItem, is_autopilot: bool = False) -> None:
        """POST a new page to the review database."""
        properties: dict = {
            "Title":        {"title": _rt(item.title or "(no title)")},
            "Post Content": {"rich_text": _rt(item.post_content or "")},
            "Source":       {"rich_text": _rt(item.source or "", limit=500)},
            "Score":        {"number": round(item.llm_score or 0, 1)},
            "Score Label":  {"select": {"name": _score_label(item.llm_score or 0)}},
            "AI Comment":   {"rich_text": _rt(item.llm_comment or "", limit=1000)},
            "Published":    {"date": None},
            "Created":      {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            "duplicated":   {"checkbox": False},
            "send_status":  {"checkbox": False},
            "Decision":     {"select": {"name": "Queued" if is_autopilot else "Pending"}},
            "Article ID":   {"rich_text": _rt(item.article_id, limit=100)},
        }

        # URL properties must be null (omitted) when empty — Notion rejects ""
        if item.url and item.url.startswith("http"):
            properties["Source URL"] = {"url": item.url}
        if item.url_to_image and item.url_to_image.startswith("http"):
            properties["Image URL"] = {"url": item.url_to_image}

        # Embed image as a block in the page body (visible when opening the page)
        children = []
        if item.url_to_image and item.url_to_image.startswith("http"):
            children.append({
                "object": "block",
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": item.url_to_image},
                },
            })

        body: dict = {
            "parent": {"database_id": self._db_id},
            "properties": properties,
        }
        if children:
            body["children"] = children

        try:
            async with self._session.post(
                f"{NOTION_API_BASE}/pages",
                headers=self._headers,
                json=body,
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(
                        "Notion page creation failed (%d) for %s: %s",
                        resp.status, item.article_id, text[:300],
                    )
                else:
                    page_data = await resp.json(content_type=None)
                    page_id = page_data.get("id", "")
                    logger.info(
                        "Notion card created: article=%s score=%.1f page_id=%s",
                        item.article_id, item.llm_score or 0, page_id,
                    )
                    if page_id:
                        redis_key = f"{self._review_pending_prefix}{item.article_id}"
                        try:
                            await self._redis.hset(redis_key, {"notion_page_id": page_id})
                        except Exception as e:
                            logger.warning("Could not store notion_page_id for %s: %s", item.article_id, e)
        except Exception as e:
            logger.error("Notion page creation error for %s: %s", item.article_id, e)

    # ------------------------------------------------------------------ #
    # Decision polling                                                     #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        """Poll the Notion database every poll_interval_s for user decisions."""
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self._process_decisions()
            except Exception as e:
                logger.warning("Notion poll error: %s", e)

    async def _process_decisions(self) -> None:
        """Query for Approved / Rejected / Publish Now pages and act on each."""
        body = {
            "filter": {
                "or": [
                    {"property": "Decision", "select": {"equals": "Approved"}},
                    {"property": "Decision", "select": {"equals": "Rejected"}},
                    {"property": "Decision", "select": {"equals": "Publish Now"}},
                ]
            },
            "page_size": 100,
        }
        try:
            async with self._session.post(
                f"{NOTION_API_BASE}/databases/{self._db_id}/query",
                headers=self._headers,
                json=body,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Notion poll query failed (%d): %s", resp.status, text[:200]
                    )
                    return
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("Notion poll request error: %s", e)
            return

        pages = data.get("results", [])
        if pages:
            logger.info("Notion poll: %d decision(s) to process", len(pages))
        for page in pages:
            await self._handle_decision(page)

    async def _handle_decision(self, page: dict) -> None:
        page_id = page.get("id", "")
        props = page.get("properties", {})

        decision = (
            (props.get("Decision") or {}).get("select") or {}
        ).get("name", "")

        article_id = self._get_rich_text(props, "Article ID")
        if not article_id:
            logger.warning("Notion page %s missing Article ID — discarding", page_id)
            await self._mark_page(page_id, "Discarded")
            return

        edit_override = (self._get_rich_text(props, "Edit Override") or "").strip()
        notion_image_url = self._get_url_property(props, "Image URL")
        redis_key = f"{self._review_pending_prefix}{article_id}"

        if decision in ("Approved", "Publish Now"):
            # Reconstruct Redis hash from Notion if the key expired (articles can sit
            # in review longer than the 24 h Redis TTL before being approved).
            existing = await self._redis.hgetall(redis_key)
            if not existing:
                # Key expired — rebuild from Notion page properties
                notion_title = self._get_rich_text(props, "Title") or ""
                notion_post_content = edit_override or self._get_rich_text(props, "Post Content") or ""
                notion_source_url = self._get_url_property(props, "Source URL") or ""
                mapping: dict = {"article_id": article_id}
                if notion_title:
                    mapping["title"] = notion_title
                if notion_post_content:
                    mapping["post_content"] = notion_post_content
                if notion_source_url:
                    mapping["url"] = notion_source_url
                if notion_image_url:
                    mapping["url_to_image"] = notion_image_url
                mapping["media"] = "[]"
                mapping["notion_page_id"] = page_id
                try:
                    await self._redis.hset(redis_key, mapping)
                    await self._redis.expire(redis_key, 3600)  # 1 h — enough to survive publish
                    logger.info(
                        "Reconstructed expired Redis hash from Notion for article %s", article_id
                    )
                except Exception as e:
                    logger.warning(
                        "Could not reconstruct Redis hash for %s: %s", article_id, e
                    )
            else:
                # Key alive — apply overrides selectively
                updates: dict = {}
                if edit_override:
                    updates["post_content"] = edit_override
                if notion_image_url:
                    updates["url_to_image"] = notion_image_url
                if updates:
                    try:
                        await self._redis.hset(redis_key, updates)
                    except Exception as e:
                        logger.warning(
                            "Could not update Redis hash for %s: %s", article_id, e
                        )

            # Topical dedup now runs unconditionally in PublishAgent._process_one (see
            # services/notion_topical_dedup.py) — that path is the only one that actually
            # executes under auto-pilot, unlike this manual "Approved" transition.

            if decision == "Publish Now" and self._publish_agent is not None:
                # Mark as Queued first so it won't reappear while publish_one() runs.
                # Decision is only set to Published by the callback after Gettr succeeds.
                await self._mark_page(page_id, "Queued")
                try:
                    success = await self._publish_agent.publish_one(article_id)
                    logger.info(
                        "Notion Publish Now: article=%s success=%s", article_id, success
                    )
                except Exception as e:
                    logger.error(
                        "Notion Publish Now failed for %s: %s", article_id, e
                    )
            else:
                await self._redis.lpush(self._publish_queue_key, article_id)
                await self._mark_page(page_id, "Queued")
                logger.info("Notion approved: article %s → publish queue", article_id)

        elif decision == "Rejected":
            try:
                await self._redis.delete(redis_key)
            except Exception as e:
                logger.warning(
                    "Could not delete Redis key for %s: %s", article_id, e
                )
            await self._mark_page(page_id, "Discarded")
            logger.info("Notion rejected: article %s removed", article_id)

    async def update_posted_page(self, page_id: str) -> None:
        """Called by PublishAgent after a successful Gettr post (DailyNews only).

        Sets Published date to now, checks send_status, and sets Decision to Published.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={
                    "properties": {
                        "Published":   {"date": {"start": now}},
                        "send_status": {"checkbox": True},
                        "Decision":    {"select": {"name": "Published"}},
                    }
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Notion post-publish update failed (%d) for page %s: %s",
                        resp.status, page_id, text[:200],
                    )
                else:
                    logger.info("Notion page %s marked Published after Gettr post", page_id)
        except Exception as e:
            logger.warning("Notion post-publish update error for page %s: %s", page_id, e)

    async def update_dropped_page(self, page_id: str) -> None:
        """Called by PublishAgent when an article is silently dropped (e.g. no image found).
        Sets Decision to Discarded so the page does not stay stuck at Queued.
        """
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"Decision": {"select": {"name": "Discarded"}}}},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Notion drop update failed (%d) for page %s: %s",
                        resp.status, page_id, text[:200],
                    )
                else:
                    logger.info("Notion page %s marked Discarded (dropped — no image)", page_id)
        except Exception as e:
            logger.warning("Notion drop update error for page %s: %s", page_id, e)

    async def _mark_duplicate(self, page_id: str) -> None:
        """Mark a page as duplicate: duplicated=True, Decision=Rejected."""
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={
                    "properties": {
                        "duplicated": {"checkbox": True},
                        "Decision":   {"select": {"name": "Rejected"}},
                    }
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Notion mark_duplicate failed (%d) for page %s: %s",
                        resp.status, page_id, text[:200],
                    )
                else:
                    logger.info("Notion page %s marked as duplicate (duplicated=True, Decision=Rejected)", page_id)
        except Exception as e:
            logger.warning("Notion mark_duplicate error for page %s: %s", page_id, e)

    async def _mark_page(self, page_id: str, decision: str) -> None:
        """Update the Decision select on a Notion page."""
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"Decision": {"select": {"name": decision}}}},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Notion mark_page failed (%d) for %s: %s",
                        resp.status, page_id, text[:200],
                    )
        except Exception as e:
            logger.warning("Notion mark_page error for %s: %s", page_id, e)

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_rich_text(props: dict, key: str) -> Optional[str]:
        p = props.get(key, {})
        if p.get("type") == "rich_text":
            parts = p.get("rich_text", [])
            return "".join(t.get("plain_text", "") for t in parts).strip() or None
        return None

    @staticmethod
    def _get_url_property(props: dict, key: str) -> Optional[str]:
        p = props.get(key, {})
        if p.get("type") == "url":
            return p.get("url") or None
        return None

    async def get_fallback_articles(self, lookback_hours: int = 6) -> list[str]:
        """Query agent_queue_dailynews for Decision in (Approved, Queued) within lookback_hours,
        sorted by Score desc. Rebuilds any expired Redis hashes from Notion. Returns article_id list.
        Called by PublishAgent when nothing was posted from the queue (stock fallback).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
        body = {
            "filter": {
                "and": [
                    {
                        "or": [
                            {"property": "Decision", "select": {"equals": "Approved"}},
                            {"property": "Decision", "select": {"equals": "Queued"}},
                        ]
                    },
                    {"property": "Created", "date": {"on_or_after": cutoff}},
                ]
            },
            "sorts": [{"property": "Score", "direction": "descending"}],
            "page_size": 50,
        }
        try:
            async with self._session.post(
                f"{NOTION_API_BASE}/databases/{self._db_id}/query",
                headers=self._headers,
                json=body,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "get_fallback_articles: Notion query failed (%d): %s", resp.status, text[:200]
                    )
                    return []
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("get_fallback_articles: Notion query error: %s", e)
            return []

        article_ids: list[str] = []
        for page in data.get("results", []):
            props = page.get("properties", {})
            page_id = page.get("id", "")
            article_id = self._get_rich_text(props, "Article ID") or ""
            if not article_id:
                continue

            redis_key = f"{self._review_pending_prefix}{article_id}"
            existing = await self._redis.hgetall(redis_key)
            if not existing:
                # Rebuild from Notion page fields
                notion_title = self._get_rich_text(props, "Title") or ""
                notion_post_content = self._get_rich_text(props, "Post Content") or ""
                notion_source_url = self._get_url_property(props, "Source URL") or ""
                notion_image_url = self._get_url_property(props, "Image URL") or ""
                mapping: dict = {"article_id": article_id, "media": "[]"}
                if page_id:
                    mapping["notion_page_id"] = page_id
                if notion_title:
                    mapping["title"] = notion_title
                if notion_post_content:
                    mapping["post_content"] = notion_post_content
                if notion_source_url:
                    mapping["url"] = notion_source_url
                if notion_image_url:
                    mapping["url_to_image"] = notion_image_url
                try:
                    await self._redis.hset(redis_key, mapping)
                    await self._redis.expire(redis_key, 3600)
                    logger.debug(
                        "get_fallback_articles: rebuilt Redis hash for article %s (page %s)",
                        article_id, page_id,
                    )
                except Exception as e:
                    logger.warning(
                        "get_fallback_articles: could not rebuild Redis hash for %s: %s", article_id, e
                    )
                    continue
            else:
                # Ensure notion_page_id is stored so _on_posted can update Notion
                if not existing.get("notion_page_id") and page_id:
                    try:
                        await self._redis.hset(redis_key, {"notion_page_id": page_id})
                    except Exception:
                        pass

            article_ids.append(article_id)

        logger.info(
            "get_fallback_articles: found %d stocked article(s) within last %dh",
            len(article_ids), lookback_hours,
        )
        return article_ids
