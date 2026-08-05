"""
Core API endpoints:
  POST /api/trigger/{rss,publish}  — manual pipeline trigger
  POST /api/schedule               — update RSS interval / publish minute / rss_paused
  POST /api/verify-password        — check password without issuing a new session
  GET  /api/queue                  — review queue inspector
  POST /api/queue/{id}/approve     — approve from browser
  POST /api/queue/{id}/reject      — reject from browser
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from aiohttp import web
from dashboard.auth import verify_password
from dashboard.handlers import config_editor

logger = logging.getLogger(__name__)


async def trigger_handler(request: web.Request) -> web.Response:
    run_type = request.match_info["run_type"]
    if run_type not in ("rss", "publish"):
        raise web.HTTPBadRequest(reason="run_type must be 'rss' or 'publish'")

    state = request.app["state"]
    pipeline = request.app.get("pipeline")
    publish_agent = request.app.get("publish_agent")

    # Reject concurrent runs
    if run_type == "rss" and pipeline:
        if pipeline.is_running:
            return web.json_response({"error": "RSS pipeline already running"}, status=409)
        run_id = f"manual-rss-{uuid.uuid4().hex[:8]}"
        asyncio.create_task(pipeline.run_ingestion(run_id=run_id))
    elif run_type == "publish" and publish_agent:
        if publish_agent.is_running:
            return web.json_response({"error": "Publish agent already running"}, status=409)
        run_id = f"manual-publish-{uuid.uuid4().hex[:8]}"

        async def _run_publish():
            try:
                await publish_agent.run(run_id=run_id)
            except Exception as e:
                logger.error("Manual publish error: %s", e)

        asyncio.create_task(_run_publish())
    else:
        raise web.HTTPServiceUnavailable(reason="Agent not available")

    return web.json_response({"run_id": run_id, "status": "triggered"})


async def schedule_handler(request: web.Request) -> web.Response:
    state = request.app["state"]
    body = await request.json()

    if "rss_interval_s" in body:
        val = int(body["rss_interval_s"])
        if val < 60:
            raise web.HTTPBadRequest(reason="rss_interval_s must be >= 60")
        state.rss_interval_s = val

    if "publish_interval_s" in body:
        val = int(body["publish_interval_s"])
        if val < 60:
            raise web.HTTPBadRequest(reason="publish_interval_s must be >= 60")
        state.publish_interval_s = val

    if "rss_paused" in body:
        state.rss_paused = bool(body["rss_paused"])

    if "publish_paused" in body:
        state.publish_paused = bool(body["publish_paused"])

    if "filter_score_threshold" in body:
        val = float(body["filter_score_threshold"])
        if not (0.0 <= val <= 10.0):
            raise web.HTTPBadRequest(reason="filter_score_threshold must be between 0 and 10")
        state.filter_score_threshold = val
        pipeline = request.app.get("pipeline")
        review_agent = request.app.get("review_agent")
        if pipeline is not None and hasattr(pipeline, "set_filter_score_threshold"):
            pipeline.set_filter_score_threshold(val)
        if review_agent is not None:
            review_agent._min_score = val

    _sim_update_needed = False
    if "within_batch_threshold" in body:
        val = float(body["within_batch_threshold"])
        if not (0.0 < val < 1.0):
            raise web.HTTPBadRequest(reason="within_batch_threshold must be between 0 and 1")
        state.within_batch_threshold = val
        _sim_update_needed = True

    if "cross_batch_threshold" in body:
        val = float(body["cross_batch_threshold"])
        if not (0.0 < val < 1.0):
            raise web.HTTPBadRequest(reason="cross_batch_threshold must be between 0 and 1")
        state.cross_batch_threshold = val
        _sim_update_needed = True

    if "notion_dedup_threshold" in body:
        val = float(body["notion_dedup_threshold"])
        if not (0.0 < val < 1.0):
            raise web.HTTPBadRequest(reason="notion_dedup_threshold must be between 0 and 1")
        state.notion_dedup_threshold = val
        sim = request.app.get("similarity_agent")
        if sim is not None:
            sim._notion_dedup_threshold = val
        _sim_update_needed = True

    if _sim_update_needed:
        sim = request.app.get("similarity_agent")
        if sim is not None and hasattr(sim, "set_thresholds"):
            sim.set_thresholds(state.within_batch_threshold, state.cross_batch_threshold)

    if "autopilot" in body:
        state.autopilot = bool(body["autopilot"])
        review_agent = request.app.get("review_agent")
        if review_agent is not None:
            review_agent.autopilot = state.autopilot
        logger.info("Auto-pilot %s", "ON" if state.autopilot else "OFF")

    if "verify_enabled" in body:
        state.verify_enabled = bool(body["verify_enabled"])
        logger.info("Verify step %s", "ON" if state.verify_enabled else "OFF")

    if "x_scraper" in body:
        scraper = str(body["x_scraper"])
        if scraper not in ("twitterapi", "socialdata"):
            raise web.HTTPBadRequest(reason="x_scraper must be 'twitterapi' or 'socialdata'")
        state.x_scraper = scraper
        x_agent = request.app.get("x_agent")
        if x_agent is not None and hasattr(x_agent, "set_scraper"):
            x_agent.set_scraper(scraper)
        logger.info("X scraper switched to %s", scraper)

    if "video_gen_enabled" in body:
        state.video_gen_enabled = bool(body["video_gen_enabled"])
        logger.info("AI video fallback %s", "ON" if state.video_gen_enabled else "OFF")

    if "video_gen_max_24h" in body:
        val = int(body["video_gen_max_24h"])
        if not (0 <= val <= 100):
            raise web.HTTPBadRequest(reason="video_gen_max_24h must be between 0 and 100")
        state.video_gen_max_24h = val
        logger.info("AI video cap set to %d per 24h%s", val, " (0 = off)" if val == 0 else "")

    state.save_schedule()
    await state.emit({
        "type": "schedule_update",
        "rss_interval_s": state.rss_interval_s,
        "publish_interval_s": state.publish_interval_s,
        "rss_paused": state.rss_paused,
        "publish_paused": state.publish_paused,
        "filter_score_threshold": state.filter_score_threshold,
        "within_batch_threshold": state.within_batch_threshold,
        "cross_batch_threshold": state.cross_batch_threshold,
        "notion_dedup_threshold": state.notion_dedup_threshold,
        "autopilot": state.autopilot,
        "verify_enabled": state.verify_enabled,
        "x_scraper": state.x_scraper,
        "video_gen_enabled": state.video_gen_enabled,
        "video_gen_max_24h": state.video_gen_max_24h,
    })
    return web.json_response({"ok": True})


async def cancel_handler(request: web.Request) -> web.Response:
    run_type = request.match_info["run_type"]
    state = request.app["state"]
    pipeline = request.app.get("pipeline")
    publish_agent = request.app.get("publish_agent")

    if run_type == "rss" and pipeline:
        if not pipeline.is_running:
            return web.json_response({"ok": False, "error": "No RSS run in progress"}, status=409)
        state.rss_cancel_event.set()
        logger.info("RSS run cancellation requested")
    elif run_type == "publish" and publish_agent:
        state.publish_cancel_event.set()
        logger.info("Publish run cancellation requested")
    else:
        raise web.HTTPBadRequest(reason="run_type must be 'rss' or 'publish'")

    return web.json_response({"ok": True})


async def verify_password_handler(request: web.Request) -> web.Response:
    """Re-verify the dashboard password (used to unlock config/prompt editing)."""
    body = await request.json()
    password = body.get("password", "")
    config = request.app["config_holder"].current
    if verify_password(password, config.dashboard.password_hash):
        return web.json_response({"ok": True})
    return web.json_response({"ok": False}, status=401)


async def hot_topics_get_handler(request: web.Request) -> web.Response:
    """GET /api/hot-topics — return current keyword list and semantic threshold."""
    store = request.app.get("hot_topics_store")
    keywords = store.get_keywords() if store else []
    threshold = store.get_semantic_threshold() if store else 0.75
    return web.json_response({"keywords": keywords, "semantic_threshold": threshold})


async def hot_topics_post_handler(request: web.Request) -> web.Response:
    """POST /api/hot-topics — save keyword list and optional semantic threshold."""
    body = await request.json()
    store = request.app.get("hot_topics_store")

    if "keywords" in body:
        keywords = body["keywords"]
        if not isinstance(keywords, list):
            raise web.HTTPBadRequest(reason="keywords must be a list")
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
        if store:
            try:
                store.set_keywords(keywords)
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)
        logger.info("Hot topics updated: %d keywords", len(keywords))

    if "semantic_threshold" in body:
        val = float(body["semantic_threshold"])
        if not (0.0 < val < 1.0):
            raise web.HTTPBadRequest(reason="semantic_threshold must be between 0 and 1")
        if store:
            store.set_semantic_threshold(val)

    count = len(store.get_keywords()) if store else 0
    return web.json_response({"ok": True, "count": count})


async def reload_config_handler(request: web.Request) -> web.Response:
    """Re-read config.yaml from disk without restarting the process."""
    config_holder = request.app["config_holder"]
    state = request.app["state"]

    try:
        config_holder.reload()
    except Exception as e:
        logger.error("Config reload failed: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=422)

    # Propagate credential changes to live clients
    config_editor._reload_live_clients(request, config_holder)

    await state.emit({"type": "config_reloaded", "detail": "manual reload"})
    logger.info("Config reloaded from disk via dashboard")
    return web.json_response({"ok": True})


def _redis_key(request: web.Request, key: str) -> str:
    """Resolve a Redis key name from pipeline-specific overrides or config fallback."""
    overrides = request.app.get("redis_keys") or {}
    if key in overrides:
        return overrides[key]
    config = request.app["config_holder"].current
    return getattr(config.redis, key)


async def queue_handler(request: web.Request) -> web.Response:
    """Return current articles in the review queue."""
    redis = request.app["redis"]
    prefix = _redis_key(request, "review_pending_prefix")
    queue_key = _redis_key(request, "review_queue_key")

    try:
        article_ids = await redis.lrange(queue_key, 0, 49)  # max 50
        items = []
        for aid in article_ids:
            try:
                data = await redis.hgetall(f"{prefix}{aid}")
                if data:
                    items.append({"article_id": aid, **data})
            except Exception:
                pass
        return web.json_response(items)
    except Exception as e:
        raise web.HTTPServiceUnavailable(reason=f"Redis error: {e}")


async def queue_action_handler(request: web.Request) -> web.Response:
    """Approve or reject an article from the browser."""
    article_id = request.match_info["article_id"]
    action = request.match_info["action"]  # approve | reject

    redis = request.app["redis"]
    pending_key = f"{_redis_key(request, 'review_pending_prefix')}{article_id}"

    data = await redis.hgetall(pending_key)
    if not data:
        raise web.HTTPNotFound(reason="Article not found in queue")

    if action == "approve":
        await redis.lpush(_redis_key(request, "publish_queue_key"), article_id)
        await redis.delete(pending_key)
        logger.info("Dashboard approved article %s", article_id)
    elif action == "reject":
        await redis.delete(pending_key)
        logger.info("Dashboard rejected article %s", article_id)
    else:
        raise web.HTTPBadRequest(reason="action must be 'approve' or 'reject'")

    return web.json_response({"ok": True})
