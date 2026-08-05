"""
GET /api/health — concurrent health checks for all downstream services.
Returns latency_ms and status for each service.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)


async def _check_redis(redis, timeout: float = 5.0) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(redis.client.ping(), timeout=timeout)
        return {"status": "ok", "latency_ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"status": "error", "error": str(e), "latency_ms": round((time.monotonic() - t0) * 1000)}


async def _check_http(session: aiohttp.ClientSession, url: str, timeout: float = 5.0) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            return {
                "status": "ok" if resp.status < 500 else "error",
                "http_status": resp.status,
                "latency_ms": round((time.monotonic() - t0) * 1000),
            }
    except Exception as e:
        return {"status": "error", "error": str(e)[:100], "latency_ms": round((time.monotonic() - t0) * 1000)}


async def _check_notion(session, api_key: str, timeout: float = 5.0) -> dict[str, Any]:
    url = "https://api.notion.com/v1/users/me"
    headers = {"Authorization": f"Bearer {api_key}", "Notion-Version": "2022-06-28"}
    t0 = time.monotonic()
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            ok = resp.status in (200, 401)  # 401 = wrong key but API reachable
            return {
                "status": "ok" if ok else "error",
                "http_status": resp.status,
                "latency_ms": round((time.monotonic() - t0) * 1000),
            }
    except Exception as e:
        return {"status": "error", "error": str(e)[:100], "latency_ms": round((time.monotonic() - t0) * 1000)}


async def health_handler(request: web.Request) -> web.Response:
    config = request.app["config_holder"].current
    redis = request.app["redis"]
    session = request.app["session"]
    # Use pipeline-specific Qdrant config if provided (Epic Fury dashboard),
    # otherwise fall back to the default DailyNews Qdrant config.
    qdrant_cfg = request.app.get("qdrant_config") or config.qdrant

    checks = await asyncio.gather(
        _check_redis(redis),
        _check_http(session, f"{qdrant_cfg.url}/readyz"),
        _check_notion(session, config.notion.api_key),
        _check_http(session, "https://api.openai.com/v1/models"),
        _check_http(session, "https://api.anthropic.com/v1/models"),
        _check_http(session, "https://gettr.com/"),
        return_exceptions=False,
    )

    labels = ["redis", "qdrant", "notion", "openai", "claude", "gettr"]
    result = {label: check for label, check in zip(labels, checks)}
    overall = "ok" if all(r.get("status") == "ok" for r in result.values()) else "degraded"
    result["overall"] = overall
    return web.json_response(result)
