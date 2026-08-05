"""
Gettr API client — faithful port of v1.0_subflow_dailynews_to_gettr.json

Builds and posts the Gettr payload:
  WITH media:    _t, acl, txt, imgs, vid, main, ovid, pvid, nmvid, vid_dur/wid/hgt
  WITHOUT media: _t, acl, txt, dsc, previmg, prevsrc, ttl

Authentication: x-app-auth header with JSON {"user": ..., "token": ...}
Transport: multipart/form-data, field name "content"
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiohttp

from core.config import GettrConfig

logger = logging.getLogger(__name__)


def _build_auth(config: GettrConfig) -> str:
    return json.dumps({"user": config.user_id, "token": config.user_token})


def _build_post_with_media(
    post_content: str,
    media: list[dict],
    user_id: str,
) -> str:
    """
    Build Gettr post payload with media.
    Replicates 'Prepare Gettr Post' node exactly.
    """
    now_ms = int(time.time() * 1000)

    # Find first video — prefer m3u8 (HLS), fall back to media_type=="video" for
    # cases where Gettr's transcoding hasn't finished yet and m3u8 is absent.
    def _is_video(m: dict) -> bool:
        return bool(m.get("m3u8")) or m.get("media_type") == "video"

    video_el = next((m for m in media if _is_video(m)), None)
    # Non-video items → images; additional video items → use their screen thumbnail as image
    # so multi-video tweets show all videos' thumbnails rather than silently dropping them.
    imgs = []
    for m in media:
        if _is_video(m):
            if m is not video_el:
                # Additional video: add thumbnail to imgs
                thumb = m.get("screen") or m.get("ori")
                if thumb:
                    imgs.append(thumb)
        else:
            img = m.get("screen") or m.get("ori")
            if img:
                imgs.append(img)  # filter None

    data = {
        "_t": "post",
        "acl": {"_t": "acl"},
        "txt": str(post_content).strip(),
        "udate": now_ms,
        "cdate": now_ms,
        "uid": user_id,
        "imgs": imgs,
        "main": (
            (video_el.get("screen") or video_el.get("ori"))
            if video_el
            else (media[0].get("screen") or media[0].get("ori") if media else None)
        ),
        # vid/pvid: prefer HLS stream (m3u8); fall back to native mp4 (nm_uri/ori)
        # when Gettr hasn't finished transcoding yet.
        "vid": (video_el.get("m3u8") or video_el.get("nm_uri") or video_el.get("ori")) if video_el else None,
        "ovid": video_el.get("nm_uri") if video_el else None,
        "pvid": (video_el.get("m3u8") or video_el.get("nm_uri") or video_el.get("ori")) if video_el else None,
        "nmvid": video_el.get("nm_uri") if video_el else None,
        "vid_dur": video_el.get("duration") if video_el else None,
        "vid_wid": (
            video_el.get("width")
            if video_el
            else (media[0].get("im_width") if len(imgs) == 1 and media else None)
        ),
        "vid_hgt": (
            video_el.get("height")
            if video_el
            else (media[0].get("im_height") if len(imgs) == 1 and media else None)
        ),
    }
    # Remove None values to keep payload clean
    data = {k: v for k, v in data.items() if v is not None or k in ("acl", "imgs")}

    return json.dumps({"data": data, "aux": None, "serial": "post"})


def _build_post_without_media(
    post_content: str,
    user_id: str,
    prev_desc: Optional[str] = None,
    prev_img: Optional[str] = None,
    prev_src_link: Optional[str] = None,
    prev_ttl: Optional[str] = None,
) -> str:
    """
    Build Gettr post payload without media (with OG preview).
    Replicates 'Prepare Gettr Post w/o Media' node exactly.
    """
    now_ms = int(time.time() * 1000)

    data: dict = {
        "_t": "post",
        "acl": {"_t": "acl"},
        "txt": post_content,
        "udate": now_ms,
        "cdate": now_ms,
        "uid": user_id,
    }
    # Only include preview fields when they have actual values — empty/None
    # causes Gettr to return an empty response body and reject the post
    if prev_desc:
        data["dsc"] = prev_desc
    if prev_img:
        data["previmg"] = prev_img
    if prev_src_link:
        data["prevsrc"] = prev_src_link
    if prev_ttl:
        data["ttl"] = prev_ttl
    return json.dumps({"data": data, "aux": None, "serial": "post"})


class GettrClient:
    def __init__(self, config: GettrConfig, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session

    def reload_config(self, config: GettrConfig) -> None:
        """Hot-reload credentials. Called after config.yaml is rewritten."""
        self._config = config
        logger.info("GettrClient: credentials reloaded (user_id=%s)", config.user_id)

    async def post_with_media(self, post_content: str, media: list[dict]) -> dict:
        """Post to Gettr with uploaded media."""
        payload = _build_post_with_media(post_content, media, self._config.user_id)
        return await self._post(payload)

    async def post_without_media(
        self,
        post_content: str,
        prev_desc: Optional[str] = None,
        prev_img: Optional[str] = None,
        prev_src_link: Optional[str] = None,
        prev_ttl: Optional[str] = None,
    ) -> dict:
        """Post to Gettr with URL preview metadata."""
        payload = _build_post_without_media(
            post_content,
            self._config.user_id,
            prev_desc=prev_desc,
            prev_img=prev_img,
            prev_src_link=prev_src_link,
            prev_ttl=prev_ttl,
        )
        return await self._post(payload)

    async def _post(self, content_payload: str) -> dict:
        """Send multipart/form-data POST to Gettr API."""
        auth = _build_auth(self._config)
        headers = {"x-app-auth": auth}

        form = aiohttp.FormData()
        form.add_field("content", content_payload)

        async with self._session.post(
            self._config.api_url,
            headers=headers,
            data=form,
        ) as resp:
            raw = await resp.text()
            if not raw.strip():
                raise RuntimeError(f"Gettr API returned empty response (HTTP {resp.status})")
            body = json.loads(raw)
            if resp.status not in (200, 201):
                raise RuntimeError(f"Gettr API error {resp.status}: {body}")
        return body
