"""Serve the single-page dashboard application."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "index.html"


async def index_handler(request: web.Request) -> web.Response:
    pipeline_type = request.app.get("pipeline_type", "rss")
    channel_title = request.app.get("channel_title")
    if channel_title:
        header = f"📡 {channel_title}"
        tab_title = channel_title
    else:
        header = "⚔️ Epic Fury Live" if pipeline_type == "epicfury" else "📰 Daily News"
        tab_title = "Epic Fury Live" if pipeline_type == "epicfury" else "Daily News"
    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("__PIPELINE_TYPE__", pipeline_type)
    html = html.replace("__PIPELINE_TITLE__", tab_title)
    html = html.replace("__PIPELINE_HEADER__", header)
    return web.Response(text=html, content_type="text/html")
