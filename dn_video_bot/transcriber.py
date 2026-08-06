"""
Transcribes a tweet's video audio via OpenAI's audio transcription endpoint
(gpt-4o-mini-transcribe by default), so the caption writer has access to
everything actually said in the video — not just whatever the tweet's own
text happened to summarize. Text-only tweets skip this (no audio_source_url).

Downloads the LOWEST-bitrate mp4 variant (not the one used for the actual
Gettr publish, which wants the highest quality) — audio content is identical
across variants and this keeps the file well under the API's upload limit.
"""

from __future__ import annotations

import logging

import aiohttp
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_DOWNLOAD_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "referer": "https://x.com/",
    "origin": "https://x.com",
}
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_MAX_BYTES = 20 * 1024 * 1024  # stay well under the API's 25MB cap


class Transcriber:
    def __init__(self, openai_api_key: str, model: str, session: aiohttp.ClientSession) -> None:
        self._client = AsyncOpenAI(api_key=openai_api_key)
        self._model = model
        self._session = session

    async def transcribe(self, audio_source_url: str) -> str:
        """Returns the transcript text, or "" on any failure (never blocks publishing)."""
        if not audio_source_url:
            return ""
        try:
            async with self._session.get(
                audio_source_url, headers=_DOWNLOAD_HEADERS, timeout=_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.read()
        except aiohttp.ClientError as e:
            logger.warning("Transcriber: download failed for %s — %s", audio_source_url, e)
            return ""

        if not data or len(data) > _MAX_BYTES:
            logger.warning("Transcriber: skipping %s — size %d bytes", audio_source_url, len(data))
            return ""

        try:
            resp = await self._client.audio.transcriptions.create(
                model=self._model,
                file=("clip.mp4", data, "video/mp4"),
            )
        except Exception as e:  # noqa: BLE001 — transcription is best-effort, never fatal
            logger.warning("Transcriber: transcription failed for %s — %s", audio_source_url, e)
            return ""

        return (resp.text or "").strip()
