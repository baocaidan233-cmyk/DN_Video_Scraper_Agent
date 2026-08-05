"""
Server-Sent Events endpoint.
GET /api/sse — streams JSON events to browser.
Browser uses EventSource which auto-reconnects.
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30  # seconds


async def sse_handler(request: web.Request) -> web.StreamResponse:
    state = request.app["state"]

    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    await response.prepare(request)

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    state.sse_subscribers.append(queue)

    async def _video_gen_used_24h(req) -> int:
        """Videos generated in the last rolling 24h, read from the publish agent
        so the ZSET key and window stay defined in exactly one place."""
        agent = req.app.get("publish_agent")
        if agent is None or not hasattr(agent, "video_quota_used"):
            return 0
        try:
            return await agent.video_quota_used()
        except Exception:
            return 0

    # Send immediate state snapshot so the browser initialises the form right away
    # (without this, the form shows hard-coded JS defaults for up to 30 s until the
    # next heartbeat from dashboard_loop fires).
    try:
        pipeline = request.app.get("pipeline")
        snapshot = {
            "type": "heartbeat",
            "review_queue_len":       state.review_queue_len,
            "publish_queue_len":      state.publish_queue_len,
            "next_rss_in_s":          state.next_rss_in_s,
            "next_publish_in_s":      state.next_publish_in_s,
            "rss_interval_s":         state.rss_interval_s,
            "publish_interval_s":     state.publish_interval_s,
            "rss_paused":             state.rss_paused,
            "publish_paused":         state.publish_paused,
            "filter_score_threshold": state.filter_score_threshold,
            "within_batch_threshold": state.within_batch_threshold,
            "cross_batch_threshold":  state.cross_batch_threshold,
            "notion_dedup_threshold": state.notion_dedup_threshold,
            "autopilot":              state.autopilot,
            "verify_enabled":         state.verify_enabled,
            "x_scraper":              state.x_scraper if hasattr(state, "x_scraper") else "twitterapi",
            "video_gen_enabled":      state.video_gen_enabled,
            "video_gen_max_24h":      state.video_gen_max_24h,
            "video_gen_used_24h":     await _video_gen_used_24h(request),
            "rss_running":            pipeline.is_running if pipeline else False,
            "publish_running":        False,
        }
        await response.write(f"data: {json.dumps(snapshot)}\n\n".encode())
    except Exception:
        pass  # Never let snapshot failure break the SSE connection

    # Send current log ring buffer on connect so the browser gets recent history
    for log_event in list(state.log_ring):
        try:
            await response.write(f"data: {json.dumps(log_event)}\n\n".encode())
        except Exception:
            break

    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                await response.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                # Keep-alive comment
                try:
                    await response.write(b": ping\n\n")
                except Exception:
                    break
            except (ConnectionResetError, asyncio.CancelledError):
                break
    finally:
        try:
            state.sse_subscribers.remove(queue)
        except ValueError:
            pass

    return response
