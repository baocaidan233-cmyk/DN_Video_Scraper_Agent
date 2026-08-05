"""
Notion client for fetching RSS feed sources.
Replicates the 'Get many database pages' node in V1.1_daily_news_rss prompt v1.json.

Queries the RSS source database filtered by in_use == true.
Returns list of RssSource (url, cookie, name) sorted by name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from core.config import NotionConfig

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


@dataclass
class RssSource:
    url: str
    name: str
    cookie: Optional[str] = None


class NotionClient:
    def __init__(self, config: NotionConfig, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def get_rss_sources(self) -> list[RssSource]:
        """
        Fetch all RSS feed sources with in_use == true, sorted by name.
        Replicates the Notion 'Get many database pages' node with in_use filter.
        """
        sources: list[RssSource] = []
        cursor: Optional[str] = None

        while True:
            body: dict = {
                "filter": {
                    "property": "in_use",
                    "checkbox": {"equals": True},
                },
                "sorts": [{"property": "Name", "direction": "ascending"}],
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor

            try:
                async with self._session.post(
                    f"{NOTION_API_BASE}/databases/{self._config.rss_database_id}/query",
                    headers=self._headers,
                    json=body,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as e:
                logger.error("Notion RSS database query failed: %s", e)
                break

            for page in data.get("results", []):
                source = self._parse_page(page)
                if source:
                    sources.append(source)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        logger.info("Notion: fetched %d active RSS sources", len(sources))
        return sources

    def _parse_page(self, page: dict) -> Optional[RssSource]:
        """Extract RSS URL, cookie, name from a Notion page."""
        props = page.get("properties", {})

        # RSS URL — field named 'RSS' (url type)
        rss_url = self._get_url(props, "RSS") or self._get_url(props, "property_rss") or self._get_url(props, "rss")
        if not rss_url:
            return None

        # Name — title property
        name = self._get_title(props, "Name") or self._get_title(props, "name") or rss_url

        # Cookie — rich_text or url property named 'cookie'
        cookie = (
            self._get_rich_text(props, "cookie")
            or self._get_url(props, "cookie")
            or self._get_rich_text(props, "property_cookie")
        )

        return RssSource(url=rss_url, name=name, cookie=cookie or None)

    @staticmethod
    def _get_url(props: dict, key: str) -> Optional[str]:
        p = props.get(key, {})
        return p.get("url") if p.get("type") == "url" else None

    @staticmethod
    def _get_title(props: dict, key: str) -> Optional[str]:
        p = props.get(key, {})
        if p.get("type") == "title":
            parts = p.get("title", [])
            return "".join(t.get("plain_text", "") for t in parts).strip() or None
        return None

    @staticmethod
    def _get_rich_text(props: dict, key: str) -> Optional[str]:
        p = props.get(key, {})
        if p.get("type") == "rich_text":
            parts = p.get("rich_text", [])
            return "".join(t.get("plain_text", "") for t in parts).strip() or None
        return None
