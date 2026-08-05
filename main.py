"""
Daily News Agent — main entry point.

Starts concurrent async tasks for two pipelines:

  RSS Pipeline (port 8080):
  1. Telegram bot polling (ReviewAgent)
  2. RSS ingestion loop — runs pipeline.run_ingestion() every 10 minutes
  3. Publish loop — runs publish_agent.run() hourly at :20
  4. Dashboard loop — aiohttp web server on port 8080

  Epic Fury Pipeline (port 8081):
  5. Telegram bot polling (review_ef) — if telegram_epicfury.bot_token is set
  6. Epic Fury ingestion loop — XAgent + WebsiteAgent every 10 minutes
  7. Epic Fury publish loop — hourly
  8. Epic Fury dashboard — port 8081

All tasks run in a single asyncio event loop (no subprocesses, minimal RAM).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal

import aiohttp
import structlog

from core.config import ConfigHolder
from core.redis_client import RedisClient
from agents.rss_agent import RssAgent
from agents.similarity_agent import SimilarityAgent
from agents.notion_review_agent import NotionReviewAgent
from agents.publish_agent import PublishAgent
from core.pipeline import Pipeline
from core.channel_runtime import build_channel
from services.openai_client import OpenAIClient
from services.qdrant_client import QdrantWrapper
from services.claude_client import ClaudeClient
from services.gcp_client import GcpClient
from services.gettr_client import GettrClient
from services.notion_client import NotionClient
from services.gemma_client import GemmaClient
from services.editor_client import EditorReviewClient
from services.video_client import VideoClient
from dashboard.hot_topics import HotTopicsStore


def _setup_logging(log_level: str) -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
    )
    logging.basicConfig(level=logging.getLevelName(log_level.upper()))


async def rss_loop(pipeline: Pipeline, state) -> None:
    """Run ingestion every state.rss_interval_s seconds. asyncio.Lock prevents overlap."""
    lock = asyncio.Lock()
    while True:
        interval = state.rss_interval_s
        if not state.rss_paused and not lock.locked():
            async with lock:
                try:
                    await pipeline.run_ingestion()
                except Exception as e:
                    logging.error("RSS ingestion error: %s", e)

        # Track countdown for dashboard heartbeat (0 when paused)
        for i in range(interval):
            state.next_rss_in_s = 0 if state.rss_paused else (interval - i)
            await asyncio.sleep(1)


async def publish_loop(publish_agent: PublishAgent, state) -> None:
    """Run publish job every state.publish_interval_s seconds.

    Polls every 60 s so that:
    - Articles approved after startup are published within 60 s (not after a full interval sleep).
    - Interval changes made on the dashboard take effect within 60 s.
    - The first publish fires within the first 60 s of startup.
    """
    import time
    lock = asyncio.Lock()
    # Initialise last_run far enough in the past so the first 60-s tick fires.
    last_run = time.monotonic() - state.publish_interval_s - 1

    while True:
        await asyncio.sleep(60)

        interval = state.publish_interval_s
        elapsed = time.monotonic() - last_run
        state.next_publish_in_s = 0 if state.publish_paused else max(0, int(interval - elapsed))

        if not state.publish_paused and not lock.locked() and elapsed >= interval:
            async with lock:
                try:
                    await publish_agent.run()
                    last_run = time.monotonic()
                except Exception as e:
                    logging.error("Publish error: %s", e)


async def _video_quota_used(publish_agent) -> int:
    """AI videos generated in the last rolling 24h, for the dashboard readout.

    Delegates to the publish agent so the ZSET key and window are defined once.
    """
    if publish_agent is None or not hasattr(publish_agent, "video_quota_used"):
        return 0
    try:
        return await publish_agent.video_quota_used()
    except Exception:
        return 0


async def dashboard_loop(
    state,
    config_holder: ConfigHolder,
    pipeline: Pipeline,
    publish_agent: PublishAgent,
    redis: RedisClient,
    session: aiohttp.ClientSession,
    claude_client: ClaudeClient,
    video_client=None,
    redis_keys: dict | None = None,
    qdrant_config=None,
    review_agent=None,
    similarity_agent=None,
    gettr_client=None,
    hot_topics_store=None,
    gemma_client=None,
    editor_client=None,
    gcp_client=None,
    gettr_test_client=None,
    gcp_test_client=None,
) -> None:
    """Start aiohttp web server and send periodic SSE heartbeats."""
    from aiohttp import web
    from dashboard.app import make_app
    from dashboard import db

    config = config_holder.current
    if not config.dashboard.enabled:
        logging.info("Dashboard disabled in config")
        return

    # Ensure session_secret exists
    if not config.dashboard.session_secret:
        # Use ephemeral secret (sessions cleared on restart — acceptable)
        _secret = secrets.token_hex(32)
        logging.info("Dashboard: no session_secret in config, using ephemeral secret")
    else:
        _secret = config.dashboard.session_secret

    app = make_app(
        state=state,
        config_holder=config_holder,
        pipeline=pipeline,
        publish_agent=publish_agent,
        redis=redis,
        session=session,
        claude_client=claude_client,
        video_client=video_client,
        redis_keys=redis_keys,
        qdrant_config=qdrant_config,
        review_agent=review_agent,
        similarity_agent=similarity_agent,
        gettr_client=gettr_client,
        hot_topics_store=hot_topics_store,
        gemma_client=gemma_client,
        editor_client=editor_client,
        gcp_client=gcp_client,
        gettr_test_client=gettr_test_client,
        gcp_test_client=gcp_test_client,
    )

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.dashboard.port)
    await site.start()
    logging.info("Dashboard running on http://0.0.0.0:%d", config.dashboard.port)

    try:
        while True:
            # Update queue lengths from Redis for heartbeat
            try:
                rql = await redis.llen(config_holder.current.redis.review_queue_key)
                pql = await redis.llen(config_holder.current.redis.publish_queue_key)
                state.review_queue_len = rql or 0
                state.publish_queue_len = pql or 0
            except Exception:
                pass

            try:
                await state.emit({
                    "type": "heartbeat",
                    "review_queue_len": state.review_queue_len,
                    "publish_queue_len": state.publish_queue_len,
                    "next_rss_in_s": state.next_rss_in_s,
                    "next_publish_in_s": state.next_publish_in_s,
                    "rss_interval_s": state.rss_interval_s,
                    "publish_interval_s": state.publish_interval_s,
                    "rss_paused": state.rss_paused,
                    "publish_paused": state.publish_paused,
                    "filter_score_threshold": state.filter_score_threshold,
                    "within_batch_threshold": state.within_batch_threshold,
                    "cross_batch_threshold": state.cross_batch_threshold,
                    "autopilot": state.autopilot,
                    "video_gen_enabled": state.video_gen_enabled,
                    "video_gen_max_24h": state.video_gen_max_24h,
                    "video_gen_used_24h": await _video_quota_used(publish_agent),
                    "rss_running": pipeline.is_running,
                    "publish_running": False,
                })
            except Exception as e:
                logging.warning("Dashboard heartbeat emit failed: %s", e)
            await asyncio.sleep(30)
    finally:
        await runner.cleanup()


async def dashboard_loop_ef(
    state,
    config_holder: ConfigHolder,
    pipeline: Pipeline,
    publish_agent: PublishAgent,
    redis: RedisClient,
    session: aiohttp.ClientSession,
    claude_client: ClaudeClient,
    video_client=None,
    port: int = 8081,
    redis_keys: dict | None = None,
    qdrant_config=None,
    pipeline_type: str = "epicfury",
    review_agent=None,
    similarity_agent=None,
    x_agent=None,
    channel_title=None,
    prompt_files=None,
    sources_path=None,
    run_types=None,
) -> None:
    """Epic Fury / generic-channel dashboard on a separate port — reuses make_app.

    The channel_title/prompt_files/sources_path/run_types args are None for the
    hardcoded EpicFury dashboard (legacy behaviour) and set for config-driven
    channels so their title, prompt/source editor and history are correct.
    """
    from aiohttp import web
    from dashboard.app import make_app

    config = config_holder.current
    if not config.dashboard.enabled:
        return

    app = make_app(
        state=state,
        config_holder=config_holder,
        pipeline=pipeline,
        publish_agent=publish_agent,
        redis=redis,
        session=session,
        claude_client=claude_client,
        video_client=video_client,
        redis_keys=redis_keys,
        qdrant_config=qdrant_config,
        pipeline_type=pipeline_type,
        review_agent=review_agent,
        similarity_agent=similarity_agent,
        x_agent=x_agent,
        channel_title=channel_title,
        prompt_files=prompt_files,
        sources_path=sources_path,
        run_types=run_types,
    )

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Dashboard running on http://0.0.0.0:%d", port)

    # Prefer the redis_keys passed in (works for both EpicFury and generic channels);
    # fall back to config.epicfury for the hardcoded EF dashboard.
    ef_cfg = config_holder.current.epicfury
    _review_q = (redis_keys or {}).get("review_queue_key") or (
        ef_cfg.redis_review_queue_key if ef_cfg else "epicfury:review:queue"
    )
    _publish_q = (redis_keys or {}).get("publish_queue_key") or (
        ef_cfg.redis_publish_queue_key if ef_cfg else "epicfury:publish:queue"
    )

    try:
        while True:
            try:
                rql = await redis.llen(_review_q)
                pql = await redis.llen(_publish_q)
                state.review_queue_len = rql or 0
                state.publish_queue_len = pql or 0
            except Exception:
                pass

            try:
                await state.emit({
                    "type": "heartbeat",
                    "review_queue_len": state.review_queue_len,
                    "publish_queue_len": state.publish_queue_len,
                    "next_rss_in_s": state.next_rss_in_s,
                    "next_publish_in_s": state.next_publish_in_s,
                    "rss_interval_s": state.rss_interval_s,
                    "publish_interval_s": state.publish_interval_s,
                    "rss_paused": state.rss_paused,
                    "publish_paused": state.publish_paused,
                    "filter_score_threshold": state.filter_score_threshold,
                    "within_batch_threshold": state.within_batch_threshold,
                    "cross_batch_threshold": state.cross_batch_threshold,
                    "autopilot": state.autopilot,
                    "x_scraper": state.x_scraper,
                    "video_gen_enabled": state.video_gen_enabled,
                    "video_gen_max_24h": state.video_gen_max_24h,
                    "video_gen_used_24h": await _video_quota_used(publish_agent),
                    "rss_running": pipeline.is_running if pipeline else False,
                    "publish_running": False,
                })
            except Exception as e:
                logging.warning("Dashboard heartbeat emit failed: %s", e)
            await asyncio.sleep(30)
    finally:
        await runner.cleanup()


async def main() -> None:
    config_holder = ConfigHolder("config.yaml")
    config = config_holder.current
    _setup_logging(config.app.log_level)
    logger = logging.getLogger(__name__)
    logger.info("Starting Daily News Agent")

    # Dashboard state
    from dashboard.state import DashboardState
    state = DashboardState(
        default_filter_score=config.claude.filter_score_threshold,
        default_within_batch=config.qdrant.within_batch_threshold,
        default_cross_batch=config.qdrant.cross_batch_threshold,
        default_video_gen_enabled=config.video_gen.enabled,
        default_video_gen_max_24h=config.video_gen.max_24h,
    )

    # Shared aiohttp session (connection pooling)
    connector = aiohttp.TCPConnector(limit=30, limit_per_host=5)
    session = aiohttp.ClientSession(connector=connector)

    # Redis
    redis = RedisClient(config.redis)
    await redis.connect()
    logger.info("Redis connected")

    # Services (shared by both pipelines)
    logger.info("Connecting to Qdrant...")
    openai_client = OpenAIClient(config.openai)
    qdrant = QdrantWrapper(config.qdrant)
    try:
        await asyncio.wait_for(qdrant.ensure_collection(), timeout=15)
        logger.info("Qdrant ready")
    except Exception as e:
        logger.warning("Qdrant unavailable at startup: %s — dedup will fail-open until it recovers", e)
    claude_client = ClaudeClient(config.claude, session, openai_client=openai_client, metadata_config=config.metadata_api)
    gcp_client = GcpClient(config.gettr, config.gcp, session)          # DailyNews CDN uploads
    gettr_client = GettrClient(config.gettr, session)
    notion_client = NotionClient(config.notion, session)

    # DailyNews A/B comparison account — editor-revised variants post here (optional)
    gettr_test_client: GettrClient | None = None
    gcp_test_client: GcpClient | None = None
    if config.gettr_test and config.gettr_test.user_token:
        gettr_test_client = GettrClient(config.gettr_test, session)
        gcp_test_client = GcpClient(config.gettr_test, config.gcp, session)

    # Hot Topics store (DailyNews only — controls single-card-per-run article selection)
    hot_topics_store = HotTopicsStore()

    # Gemma verification client (DailyNews only — anti-CCP content quality check)
    gemma_client: GemmaClient | None = None
    if config.gemma.enabled:
        if config.gemma.api_key:
            gemma_client = GemmaClient(config.gemma)
            logger.info("Gemma content verification client initialized (model: %s)", config.gemma.model)
        else:
            logger.warning(
                "Gemma verification is enabled but no api_key is set — "
                "add gemma.api_key to config.yaml to enable content verification"
            )

    # Editor review branch (DailyNews only — A/B variant against the standard post)
    editor_client = EditorReviewClient(config.editor_review, config.claude, openai_client=openai_client)
    logger.info(
        "Editor A/B branch: review=%s, test account=%s",
        "ON" if editor_client.enabled else "OFF",
        config.gettr_test.user_id if gettr_test_client else "not configured",
    )
    if editor_client.enabled and not gettr_test_client:
        logger.warning(
            "editor_review is enabled but gettr_test is not configured — "
            "variants will be generated but never posted"
        )

    logger.info("All services initialized")

    # --- RSS Pipeline ---
    rss_agent = RssAgent(config.rss, redis, session)
    similarity_agent = SimilarityAgent(
        openai_client, qdrant, config.qdrant,
        state=state,
    )
    # AI short-video fallback for posts with no usable image. One client shared by
    # every pipeline — VideoClient serialises renders internally (Semaphore(1)),
    # which is what keeps two ffmpeg runs off this 2-vCPU box at once.
    video_client = VideoClient(
        openai_client,
        timeout_s=config.video_gen.timeout_s,
        width=config.video_gen.width,
        height=config.video_gen.height,
        brief_model=config.video_gen.brief_model or None,
    )

    publish_agent = PublishAgent(
        redis=redis,
        gcp=gcp_client,
        gettr=gettr_client,
        session=session,
        user_agent=config.gcp.user_agent,
        state=state,
        metadata_api_config=config.metadata_api,
        gettr_test=gettr_test_client,
        gcp_test=gcp_test_client,
        video_client=video_client,
        video_brand_slug="dn",
    )

    # Topical dedup checker — DailyNews pipeline only (requires notion_dedup config)
    _topical_dedup = None
    if config.notion_dedup and config.notion_dedup.api_key:
        from services.notion_topical_dedup import NotionTopicalDedupChecker
        _topical_dedup = NotionTopicalDedupChecker(
            session=session,
            daily_news_api_key=config.notion_dedup.api_key,
            daily_news_db_id=config.notion_dedup.article_database_id,
            openai=openai_client,
            similarity_threshold=config.notion_dedup.similarity_threshold,
            recent_lookback_hours=config.notion_dedup.recent_lookback_hours,
            enforce_recent_skip=config.notion_dedup.enforce_recent_skip,
        )
        publish_agent._topical_dedup = _topical_dedup
        logger.info(
            "Notion topical dedup checker initialized (enforce_recent_skip=%s)",
            config.notion_dedup.enforce_recent_skip,
        )

    review_agent = NotionReviewAgent(
        config.notion_review, redis, session, claude_config=config.claude,
        publish_agent=publish_agent,
    )
    review_agent.autopilot = state.autopilot  # restore persisted autopilot state
    publish_agent._on_posted = review_agent.update_posted_page
    publish_agent._on_dropped = review_agent.update_dropped_page
    publish_agent._notion_fallback = review_agent.get_fallback_articles
    # Sync persisted thresholds from schedule.json into live agents (overrides config.yaml defaults)
    similarity_agent.set_thresholds(state.within_batch_threshold, state.cross_batch_threshold)
    review_agent._min_score = state.filter_score_threshold
    pipeline = Pipeline(
        rss_agent=rss_agent,
        similarity_agent=similarity_agent,
        claude_client=claude_client,
        claude_config=config.claude,
        notion_client=notion_client,
        review_agent=review_agent,
        state=state,
        hot_topics_store=hot_topics_store,
        always_rewrite=True,
        gemma_client=gemma_client,
        editor_client=editor_client,
    )
    pipeline.set_filter_score_threshold(state.filter_score_threshold)

    # --- Epic Fury Pipeline (optional) ---
    # state_ef and dashboard always start when config.epicfury is present.
    # The full pipeline (Qdrant, XAgent, Telegram bot) only starts if all
    # credentials are set. Failures during pipeline init are logged and the
    # dashboard still runs on port 8081 in a degraded state.

    state_ef = None
    pipeline_ef = None
    publish_ef = None
    review_ef = None
    ef_enabled = False          # full pipeline ready
    ef_redis_keys = None
    qdrant_ef_config = None

    if config.epicfury:
        state_ef = DashboardState(
            schedule_path="data/schedule_ef.json",
            default_filter_score=config.epicfury.filter_score_threshold,
            default_within_batch=(config.qdrant_epicfury or config.qdrant).within_batch_threshold,
            default_cross_batch=(config.qdrant_epicfury or config.qdrant).cross_batch_threshold,
            default_video_gen_enabled=config.epicfury.video_gen.enabled,
            default_video_gen_max_24h=config.epicfury.video_gen.max_24h,
        )
        ef_cfg = config.epicfury
        qdrant_ef_config = config.qdrant_epicfury or config.qdrant
        ef_redis_keys = {
            "url_hash_key_prefix":   ef_cfg.redis_url_hash_prefix,
            "url_hash_ttl_s":        ef_cfg.redis_url_hash_ttl_s,
            "review_pending_prefix": ef_cfg.redis_review_pending_prefix,
            "review_queue_key":      ef_cfg.redis_review_queue_key,
            "publish_queue_key":     ef_cfg.redis_publish_queue_key,
            "post_hash_key_prefix":  ef_cfg.redis_post_hash_prefix,
            "post_hash_ttl_s":       ef_cfg.redis_post_hash_ttl_s,
            "tg_msg_prefix":         ef_cfg.redis_tg_msg_prefix,
        }

        _ef_pipeline_ok = bool(
            config.gettr_epicfury
            and config.telegram_epicfury
            and config.telegram_epicfury.bot_token
        )

        if _ef_pipeline_ok:
            try:
                from agents.x_agent import XAgent
                from agents.website_agent import WebsiteAgent

                logger.info("Initializing Epic Fury pipeline")
                qdrant_ef = QdrantWrapper(qdrant_ef_config)
                await asyncio.wait_for(qdrant_ef.ensure_collection(), timeout=15)

                similarity_ef = SimilarityAgent(openai_client, qdrant_ef, qdrant_ef_config, state=state_ef)
                similarity_ef.set_thresholds(state_ef.within_batch_threshold, state_ef.cross_batch_threshold)
                gettr_ef = GettrClient(config.gettr_epicfury, session)
                gcp_client_ef = GcpClient(config.gettr_epicfury, config.gcp, session)

                x_agent = XAgent(
                    config.epicfury.x, redis, ef_redis_keys, session=session,
                    twitterapi_config=config.epicfury.twitterapi,
                    socialdata_config=config.epicfury.socialdata,
                    x_scraper=state_ef.x_scraper,  # restore persisted choice
                    keywords=config.epicfury.keywords or [],
                    state=state_ef,
                )
                await x_agent.setup()

                website_agent = WebsiteAgent(config.epicfury, redis, session, ef_redis_keys)

                # EF uses its own score threshold (5.0 vs DailyNews 6.0)
                ef_claude_config = config.claude.model_copy(
                    update={"filter_score_threshold": config.epicfury.filter_score_threshold}
                )

                publish_ef = PublishAgent(
                    redis=redis,
                    gcp=gcp_client_ef,
                    gettr=gettr_ef,
                    session=session,
                    user_agent=config.gcp.user_agent,
                    state=state_ef,
                    redis_key_overrides=ef_redis_keys,
                    pipeline_type="epicfury",
                    video_client=video_client,
                    video_brand_slug="ef",
                )
                review_ef = NotionReviewAgent(
                    config.notion_review_epicfury,
                    redis,
                    session,
                    claude_config=ef_claude_config,
                    redis_key_overrides=ef_redis_keys,
                    publish_agent=publish_ef,
                )
                review_ef.autopilot = state_ef.autopilot  # restore persisted autopilot state
                pipeline_ef = Pipeline(
                    rss_agent=None,
                    source_agents=[x_agent, website_agent],
                    sources_md_path=config.epicfury.sources_md_path,
                    similarity_agent=similarity_ef,
                    claude_client=claude_client,
                    claude_config=ef_claude_config,
                    notion_client=None,
                    review_agent=review_ef,
                    state=state_ef,
                    video_score_boost=1.0,
                    score_prompt_override="prompts/epicfury_score_articles.txt",
                    post_prompt_override="prompts/epicfury_generate_post_system.txt",
                    post_user_template_override="prompts/epicfury_generate_post_user.txt",
                    always_rewrite=True,
                )
                ef_enabled = True
                logger.info("Epic Fury pipeline initialized")
            except Exception as _ef_exc:
                logger.error(
                    "Epic Fury pipeline init failed: %s — crashing so process manager can restart",
                    _ef_exc,
                )
                raise
        else:
            logger.info(
                "Epic Fury pipeline credentials not set — dashboard on :%d will run without pipeline",
                config.dashboard.port2,
            )
    else:
        logger.info("Epic Fury disabled (no epicfury config section)")

    # --- Config-driven extra channels (fully additive; no code changes to add one) ---
    channel_runtimes = []
    for _ch_cfg in config.channels:
        _rt = await build_channel(
            config, _ch_cfg,
            redis=redis, session=session,
            openai_client=openai_client, claude_client=claude_client,
            video_client=video_client,
        )
        channel_runtimes.append(_rt)

    async def shutdown(sig: signal.Signals) -> None:
        logger.info("Shutting down (signal %s)...", sig.name)
        await session.close()
        await redis.close()

    def _reload_config() -> None:
        try:
            config_holder.reload()
            _cfg = config_holder.current
            gettr_client.reload_config(_cfg.gettr)
            gcp_client.reload_config(_cfg.gettr)
            editor_client.reload_config(_cfg.editor_review)
            if gettr_test_client and _cfg.gettr_test:
                gettr_test_client.reload_config(_cfg.gettr_test)
                gcp_test_client.reload_config(_cfg.gettr_test)
            logger.info("Config reloaded via SIGUSR1")
        except Exception as e:
            logger.error("Config reload failed: %s", e)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda s=sig: asyncio.create_task(shutdown(s))
        )
    try:
        loop.add_signal_handler(signal.SIGUSR1, _reload_config)
    except (AttributeError, OSError):
        pass  # SIGUSR1 not available on all platforms

    # Route all log records (INFO+ for own modules, ERROR+ for third-party) to dashboard Logs tab
    from dashboard.state import DashboardLogHandler
    _states_for_log = [state]
    if state_ef is not None:
        _states_for_log.append(state_ef)
    _states_for_log.extend(rt.state for rt in channel_runtimes)
    _log_handler = DashboardLogHandler(_states_for_log)
    _log_handler._loop = loop
    logging.getLogger().addHandler(_log_handler)

    logger.info("All components initialized — starting loops")

    # Initialize SQLite history DB once, before any pipeline loop can call db.save_run_start()
    from dashboard import db as _db
    await _db.init_db()

    # Build gather tasks
    gather_tasks = [
        review_agent.start_card_sender(),
        rss_loop(pipeline, state),
        publish_loop(publish_agent, state),
        dashboard_loop(
            state=state,
            config_holder=config_holder,
            pipeline=pipeline,
            publish_agent=publish_agent,
            redis=redis,
            session=session,
            claude_client=claude_client,
            video_client=video_client,
            redis_keys=None,               # DailyNews: use config.redis.* directly
            qdrant_config=config.qdrant,   # DailyNews Qdrant
            review_agent=review_agent,
            similarity_agent=similarity_agent,
            gettr_client=gettr_client,
            hot_topics_store=hot_topics_store,
            gemma_client=gemma_client,
            editor_client=editor_client,
            gcp_client=gcp_client,
            gettr_test_client=gettr_test_client,
            gcp_test_client=gcp_test_client,
        ),
    ]

    # DailyNews-only: periodically cross-check not-yet-sent Notion cards against Gettr's
    # own recent posts, catching duplicates that never had a Notion candidate to diff
    # against in PublishAgent's before_publish/after_publish hooks.
    if _topical_dedup is not None:
        from services.gettr_feed_client import GettrFeedClient
        gather_tasks.append(
            _topical_dedup.run_gettr_crosscheck_loop(
                GettrFeedClient(session, config.notion_dedup.gettr_handle),
                interval_minutes=config.notion_dedup.gettr_crosscheck_interval_minutes,
            )
        )

    # Always start EF dashboard if state_ef was created (config.epicfury is set)
    if state_ef is not None:
        gather_tasks.append(
            dashboard_loop_ef(
                state=state_ef,
                config_holder=config_holder,
                pipeline=pipeline_ef,       # may be None — dashboard handles that
                publish_agent=publish_ef,   # may be None
                redis=redis,
                session=session,
                claude_client=claude_client,
            video_client=video_client,
                port=config.dashboard.port2,
                redis_keys=ef_redis_keys,
                qdrant_config=qdrant_ef_config,
                pipeline_type="epicfury",
                review_agent=review_ef,
                similarity_agent=similarity_ef if ef_enabled else None,
                x_agent=x_agent if ef_enabled else None,
            )
        )

    # Only add pipeline loops if EF pipeline fully initialized
    if ef_enabled:
        gather_tasks += [
            review_ef.start_card_sender(),
            rss_loop(pipeline_ef, state_ef),
            publish_loop(publish_ef, state_ef),
        ]

    # Config-driven channels: dashboard always starts; pipeline loops only if enabled.
    for _rt in channel_runtimes:
        gather_tasks.append(
            dashboard_loop_ef(
                state=_rt.state,
                config_holder=config_holder,
                pipeline=_rt.pipeline,          # may be None — dashboard handles that
                publish_agent=_rt.publish_agent,
                redis=redis,
                session=session,
                claude_client=claude_client,
            video_client=video_client,
                port=_rt.dashboard_port,
                redis_keys=_rt.redis_keys,
                qdrant_config=_rt.qdrant_config,
                pipeline_type="epicfury",
                review_agent=_rt.review_agent,
                similarity_agent=_rt.similarity_agent,
                x_agent=_rt.x_agent,
                channel_title=_rt.title,
                prompt_files=_rt.prompt_files,
                sources_path=_rt.sources_path,
                run_types=_rt.run_types,
            )
        )
        if _rt.enabled:
            gather_tasks += [
                _rt.review_agent.start_card_sender(),
                rss_loop(_rt.pipeline, _rt.state),
                publish_loop(_rt.publish_agent, _rt.state),
            ]

    await asyncio.gather(*gather_tasks)


if __name__ == "__main__":
    asyncio.run(main())
