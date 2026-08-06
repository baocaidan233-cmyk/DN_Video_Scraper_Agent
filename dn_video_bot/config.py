"""
Standalone config for the DN Video Scraper bot.

Deliberately does NOT touch core/config.py (leon's file) — reads config.yaml
itself and reuses the existing Pydantic sub-models (GettrConfig, OpenAIConfig,
etc.) plus a new `dn_video:` top-level section for this bot's own settings.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from core.config import (
    GcpConfig,
    GettrConfig,
    OpenAIConfig,
    RedisConfig,
)


class DnVideoBotConfig(BaseModel):
    notion_api_key: str
    notion_database_id: str
    status_value: str = "breaking log"
    row_lookback_hours: int = 24  # ignore rows created before this — old/stale entries shouldn't surface

    tick_interval_minutes: int = 15
    daily_cap: int = 30
    daily_cap_window_hours: int = 24

    # Avoid clashing with the human-editor n8n job that publishes at :20 every hour.
    avoid_minute: int = 20
    avoid_delay_seconds: int = 300

    # 🔥🔥🔥 bypasses the 15-min tick and the daily cap, publishing immediately —
    # gated only by this minimum gap since the last publish (any priority),
    # so several 🔥🔥🔥 rows appearing at once don't flood Gettr.
    min_fire_gap_minutes: int = 5

    # Content-similarity dedup (own posts only, see content_dedup.py)
    content_similarity_threshold: float = 0.8
    content_similarity_window_hours: int = 12

    # JUST IN vs BREAKING prefix
    breaking_window_hours: int = 4

    redis_url_hash_prefix: str = "dn_video:url_hash:"
    url_dedup_ttl_hours: int = 24  # a repeat of the same link past this window is treated as new
    content_dedup_redis_key: str = "dn_video:content_embeddings"

    use_test_account: bool = True

    # Caption is a rewrite of the tweet's own text PLUS a transcript of the
    # video's audio (see transcriber.py, added back 2026-08-06 — the transcript
    # regularly carries far more detail than the tweet text alone summarized).
    caption_model: str = "gpt-4o-mini"
    transcribe_model: str = "gpt-4o-mini-transcribe"


class Resources(BaseModel):
    """Bundle of everything run.py needs to construct the bot's clients."""

    model_config = {"arbitrary_types_allowed": True}

    bot: DnVideoBotConfig
    gettr: GettrConfig
    gcp: GcpConfig
    openai: OpenAIConfig
    redis: RedisConfig


def load_resources(path: str | Path = "config.yaml") -> Resources:
    with open(path) as f:
        data = yaml.safe_load(f)

    dn_video_raw = data.get("dn_video", {})
    notion_dedup = data["notion_dedup"]
    bot = DnVideoBotConfig(
        notion_api_key=notion_dedup["api_key"],
        notion_database_id=notion_dedup["article_database_id"],
        **dn_video_raw,
    )

    if bot.use_test_account:
        # Prefer our own test account (gettr_test_own) over the colleague's
        # shared gettr_test — added 2026-07-31 so this bot never posts to
        # someone else's test account, and never touches their config entry.
        gettr_section = "gettr_test_own" if "gettr_test_own" in data else "gettr_test"
    else:
        gettr_section = "gettr"
    gettr = GettrConfig(**data[gettr_section])

    gcp = GcpConfig(**data.get("gcp", {}))
    openai = OpenAIConfig(**data["openai"])
    redis = RedisConfig(**data["redis"])

    return Resources(
        bot=bot, gettr=gettr, gcp=gcp, openai=openai, redis=redis,
    )
