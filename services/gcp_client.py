"""
GCP resumable upload client — faithful port of upload_media_to_gettr_v.1.1.json

Flow (matches n8n exactly):
  1. detect_media_type: extension → MIME type, media_type (image/video), filename
  2. get_media_channel: POST to Gettr upload API → get GCP URL + notify_url
  3. gcp_1: POST to GCP URL with x-goog-resumable: start → get Location header (upload session URL)
  4. download_media + upload: stream directly source → GCP PUT (no full buffer)
  5. send_notification: notify Gettr of completed upload → get final media metadata
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import aiohttp

from core.config import GettrConfig, GcpConfig

logger = logging.getLogger(__name__)

# MIME map from detect_media_type node
_MIME_MAP: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "avif": "image/avif",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "mts": "video/x-msvideo",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4v": "video/x-m4v",
    "ts": "video/MP2T",
    "m3u8": "video/mp4",  # HLS manifest — treat as video so it isn't misidentified as image
}

_VIDEO_URL_RE = re.compile(
    r"(/video/)|([?&]mime_type=video)|(\.(mp4|mov|avi|webm|mkv|flv|wmv|3gp|m4v|ts|mts)([?#]|$))",
    re.IGNORECASE,
)


def detect_media_type(media_url: str) -> tuple[str, str, str]:
    """
    Returns (content_type, media_type, filename).
    Replicates detect_media_type node logic exactly.

    Also handles URLs where the format is a query parameter rather than a file
    extension (e.g. pbs.twimg.com/media/XXX?format=jpg&name=large).
    """
    clean_path = re.split(r"[?#]", media_url)[0]
    parts = [p for p in clean_path.split("/") if p]
    filename_part = parts[-1] if parts else ""
    ext_match = re.search(r"\.([a-z0-9]+)$", filename_part, re.IGNORECASE)
    ext = ext_match.group(1).lower() if ext_match else ""

    # Fallback: check ?format=jpg / ?format=mp4 query parameter (pbs.twimg.com style)
    if not ext:
        qs_match = re.search(r"[?&]format=([a-z0-9]+)", media_url, re.IGNORECASE)
        if qs_match:
            ext = qs_match.group(1).lower()

    content_type = _MIME_MAP.get(ext)
    if not content_type:
        is_video = bool(_VIDEO_URL_RE.search(media_url))
        # Default to image/jpeg (not application/octet-stream) — Gettr CDN rejects
        # octet-stream uploads with ERR_UPLOAD_FAILURE and returns empty screen/ori.
        content_type = "video/mp4" if is_video else "image/jpeg"

    media_type = "video" if content_type.startswith("video/") else "image"
    tmp_ext = ext or ("mp4" if media_type == "video" else "jpg")
    filename = f"tempname.{tmp_ext}"

    return content_type, media_type, filename


class GcpClient:
    def __init__(
        self,
        gettr_config: GettrConfig,
        gcp_config: GcpConfig,
        session: aiohttp.ClientSession,
    ) -> None:
        self._gettr = gettr_config
        self._gcp = gcp_config
        self._session = session

    def reload_config(self, gettr_config: GettrConfig) -> None:
        """Hot-swap the Gettr credentials used to request upload channels."""
        self._gettr = gettr_config

    async def upload_media(
        self,
        media_url: str,
        download_cookie: Optional[str] = None,
        max_retries: int = 3,
        retry_delay_s: float = 5.0,
        content_type_override: Optional[str] = None,
    ) -> dict:
        """
        Full media upload flow with retry on failure.
        Matches n8n's retryOnFail=true, waitBetweenTries=5000 on the upload node.
        Returns Gettr media metadata dict. Raises after all retries exhausted.
        content_type_override: force a specific MIME type (e.g. "image/jpeg" for
        URLs with no extension like Pollinations).
        """
        auto_content_type, media_type, filename = detect_media_type(media_url)
        if content_type_override:
            content_type = content_type_override
            media_type = "video" if content_type_override.startswith("video/") else "image"
            _ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                        "image/gif": "gif"}
            ext = _ext_map.get(content_type_override, "jpg")
            filename = f"tempname.{ext}"
        else:
            content_type = auto_content_type
        last_exc: Exception = RuntimeError("Upload never attempted")

        for attempt in range(1, max_retries + 1):
            try:
                # Step 1: Get Gettr upload channel (GCP URL + notify_url)
                # Re-request each attempt — channels may expire
                channel = await self._get_upload_channel(filename)
                # Gettr API returns "gcs" key (Google Cloud Storage); older docs show "gcp"
                gcs_info = channel.get("gcs") or channel.get("gcp") or {}
                gcp_url = gcs_info.get("url")
                if not gcp_url:
                    raise RuntimeError(f"Gettr upload channel missing storage URL: {channel}")
                notify_url = channel["notify_url"]

                # Step 2: Initiate GCP resumable upload session → get Location URL
                location = await self._initiate_gcp_session(gcp_url, content_type)

                # Step 3: Stream download + upload to GCP (with proxy fallback on download failure)
                try:
                    await self._stream_to_gcp(media_url, location, content_type, download_cookie)
                except Exception as direct_exc:
                    if not self._gcp.download_proxy_url:
                        raise
                    logger.warning(
                        "Direct download failed for %s: %s — retrying via proxy",
                        media_url[:80], direct_exc,
                    )
                    data = await self._download_via_proxy(media_url)
                    async with self._session.put(
                        location,
                        data=data,
                        headers={"content-type": content_type},
                        timeout=aiohttp.ClientTimeout(total=self._gcp.resumable_upload_timeout_s),
                    ) as gcp_resp:
                        gcp_resp.raise_for_status()

                # Step 4: Notify Gettr of completion → get media metadata
                uploaded_url = location.split("?")[0]
                media_meta = await self._notify_gettr(notify_url, uploaded_url)
                if media_meta.get("message") == "ERR_UPLOAD_FAILURE" or (
                    not media_meta.get("ori") and not media_meta.get("screen")
                    and media_meta.get("status", 0) != 0
                ):
                    raise RuntimeError(
                        f"Gettr notify returned upload failure for {media_url}: {media_meta.get('message')}"
                    )
                media_meta["media_type"] = media_type
                return media_meta

            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "Media upload attempt %d/%d failed for %s: %s — retrying in %.0fs",
                        attempt, max_retries, media_url, exc, retry_delay_s,
                    )
                    await asyncio.sleep(retry_delay_s)
                else:
                    logger.error(
                        "Media upload failed after %d attempts for %s: %s",
                        max_retries, media_url, exc,
                    )

        raise last_exc

    async def upload_bytes(
        self,
        data: bytes,
        content_type: str = "image/jpeg",
        filename: str = "tempname.jpg",
        media_type: str = "image",
        extra_meta: Optional[dict] = None,
    ) -> dict:
        """Upload raw bytes to the Gettr CDN (no source URL needed).

        Used for locally generated media — a Pollinations image, or an MP4 from
        the video generator — so the slow generation step and the upload step
        are fully decoupled.

        `media_type` must be "video" for MP4s: GettrClient._build_post_with_media
        keys the whole vid/ovid/pvid payload off it, and mislabelling a video as
        an image silently posts it into `imgs` instead. `extra_meta` carries the
        fields only the generator knows (duration, vid_wid, vid_hgt).

        Returns Gettr media metadata dict. Raises on failure.
        """
        channel = await self._get_upload_channel(filename)
        gcs_info = channel.get("gcs") or channel.get("gcp") or {}
        gcp_url = gcs_info.get("url")
        if not gcp_url:
            raise RuntimeError(f"Gettr upload channel missing storage URL: {channel}")
        notify_url = channel["notify_url"]

        location = await self._initiate_gcp_session(gcp_url, content_type)

        async with self._session.put(
            location,
            data=data,
            headers={"content-type": content_type},
            timeout=aiohttp.ClientTimeout(total=self._gcp.resumable_upload_timeout_s),
        ) as resp:
            resp.raise_for_status()

        uploaded_url = location.split("?")[0]
        media_meta = await self._notify_gettr(notify_url, uploaded_url)
        media_meta["media_type"] = media_type
        if extra_meta:
            media_meta.update(extra_meta)
        return media_meta

    async def _download_via_proxy(self, media_url: str) -> bytes:
        """Download media via self-hosted proxy when direct download fails.

        Mirrors n8n 'HTTP Request1' node: POST to /api/v1/media/download with the
        source URL; proxy fetches it server-side and returns raw bytes.
        """
        async with self._session.post(
            self._gcp.download_proxy_url,
            json={"url": media_url},
            headers={"X-API-Key": self._gcp.download_proxy_api_key or ""},
            timeout=aiohttp.ClientTimeout(total=self._gcp.download_timeout_s),
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _get_upload_channel(self, filename: str) -> dict:
        url = "https://upload.gettr.com/media/get_upload_channel?scene=getter"
        headers = {
            "filename": filename,
            "authorization": self._gettr.user_token,
            "userid": self._gettr.user_id,
            "user-agent": self._gcp.user_agent,
        }
        async with self._session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return data

    async def _initiate_gcp_session(self, gcp_url: str, content_type: str) -> str:
        """POST to GCP initiation URL → returns Location header (upload session URL)."""
        headers = {
            "user-agent": self._gcp.user_agent,
            "x-goog-resumable": "start",
            "accept": "application/json",
            "content-type": content_type,
        }
        async with self._session.post(
            gcp_url,
            headers=headers,
            json={"unuse": 0},
            timeout=aiohttp.ClientTimeout(total=self._gcp.resumable_upload_timeout_s),
        ) as resp:
            resp.raise_for_status()
            location = resp.headers.get("Location") or resp.headers.get("location")
            if not location:
                body = await resp.json(content_type=None)
                location = body.get("headers", {}).get("location") or body.get("location")
            if not location:
                raise RuntimeError("GCP did not return a Location header for resumable upload")
        return location

    async def _stream_to_gcp(
        self,
        source_url: str,
        upload_url: str,
        content_type: str,
        cookie: Optional[str],
    ) -> None:
        """Stream media directly from source to GCP (no full buffering)."""
        download_headers = {
            "user-agent": self._gcp.user_agent,
            "accept": "*/*",
        }
        if cookie:
            download_headers["cookie"] = cookie
        # Twitter CDN expects Referer from x.com; all other CDNs get their own
        # origin as Referer to satisfy hotlink-protection checks.
        if "twimg.com" in source_url:
            download_headers["referer"] = "https://x.com/"
            download_headers["origin"] = "https://x.com"
        else:
            m = re.match(r'(https?://[^/]+)', source_url)
            if m:
                download_headers["referer"] = m.group(1) + "/"

        async with self._session.get(
            source_url,
            headers=download_headers,
            timeout=aiohttp.ClientTimeout(total=self._gcp.download_timeout_s),
        ) as src_resp:
            src_resp.raise_for_status()
            # Reject non-media responses (HTML login pages, paywalls, bot challenges)
            # that return HTTP 200 but are not actual images or videos.
            ct = src_resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ct and not (
                ct.startswith("image/")
                or ct.startswith("video/")
                or ct == "application/octet-stream"
            ):
                raise ValueError(
                    f"Source returned non-media content-type '{ct}' for {source_url}"
                )
            async with self._session.put(
                upload_url,
                data=src_resp.content,  # stream directly
                headers={"content-type": content_type},
                timeout=aiohttp.ClientTimeout(total=self._gcp.resumable_upload_timeout_s),
            ) as gcp_resp:
                gcp_resp.raise_for_status()

    async def _notify_gettr(self, notify_url: str, uploaded_url: str) -> dict:
        """Notify Gettr that upload is complete. Returns media metadata."""
        full_url = f"https://upload.gettr.com/{notify_url.lstrip('/')}"
        params = {"uploadedurl": uploaded_url, "result": "ok"}
        headers = {
            "authorization": self._gettr.user_token,
            "origin": "https://gettr.com",
            "userid": self._gettr.user_id,
            "user-agent": self._gcp.user_agent,
        }
        # Gettr's notify endpoint requires GET (POST returns 403)
        async with self._session.get(
            full_url, params=params, headers=headers
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return data
