"""
Entry point. Run from the repo root (same directory as config.yaml):

    python3 -m dn_video_bot.run

Manual nohup only for v1.0 — no systemd unit yet (see plan doc, step 5).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from core.redis_client import RedisClient
from services.gcp_client import GcpClient
from services.gettr_client import GettrClient
from services.openai_client import OpenAIClient

from .config import load_resources
from .content_dedup import ContentDedup
from .caption_writer import CaptionWriter
from .dedup import UrlDedup
from .notion_source import NotionSource
from .publisher import Publisher
from .scheduler import Scheduler
from .transcriber import Transcriber

from .tweet_fetcher import TweetFetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    resources = load_resources("config.yaml")
    bot = resources.bot

    logger.info(
        "Starting DN Video Scraper — test_account=%s tick=%dmin cap=%d/%dh",
        bot.use_test_account, bot.tick_interval_minutes, bot.daily_cap, bot.daily_cap_window_hours,
    )

    redis = RedisClient(resources.redis)
    await redis.connect()

    openai = OpenAIClient(resources.openai)
    content_dedup = ContentDedup(
        redis=redis, openai=openai,
        threshold=bot.content_similarity_threshold,
        window_hours=bot.content_similarity_window_hours,
        redis_key=bot.content_dedup_redis_key,
    )

    async with aiohttp.ClientSession() as session:
        notion = NotionSource(
            session, bot.notion_api_key, bot.notion_database_id, bot.status_value,
            lookback_hours=bot.row_lookback_hours,
        )
        url_dedup = UrlDedup(redis, bot.redis_url_hash_prefix, ttl_hours=bot.url_dedup_ttl_hours)
        tweet_fetcher = TweetFetcher(session)
        caption_writer = CaptionWriter(resources.openai.api_key, bot.caption_model, bot.breaking_window_hours)
        transcriber = Transcriber(resources.openai.api_key, bot.transcribe_model, session)
        gcp_client = GcpClient(resources.gettr, resources.gcp, session)
        gettr_client = GettrClient(resources.gettr, session)
        publisher = Publisher(gcp_client, gettr_client)

        scheduler = Scheduler(
            notion=notion,
            url_dedup=url_dedup,
            content_dedup=content_dedup,
            tweet_fetcher=tweet_fetcher,
            caption_writer=caption_writer,
            transcriber=transcriber,
            publisher=publisher,
            redis=redis,
            tick_interval_minutes=bot.tick_interval_minutes,
            daily_cap=bot.daily_cap,
            daily_cap_window_hours=bot.daily_cap_window_hours,
            avoid_minute=bot.avoid_minute,
            avoid_delay_seconds=bot.avoid_delay_seconds,
            min_fire_gap_minutes=bot.min_fire_gap_minutes,
            debug_suffix=bot.use_test_account,
        )
        await scheduler.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
