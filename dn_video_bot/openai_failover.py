"""
Wraps one or more OpenAI API keys and fails over to the next one when the
current key is out of credits — rather than retrying (and logging errors
for) a dead key forever, as happened 2026-08-07 when the primary key ran
out mid-production and every tick failed until someone noticed and topped
up billing.

Sticky: once a key is marked dead, later calls skip straight to the next
live key — no wasted retry against a key we already know is exhausted.
Scoped to dn_video_bot only (not services/openai_client.py, which is
shared with leon's own dailynews-agent pipeline — see config.py's note
about not touching his shared config/clients).
"""

from __future__ import annotations

import logging

from openai import APIStatusError, AsyncOpenAI

logger = logging.getLogger(__name__)


def _is_quota_error(e: Exception) -> bool:
    if not isinstance(e, APIStatusError) or e.status_code != 429:
        return False
    body = getattr(e, "body", None)
    # e.body is the error object itself (e.g. {"message":..., "code":
    # "credit_balance_exhausted"}) — NOT wrapped in an outer {"error": {...}}
    # the way the raw HTTP JSON response is. Confirmed live 2026-08-07.
    if not isinstance(body, dict):
        return False
    code = body.get("code") or body.get("error", {}).get("code")
    return code in ("insufficient_quota", "credit_balance_exhausted")


class FailoverOpenAI:
    def __init__(self, api_keys: list[str]) -> None:
        keys = [k for k in api_keys if k]
        if not keys:
            raise ValueError("FailoverOpenAI needs at least one non-empty API key")
        self._clients = [AsyncOpenAI(api_key=k) for k in keys]
        self._active = 0

    async def chat_completion(self, **kwargs):
        return await self._call_with_failover(
            lambda client: client.chat.completions.create(**kwargs)
        )

    async def transcription(self, **kwargs):
        return await self._call_with_failover(
            lambda client: client.audio.transcriptions.create(**kwargs)
        )

    async def _call_with_failover(self, call):
        while True:
            client = self._clients[self._active]
            try:
                return await call(client)
            except Exception as e:
                if _is_quota_error(e) and self._active < len(self._clients) - 1:
                    self._active += 1
                    logger.warning(
                        "OpenAI key #%d ran out of credits — failing over to key #%d",
                        self._active, self._active + 1,
                    )
                    continue
                raise
