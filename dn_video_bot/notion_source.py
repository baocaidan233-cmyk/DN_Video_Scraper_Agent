"""
Reads eligible rows from the shared daily_news Notion database and writes
results back. Shares the database with the human editor's own workflow and
the legacy n8n job — only ever touches rows matching our own filter
(status=<status_value> AND URL not empty AND send_status=false), and only
ever writes to URL/send_status/post_content/Notes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionRow:
    def __init__(
        self, page_id: str, url: str, title: str, urgency: str, created_time: str,
        post_content: str = "",
    ) -> None:
        self.page_id = page_id
        self.url = url
        self.title = title
        self.urgency = urgency  # "" | "🔥" | "🔥🔥🔥"
        self.created_time = created_time
        self.post_content = post_content  # editor-authored caption, if any — see scheduler._process

    @property
    def priority(self) -> int:
        if self.urgency == "🔥🔥🔥":
            return 2
        if self.urgency == "🔥":
            return 1
        return 0


class NotionSource:
    def __init__(
        self, session: aiohttp.ClientSession, api_key: str, database_id: str,
        status_value: str, lookback_hours: int = 24,
    ) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._db_id = database_id
        self._status_value = status_value
        self._lookback_hours = lookback_hours

    async def fetch_eligible_rows(self) -> list[NotionRow]:
        """status=<status_value> AND URL not empty AND send_status=false
        AND Duplicate is empty AND created within the last lookback_hours,
        oldest first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)).isoformat()
        body = {
            "filter": {
                "and": [
                    {"property": "status", "status": {"equals": self._status_value}},
                    {"property": "URL", "url": {"is_not_empty": True}},
                    {"property": "send_status", "checkbox": {"equals": False}},
                    {"property": "Duplicate", "select": {"is_empty": True}},
                    {"property": "Created time", "created_time": {"on_or_after": cutoff}},
                ]
            },
            "sorts": [{"property": "Created time", "direction": "ascending"}],
            "page_size": 100,
        }
        rows: list[NotionRow] = []
        cursor: Optional[str] = None
        while True:
            if cursor:
                body["start_cursor"] = cursor
            try:
                async with self._session.post(
                    f"{NOTION_API_BASE}/databases/{self._db_id}/query",
                    headers=self._headers,
                    json=body,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning("Notion query failed (%d): %s", resp.status, text[:200])
                        break
                    data = await resp.json(content_type=None)
            except Exception as e:
                logger.warning("Notion query error: %s", e)
                break

            for page in data.get("results", []):
                props = page.get("properties", {})
                url = self._get_url(props, "URL")
                if not url:
                    continue
                rows.append(NotionRow(
                    page_id=page["id"],
                    url=url,
                    title=self._get_title(props, "title"),
                    urgency=self._get_select(props, "Urgency"),
                    created_time=page.get("created_time", ""),
                    post_content=self._get_rich_text(props, "post_content"),
                ))

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return rows

    async def mark_published(self, page_id: str, post_content: str, gettr_link: str) -> None:
        """Writes back post_content + Notes only. Deliberately does NOT check
        send_status — a human editor checks that box themselves after
        reviewing the post (per explicit user instruction 2026-08-02). This
        bot's own Redis url_dedup (not send_status) is what actually prevents
        re-publishing the same link on a later poll."""
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={
                    "properties": {
                        "post_content": {"rich_text": [{"text": {"content": post_content[:2000]}}]},
                        "Notes": {"rich_text": [{"text": {"content": gettr_link[:2000]}}]},
                    }
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("Notion mark_published failed (%d) for %s: %s", resp.status, page_id, text[:200])
                else:
                    logger.info("Notion page %s marked published (%s)", page_id, gettr_link)
        except Exception as e:
            logger.warning("Notion mark_published error for %s: %s", page_id, e)

    async def mark_note_only(self, page_id: str, gettr_link: str) -> None:
        """Used when the editor already wrote post_content themselves — that
        text was used verbatim to publish, so only the Gettr link needs
        writing back, post_content is left untouched."""
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"Notes": {"rich_text": [{"text": {"content": gettr_link[:2000]}}]}}},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("Notion mark_note_only failed (%d) for %s: %s", resp.status, page_id, text[:200])
                else:
                    logger.info("Notion page %s marked published (editor-authored, %s)", page_id, gettr_link)
        except Exception as e:
            logger.warning("Notion mark_note_only error for %s: %s", page_id, e)

    async def mark_duplicate(self, page_id: str, note: str) -> None:
        try:
            async with self._session.patch(
                f"{NOTION_API_BASE}/pages/{page_id}",
                headers=self._headers,
                json={
                    "properties": {
                        "Duplicate": {"select": {"name": "Duplicate"}},
                        "Notes": {"rich_text": [{"text": {"content": note[:2000]}}]},
                    }
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("Notion mark_duplicate failed (%d) for %s: %s", resp.status, page_id, text[:200])
                else:
                    logger.info("Notion page %s marked Duplicate", page_id)
        except Exception as e:
            logger.warning("Notion mark_duplicate error for %s: %s", page_id, e)

    @staticmethod
    def _get_url(props: dict, key: str) -> Optional[str]:
        p = props.get(key, {})
        return p.get("url") if p.get("type") == "url" else None

    @staticmethod
    def _get_rich_text(props: dict, key: str) -> str:
        p = props.get(key, {})
        if p.get("type") == "rich_text":
            return "".join(t.get("plain_text", "") for t in p.get("rich_text", [])).strip()
        return ""

    @staticmethod
    def _get_title(props: dict, key: str) -> str:
        p = props.get(key, {})
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", [])).strip()
        return ""

    @staticmethod
    def _get_select(props: dict, key: str) -> str:
        p = props.get(key, {})
        sel = p.get("select") or {}
        return sel.get("name", "") if p.get("type") == "select" else ""
