"""
Notion Topical Dedup Checker — embedding-based, replaces the earlier Haiku
pairwise-LLM version (which only ever ran on the manual-review "Approved"
transition and therefore never fired once both pipelines went full auto-pilot).

Compares against the human-editor "daily_news" Notion database only. The
bot's own review DB (agent_queue_dailynews) was dropped from this checker —
nothing writes to it manually any more, so comparing against it was comparing
the bot against itself.

Three checks, each serving a different point in the flow:

  - before_publish(): candidate vs cards already published in the last
    `recent_lookback_hours` (send_status=True, Last edited time >= cutoff).
    A match means a human editor (via the hourly n8n "waiting for post" job)
    already published the same story — the caller should skip.
    `enforce_recent_skip` gates whether a match actually skips or just logs;
    defaults off so this can run in shadow mode against live traffic before
    it's trusted to affect what gets published.

  - after_publish(): candidate (just posted by the bot) vs NOT-yet-sent
    pending cards (send_status=False, status in ('2nd_eye','waiting for
    post')). A match means a human editor still has the same story queued —
    marks that Notion card's Duplicate select + appends the just-published
    Gettr link to Notes, so the editor sees it before the next hourly n8n
    run would otherwise publish it again. Only possible *after* a successful
    publish, since the Gettr link doesn't exist before then.

  - run_gettr_crosscheck_loop(): periodically compares not-yet-sent pending
    cards against a GettrFeedClient's recent posts. Catches the reverse gap:
    the bot posts a story that was never entered into (or already left) this
    Notion database, so before_publish/after_publish never had a candidate
    to compare against — this checks the actual Gettr timeline instead.

All three use OpenAI embeddings + cosine similarity, not LLM judgment.
Embeddings for Notion pages are cached in-process, keyed by page id and
invalidated on "Last edited time" change — the pending/recent-published sets
are small (tens of items), so this stays cheap without a persistent store.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from services.openai_client import OpenAIClient

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

_PENDING_STATUSES = ("2nd_eye", "waiting for post")
_MAX_NOTES_CHARS = 2000  # Notion rich_text block limit


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class NotionTopicalDedupChecker:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        daily_news_api_key: str,
        daily_news_db_id: str,
        openai: OpenAIClient,
        similarity_threshold: float = 0.80,
        recent_lookback_hours: int = 24,
        enforce_recent_skip: bool = False,
    ) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {daily_news_api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._db_id = daily_news_db_id
        self._openai = openai
        self._threshold = similarity_threshold
        self._lookback_hours = recent_lookback_hours
        self.enforce_recent_skip = enforce_recent_skip
        # page_id -> (last_edited_time, embedding) — invalidated when the page changes
        self._embed_cache: dict[str, tuple[str, list[float]]] = {}

    # ------------------------------------------------------------------ #
    # Public checks                                                       #
    # ------------------------------------------------------------------ #

    async def before_publish(self, candidate_content: str) -> bool:
        """Compare against cards published in the last N hours. Returns True
        only if a match is found AND enforce_recent_skip is on — otherwise
        always returns False (shadow mode: logs what would have happened)."""
        if not candidate_content.strip():
            return False
        recent = await self._fetch_recent_published()
        if not recent:
            return False
        match = await self._find_match(candidate_content, recent)
        if not match:
            return False
        if self.enforce_recent_skip:
            logger.info(
                "Topical dedup (before_publish): skipping — matches recently-published Notion card %s",
                match["id"],
            )
            return True
        logger.info(
            "Topical dedup (before_publish): WOULD skip (enforce_recent_skip=False) — matches Notion card %s",
            match["id"],
        )
        return False

    async def after_publish(self, candidate_content: str, gettr_post_id: str) -> None:
        """Mark any not-yet-sent pending Notion card matching the just-published
        candidate as Duplicate, with the new Gettr link appended to Notes."""
        if not candidate_content.strip() or not gettr_post_id:
            return
        pending = await self._fetch_pending()
        if not pending:
            return
        match = await self._find_match(candidate_content, pending)
        if not match:
            return
        gettr_link = f"https://gettr.com/post/{gettr_post_id}"
        await self._mark_duplicate(match["id"], match.get("notes", ""), gettr_link)
        logger.info(
            "Topical dedup (after_publish): marked pending Notion card %s duplicate of %s",
            match["id"], gettr_link,
        )

    async def run_gettr_crosscheck_loop(self, gettr_client, interval_minutes: int = 15) -> None:
        """Periodically compares not-yet-sent pending cards against gettr_client's
        recent post feed — catches stories the bot posted that never had (or no
        longer have) a matching candidate to diff against in before_publish/
        after_publish. Runs forever; a failed cycle just logs and retries."""
        while True:
            try:
                await self._run_gettr_crosscheck_once(gettr_client)
            except Exception as e:
                logger.error("Gettr crosscheck cycle failed: %s", e)
            await asyncio.sleep(interval_minutes * 60)

    async def _run_gettr_crosscheck_once(self, gettr_client) -> None:
        pending = await self._fetch_pending()
        if not pending:
            return
        gettr_posts = [p for p in await gettr_client.fetch_recent_posts() if p["text"].strip()]
        if not gettr_posts:
            return

        gettr_embeddings = await self._openai.embed_texts([p["text"] for p in gettr_posts])
        for page in pending:
            embedding = await self._embed_page(page)
            if embedding is None:
                continue
            for post, post_embedding in zip(gettr_posts, gettr_embeddings):
                if _cosine(embedding, post_embedding) >= self._threshold:
                    gettr_link = f"https://gettr.com/post/{post['id']}"
                    await self._mark_duplicate(page["id"], page.get("notes", ""), gettr_link)
                    logger.info(
                        "Gettr crosscheck: marked pending Notion card %s duplicate of %s",
                        page["id"], gettr_link,
                    )
                    break

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _find_match(self, candidate_content: str, pages: list[dict]) -> Optional[dict]:
        try:
            candidate_embedding = (await self._openai.embed_texts([candidate_content]))[0]
        except Exception as e:
            logger.warning("Topical dedup: embedding candidate failed (%s) — allowing through", e)
            return None
        for page in pages:
            embedding = await self._embed_page(page)
            if embedding is None:
                continue
            if _cosine(candidate_embedding, embedding) >= self._threshold:
                return page
        return None

    async def _embed_page(self, page: dict) -> Optional[list[float]]:
        page_id = page["id"]
        last_edited = page["last_edited_time"]
        cached = self._embed_cache.get(page_id)
        if cached and cached[0] == last_edited:
            return cached[1]
        content = page["post_content"]
        if not content.strip():
            return None
        try:
            embedding = (await self._openai.embed_texts([content]))[0]
        except Exception as e:
            logger.warning("Topical dedup: embedding Notion page %s failed (%s) — skipping", page_id, e)
            return None
        self._embed_cache[page_id] = (last_edited, embedding)
        return embedding

    async def _fetch_pending(self) -> list[dict]:
        """send_status=False AND Duplicate is empty AND status in ('2nd_eye','waiting for post')."""
        filter_ = {
            "and": [
                {"property": "send_status", "checkbox": {"equals": False}},
                {"property": "Duplicate", "select": {"is_empty": True}},
                {"or": [
                    {"property": "status", "status": {"equals": s}} for s in _PENDING_STATUSES
                ]},
            ]
        }
        return await self._query(filter_)

    async def _fetch_recent_published(self) -> list[dict]:
        """send_status=True AND Last edited time >= now - lookback_hours.

        Deliberately keys off the built-in "Last edited time", not "TimerForPub"
        (unused by editors — not a reliable publish-time signal)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)).isoformat()
        filter_ = {
            "and": [
                {"property": "send_status", "checkbox": {"equals": True}},
                {"property": "Last edited time", "last_edited_time": {"on_or_after": cutoff}},
            ]
        }
        return await self._query(filter_)

    async def _query(self, filter_: dict) -> list[dict]:
        pages: list[dict] = []
        cursor: Optional[str] = None

        while True:
            body: dict = {"filter": filter_, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            try:
                async with self._session.post(
                    f"{NOTION_API_BASE}/databases/{self._db_id}/query",
                    headers=self._headers,
                    json=body,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning("Topical dedup: Notion query failed (%d): %s", resp.status, text[:200])
                        break
                    data = await resp.json(content_type=None)
            except Exception as e:
                logger.warning("Topical dedup: Notion query error: %s", e)
                break

            for page in data.get("results", []):
                props = page.get("properties", {})
                content = self._get_rich_text(props, "post_content")
                if content:
                    pages.append({
                        "id": page["id"],
                        "last_edited_time": page.get("last_edited_time", ""),
                        "post_content": content,
                        "notes": self._get_rich_text(props, "Notes"),
                    })

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return pages

    async def _mark_duplicate(self, page_id: str, existing_notes: str, gettr_link: str) -> None:
        note_line = f"[topical dedup] 疑似与已发布内容重复: {gettr_link}"
        new_notes = f"{existing_notes}\n{note_line}" if existing_notes else note_line
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={
                    "properties": {
                        "Duplicate": {"select": {"name": "Duplicate"}},
                        "Notes": {"rich_text": [{"text": {"content": new_notes[-_MAX_NOTES_CHARS:]}}]},
                    }
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        "Topical dedup: failed to mark page %s duplicate (%d): %s",
                        page_id, resp.status, text[:200],
                    )
        except Exception as e:
            logger.warning("Topical dedup: error marking page %s duplicate: %s", page_id, e)

    @staticmethod
    def _get_rich_text(props: dict, key: str) -> str:
        p = props.get(key, {})
        if p.get("type") == "rich_text":
            return "".join(t.get("plain_text", "") for t in p.get("rich_text", [])).strip()
        return ""
