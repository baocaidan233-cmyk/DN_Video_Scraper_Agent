"""
Thin wrapper around the existing GcpClient/GettrClient — no PublishAgent
involved, since none of its dedup/Notion-callback/word-count machinery
applies here (that's all handled by dn_video_bot's own modules).
"""

from __future__ import annotations

import logging

from services.gcp_client import GcpClient
from services.gettr_client import GettrClient

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(self, gcp: GcpClient, gettr: GettrClient) -> None:
        self._gcp = gcp
        self._gettr = gettr

    async def publish_video(self, video_url: str, caption: str) -> str:
        """Uploads the video to Gettr's CDN and posts it. Returns the Gettr post id."""
        media_meta = await self._gcp.upload_media(video_url)
        result = await self._gettr.post_with_media(caption, [media_meta])
        post_id = result["result"]["data"]["_id"]
        return post_id

    @staticmethod
    def post_link(post_id: str) -> str:
        return f"https://gettr.com/post/{post_id}"
