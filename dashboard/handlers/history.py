"""GET /api/history — paginated run history from SQLite."""
from __future__ import annotations

from aiohttp import web

from dashboard import db


_PIPELINE_RUN_TYPES: dict[str, list[str]] = {
    "rss":      ["rss", "publish"],
    "epicfury": ["epicfury", "ef_publish"],
}


async def history_handler(request: web.Request) -> web.Response:
    limit = min(int(request.rel_url.query.get("limit", 50)), 200)
    page = max(0, int(request.rel_url.query.get("page", 0)))
    offset = page * limit
    # Per-channel dashboards pass explicit run_types; legacy dashboards key off pipeline_type.
    run_types = request.app.get("run_types") or _PIPELINE_RUN_TYPES.get(
        request.app.get("pipeline_type", "rss")
    )
    result = await db.get_history(limit=limit, offset=offset, run_types=run_types)
    result["page"] = page
    result["limit"] = limit
    return web.json_response(result)
