"""
Generic, config-driven channel builder.

A "channel" is an EpicFury-style social-media news pipeline (X + websites →
score/rewrite → Notion review → Gettr publish). Every channel listed under
`config.channels` is built here — no per-channel code exists anywhere else.

This mirrors the hardcoded EpicFury init block in main.py, but parameterised by
a `ChannelConfig`. DailyNews and EpicFury themselves are NOT built here; they
remain hardcoded in main.py (DailyNews is genuinely different; EF predates this
mechanism). Behaviour is identical to EF: `pipeline_type="epicfury"` drives the
publish agent (OG-preview fallback, no Notion callbacks), Gemma/topical-dedup
stay off, and history run_types are slug-based so channels never mix.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from core.config import Config, ChannelConfig
from core.redis_client import RedisClient
from services.openai_client import OpenAIClient
from services.claude_client import ClaudeClient
from services.gettr_client import GettrClient
from services.gcp_client import GcpClient
from services.qdrant_client import QdrantWrapper
from agents.similarity_agent import SimilarityAgent
from agents.publish_agent import PublishAgent
from agents.notion_review_agent import NotionReviewAgent
from core.pipeline import Pipeline
from dashboard.state import DashboardState

logger = logging.getLogger(__name__)


@dataclass
class ChannelRuntime:
    """Everything main.py needs to schedule a channel's loops + dashboard."""
    slug: str
    title: str
    dashboard_port: int
    state: DashboardState
    redis_keys: dict
    qdrant_config: object
    enabled: bool                       # True only if the full pipeline started
    # None until enabled=True:
    pipeline: Pipeline | None = None
    publish_agent: PublishAgent | None = None
    review_agent: NotionReviewAgent | None = None
    similarity_agent: SimilarityAgent | None = None
    x_agent: object | None = None
    # dashboard identity (per-channel, so the config editor/history/title are correct)
    prompt_files: list[str] | None = None
    sources_path: str | None = None
    run_types: list[str] | None = None


async def build_channel(
    config: Config,
    ch: ChannelConfig,
    *,
    redis: RedisClient,
    session: aiohttp.ClientSession,
    openai_client: OpenAIClient,
    claude_client: ClaudeClient,
    video_client=None,          # shared VideoClient; None disables the video fallback
) -> ChannelRuntime:
    """Build one channel. Always returns a ChannelRuntime (so the dashboard runs
    even in a degraded state); raises if the pipeline init genuinely fails so the
    process manager restarts — same contract as the EpicFury block."""
    from agents.x_agent import XAgent
    from agents.website_agent import WebsiteAgent

    slug = ch.slug
    qdrant_config = ch.qdrant or config.qdrant
    redis_keys = ch.redis_keys()
    ingest_run_type = slug
    publish_run_type = f"{slug}_publish"

    state = DashboardState(
        schedule_path=ch.resolved_schedule_path,
        default_filter_score=ch.source.filter_score_threshold,
        default_within_batch=qdrant_config.within_batch_threshold,
        default_cross_batch=qdrant_config.cross_batch_threshold,
        default_video_gen_enabled=ch.source.video_gen.enabled,
        default_video_gen_max_24h=ch.source.video_gen.max_24h,
    )

    prompt_files = [
        ch.resolved_score_prompt.split("/")[-1],
        ch.resolved_post_system_prompt.split("/")[-1],
        ch.resolved_post_user_prompt.split("/")[-1],
        "video_brief.txt",              # shared by every pipeline's video fallback
    ]
    rt = ChannelRuntime(
        slug=slug,
        title=ch.resolved_title,
        dashboard_port=ch.dashboard_port,
        state=state,
        redis_keys=redis_keys,
        qdrant_config=qdrant_config,
        enabled=False,
        prompt_files=prompt_files,
        sources_path=ch.source.sources_md_path,
        run_types=[ingest_run_type, publish_run_type],
    )

    if not ch.enabled:
        logger.info("Channel %s disabled (enabled: false) — pipeline will not start", slug)
        return rt

    # Same gate as EpicFury: the pipeline only starts when publish creds + a
    # (possibly disabled) Telegram bot token are present.
    pipeline_ok = bool(ch.gettr and ch.gettr.user_token and ch.telegram and ch.telegram.bot_token)
    if not pipeline_ok:
        logger.info(
            "Channel %s credentials not set (need gettr.user_token + telegram.bot_token) — "
            "dashboard on :%d will run without pipeline",
            slug, ch.dashboard_port,
        )
        return rt

    try:
        logger.info("Initializing channel %s", slug)
        qdrant = QdrantWrapper(qdrant_config)
        await asyncio.wait_for(qdrant.ensure_collection(), timeout=15)

        similarity = SimilarityAgent(openai_client, qdrant, qdrant_config, state=state)
        similarity.set_thresholds(state.within_batch_threshold, state.cross_batch_threshold)

        gettr = GettrClient(ch.gettr, session)
        gcp = GcpClient(ch.gettr, config.gcp, session)

        x_agent = XAgent(
            ch.source.x, redis, redis_keys, session=session,
            twitterapi_config=ch.source.twitterapi,
            socialdata_config=ch.source.socialdata,
            x_scraper=state.x_scraper,
            keywords=ch.source.keywords or [],
            state=state,
        )
        await x_agent.setup()

        website_agent = WebsiteAgent(ch.source, redis, session, redis_keys)

        channel_claude_config = config.claude.model_copy(
            update={"filter_score_threshold": ch.source.filter_score_threshold}
        )

        publish_agent = PublishAgent(
            redis=redis,
            gcp=gcp,
            gettr=gettr,
            session=session,
            user_agent=config.gcp.user_agent,
            state=state,
            redis_key_overrides=redis_keys,
            pipeline_type="epicfury",              # behaviour mode (shared by all socials channels)
            publish_run_type=publish_run_type,     # but history is isolated per slug
            video_client=video_client,
            video_brand_slug=slug,                 # video/brand/<slug>, falling back to dn
        )
        review_agent = NotionReviewAgent(
            ch.notion_review,
            redis,
            session,
            claude_config=channel_claude_config,
            redis_key_overrides=redis_keys,
            publish_agent=publish_agent,
        )
        review_agent.autopilot = state.autopilot
        pipeline = Pipeline(
            rss_agent=None,
            source_agents=[x_agent, website_agent],
            sources_md_path=ch.source.sources_md_path,
            similarity_agent=similarity,
            claude_client=claude_client,
            claude_config=channel_claude_config,
            notion_client=None,
            review_agent=review_agent,
            state=state,
            video_score_boost=ch.video_score_boost,
            score_prompt_override=ch.resolved_score_prompt,
            post_prompt_override=ch.resolved_post_system_prompt,
            post_user_template_override=ch.resolved_post_user_prompt,
            always_rewrite=True,
            run_type_override=ingest_run_type,
        )

        rt.similarity_agent = similarity
        rt.x_agent = x_agent
        rt.publish_agent = publish_agent
        rt.review_agent = review_agent
        rt.pipeline = pipeline
        rt.enabled = True
        logger.info("Channel %s initialized", slug)
        return rt
    except Exception as exc:
        logger.error("Channel %s init failed: %s — crashing so process manager can restart", slug, exc)
        raise
