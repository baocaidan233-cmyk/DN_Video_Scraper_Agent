"""
aiohttp web Application factory.
Called once from dashboard_loop() in main.py.
"""
from __future__ import annotations

import logging

from aiohttp import web

from dashboard.auth import session_middleware, login_get, login_post, logout
from dashboard.handlers import pages, sse, api, history, health, config_editor

logger = logging.getLogger(__name__)


def make_app(
    state,
    config_holder,
    pipeline,
    publish_agent,
    redis,
    session,
    claude_client=None,
    redis_keys: dict | None = None,   # pipeline-specific Redis key overrides
    qdrant_config=None,               # pipeline-specific Qdrant config for health check
    pipeline_type: str = "rss",       # "rss" | "epicfury" — controls dashboard node graph
    review_agent=None,                # ReviewAgent — for live score threshold updates
    similarity_agent=None,            # SimilarityAgent — for live dedup threshold updates
    gettr_client=None,                # GettrClient — for hot-reload of credentials
    hot_topics_store=None,            # HotTopicsStore — DailyNews only
    gemma_client=None,                # GemmaClient — DailyNews only; content verification
    editor_client=None,               # EditorReviewClient — DailyNews only; A/B editor branch
    gcp_client=None,                  # GcpClient — for hot-reload of credentials
    gettr_test_client=None,           # GettrClient — DailyNews A/B test account
    gcp_test_client=None,             # GcpClient bound to the A/B test account
    x_agent=None,                     # XAgent — EpicFury only; for live scraper switching
    video_client=None,                # VideoClient — for hot-reload of video_brief.txt
    channel_title=None,               # per-channel dashboard title (None → derived from pipeline_type)
    prompt_files=None,                # per-channel prompt filenames (None → hardcoded by pipeline_type)
    sources_path=None,                # per-channel sources .md path (None → hardcoded by pipeline_type)
    run_types=None,                   # per-channel history run_types (None → hardcoded by pipeline_type)
) -> web.Application:
    app = web.Application(middlewares=[session_middleware])

    # Shared objects available to all handlers via request.app[key]
    app["state"] = state
    app["config_holder"] = config_holder
    app["pipeline"] = pipeline
    app["publish_agent"] = publish_agent
    app["redis"] = redis
    app["session"] = session          # aiohttp.ClientSession for health checks
    app["claude_client"] = claude_client
    app["redis_keys"] = redis_keys    # None → handlers fall back to config.redis.*
    app["qdrant_config"] = qdrant_config  # None → handlers fall back to config.qdrant
    app["pipeline_type"] = pipeline_type
    app["review_agent"] = review_agent
    app["similarity_agent"] = similarity_agent
    app["gettr_client"] = gettr_client
    app["hot_topics_store"] = hot_topics_store
    app["gemma_client"] = gemma_client
    app["editor_client"] = editor_client
    app["gcp_client"] = gcp_client
    app["gettr_test_client"] = gettr_test_client
    app["gcp_test_client"] = gcp_test_client
    app["x_agent"] = x_agent
    app["video_client"] = video_client
    # Per-channel dashboard identity (None → legacy behaviour keyed off pipeline_type)
    app["channel_title"] = channel_title
    app["prompt_files"] = prompt_files
    app["sources_path"] = sources_path
    app["run_types"] = run_types

    # ------------------------------------------------------------------ #
    # Routes                                                               #
    # ------------------------------------------------------------------ #
    app.router.add_get("/", pages.index_handler)
    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)

    app.router.add_get("/api/sse", sse.sse_handler)

    app.router.add_post("/api/trigger/{run_type}", api.trigger_handler)
    app.router.add_post("/api/cancel/{run_type}", api.cancel_handler)
    app.router.add_post("/api/schedule", api.schedule_handler)
    app.router.add_post("/api/verify-password", api.verify_password_handler)
    app.router.add_post("/api/reload-config", api.reload_config_handler)
    app.router.add_get("/api/hot-topics", api.hot_topics_get_handler)
    app.router.add_post("/api/hot-topics", api.hot_topics_post_handler)
    app.router.add_get("/api/queue", api.queue_handler)
    app.router.add_post("/api/queue/{article_id}/{action}", api.queue_action_handler)

    app.router.add_get("/api/history", history.history_handler)
    app.router.add_get("/api/health", health.health_handler)

    app.router.add_get("/api/config", config_editor.get_config)
    app.router.add_post("/api/config", config_editor.post_config)
    app.router.add_get("/api/prompts", config_editor.list_prompts)
    app.router.add_get("/api/prompts/{name}", config_editor.get_prompt)
    app.router.add_post("/api/prompts/{name}", config_editor.post_prompt)
    app.router.add_get("/api/sources", config_editor.get_sources)
    app.router.add_post("/api/sources", config_editor.post_sources)

    return app
