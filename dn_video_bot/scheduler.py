"""
Two publish paths sharing one 60s poll:

- Normal tick (15 min by default): pick one row by priority (🔥 > oldest
  normal), avoid the human n8n job's :20 publish slot, then run it through
  fetch → analyze → caption → publish → write-back.
- 🔥🔥🔥 instant path (2026-07-31 change): does NOT wait for the 15-min tick
  or the daily cap — publishes as soon as a 🔥🔥🔥 row is seen, gated only by
  a minimum gap since the last publish (any priority) so a burst of several
  🔥🔥🔥 rows at once doesn't flood Gettr. Publishing (via either path)
  resets the 15-min clock for the next normal-priority post.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from core.redis_client import RedisClient

from .caption_writer import CaptionWriter
from .content_dedup import ContentDedup
from .dedup import UrlDedup
from .notion_source import NotionRow, NotionSource
from .publisher import Publisher
from .transcriber import Transcriber

from .tweet_fetcher import TweetFetcher, TweetFetchError

logger = logging.getLogger(__name__)

_CAP_KEY = "dn_video:daily_cap"
_POLL_S = 60


class Scheduler:
    def __init__(
        self,
        notion: NotionSource,
        url_dedup: UrlDedup,
        content_dedup: ContentDedup,
        tweet_fetcher: TweetFetcher,
        caption_writer: CaptionWriter,
        transcriber: Transcriber,
        publisher: Publisher,
        redis: RedisClient,
        tick_interval_minutes: int,
        daily_cap: int,
        daily_cap_window_hours: int,
        avoid_minute: int,
        avoid_delay_seconds: int,
        min_fire_gap_minutes: int = 5,
        debug_suffix: bool = False,
    ) -> None:
        self._notion = notion
        self._url_dedup = url_dedup
        self._content_dedup = content_dedup
        self._tweet_fetcher = tweet_fetcher
        self._caption_writer = caption_writer
        self._transcriber = transcriber
        self._publisher = publisher
        self._redis = redis
        self._tick_interval_s = tick_interval_minutes * 60
        self._daily_cap = daily_cap
        self._daily_cap_window_s = daily_cap_window_hours * 3600
        self._avoid_minute = avoid_minute
        self._avoid_delay_s = avoid_delay_seconds
        self._min_fire_gap_s = min_fire_gap_minutes * 60
        # Test-only: append account handle + urgency to the Gettr body so a
        # human reviewing test posts doesn't have to cross-reference Notion.
        # Never included in Notion's post_content or in the dedup embedding —
        # both should reflect the clean editorial caption only.
        self._debug_suffix = debug_suffix
        self._last_published_at = 0.0

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(_POLL_S)
            try:
                if await self._maybe_handle_instant_fire():
                    continue
                if time.time() - self._last_published_at < self._tick_interval_s:
                    continue
                await self._tick()
            except Exception as e:
                logger.error("Scheduler tick failed: %s", e)

    async def _fetch_candidates(self) -> list[NotionRow]:
        rows = await self._notion.fetch_eligible_rows()
        candidates: list[NotionRow] = []
        for row in rows:
            # A prior Duplicate drop already marked this URL "seen" — an
            # editor override (Duplicate + 🔥 or 🔥🔥🔥) must still get through.
            if row.was_duplicate and row.priority >= 1:
                candidates.append(row)
                continue
            if await self._url_dedup.already_seen(row.url):
                continue
            candidates.append(row)
        return candidates

    async def _maybe_handle_instant_fire(self) -> bool:
        """🔥🔥🔥 bypasses the 15-min tick, the daily cap, AND the :20
        avoid-slot delay entirely (per explicit user instruction — truly
        immediate, no exceptions) — only gated by a minimum gap since the
        last publish (any priority), so a burst of several 🔥🔥🔥 rows at
        once doesn't flood Gettr. Tries every 🔥🔥🔥 candidate in order
        (oldest first) until one actually publishes — a row that fails to
        fetch or turns out to be a duplicate doesn't consume the gap, since
        nothing was actually posted. Returns True only on an actual publish
        (caller should skip the normal tick this cycle either way)."""
        if time.time() - self._last_published_at < self._min_fire_gap_s:
            return False

        candidates = [r for r in await self._fetch_candidates() if r.priority == 2]
        for row in candidates:  # Notion already sorted oldest-first
            if await self._process(row):
                self._last_published_at = time.time()
                return True
        return False

    async def _tick(self) -> None:
        candidates = await self._fetch_candidates()
        # Notion API already sorted oldest-first; a stable sort on priority keeps
        # that ordering within each priority tier (🔥 > normal). 🔥🔥🔥 rows are
        # handled by _maybe_handle_instant_fire before this ever runs, but a
        # stale one could still show up here (e.g. dedup already marked it) —
        # excluded so it's never double-counted against the daily cap.
        candidates = [r for r in candidates if r.priority < 2]
        candidates.sort(key=lambda r: -r.priority)

        for row in candidates:
            if row.priority == 0 and await self._cap_reached():
                logger.info("Daily cap reached — holding off on normal-priority rows this tick")
                return

            if datetime.now().minute == self._avoid_minute:
                logger.info("Avoiding :%02d publish slot — sleeping %ds", self._avoid_minute, self._avoid_delay_s)
                await asyncio.sleep(self._avoid_delay_s)

            if await self._process(row):
                self._last_published_at = time.time()
                if row.priority == 0:
                    await self._record_cap_usage()
                return
            # row failed to fetch or was a duplicate — nothing was actually
            # published, so try the next candidate instead of wasting this tick.

    async def _process(self, row: NotionRow) -> bool:
        """Returns True only if a post actually went out — callers must not
        treat a skipped row (fetch failure, duplicate, empty caption) as
        having consumed a tick/fire-gap."""
        try:
            tweet = await self._tweet_fetcher.fetch(row.url)
        except TweetFetchError as e:
            logger.warning("Skipping row %s — %s", row.page_id, e)
            await self._url_dedup.mark_seen(row.url)
            return False

        editor_wrote_it = bool(row.post_content.strip())
        if editor_wrote_it:
            # Editor already wrote post_content themselves — use it verbatim,
            # no AI rewrite, no JUST IN/BREAKING prefix added on top of it.
            caption = row.post_content.strip()
        else:
            transcript = await self._transcriber.transcribe(tweet.audio_source_url)
            caption = await self._caption_writer.write(
                tweet_text=tweet.text,
                tweet_created_at=tweet.created_at,
                urgency=row.urgency,
                account_name=tweet.account_name,
                account_handle=tweet.account_handle,
                account_label=tweet.account_label,
                mentions=tweet.mentions,
                transcript=transcript,
            )
            if not caption.strip() or not caption.strip().split(" - ", 1)[-1].strip():
                logger.warning("Skipping row %s — caption writer returned an empty body", row.page_id)
                await self._url_dedup.mark_seen(row.url)
                return False

        override_duplicate = row.was_duplicate and row.priority >= 1
        if override_duplicate:
            logger.info(
                "Row %s previously marked Duplicate but editor raised Urgency to %s — overriding, publishing anyway",
                row.page_id, row.urgency,
            )
        elif await self._content_dedup.is_duplicate(caption):
            logger.info("Row %s dropped — matches a recently published caption", row.page_id)
            await self._url_dedup.mark_seen(row.url)
            await self._notion.mark_duplicate(
                row.page_id,
                "[内容去重] 与近期(本机器人)已发布内容相似，判定为重复，未发布。",
            )
            return False

        gettr_body = caption
        if self._debug_suffix:
            gettr_body += f"\n\n[TEST] source=@{tweet.account_handle or 'unknown'} urgency={row.urgency or 'none'}"

        post_id = await self._publisher.publish_video(tweet.video_url, gettr_body)
        link = Publisher.post_link(post_id)

        if editor_wrote_it:
            await self._notion.mark_note_only(row.page_id, link)
        else:
            await self._notion.mark_published(row.page_id, caption, link)
        if override_duplicate:
            await self._notion.clear_duplicate(row.page_id)
        await self._url_dedup.mark_seen(row.url)
        await self._content_dedup.record(row.page_id, row.url, caption)

        logger.info("Published row %s (priority=%d) -> %s", row.page_id, row.priority, link)
        return True

    async def _cap_reached(self) -> bool:
        cutoff = time.time() - self._daily_cap_window_s
        await self._redis.zremrangebyscore(_CAP_KEY, 0, cutoff)
        return await self._redis.zcard(_CAP_KEY) >= self._daily_cap

    async def _record_cap_usage(self) -> None:
        now = time.time()
        await self._redis.zadd(_CAP_KEY, {str(now): now})
