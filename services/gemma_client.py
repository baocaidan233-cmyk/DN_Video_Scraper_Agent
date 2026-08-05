"""
GemmaClient — Anti-CCP content verification using Google's Gemma 4 model.
Calls Google AI Studio via the OpenAI-compatible endpoint.
Uses prompts/verify_post.txt as the system prompt.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

VERIFY_PROMPT_PATH = Path("prompts/verify_post.txt")

# Matches "**VERDICT:** PASS" / "**VERDICT:** FAIL" / "**VERDICT:** REVISE"
_VERDICT_RE = re.compile(r"\*\*VERDICT:\*\*\s*(PASS|FAIL|REVISE)", re.IGNORECASE)


class GemmaClient:
    def __init__(self, config) -> None:
        self._config = config
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self._system_prompt: str = self._load_prompt()

    def _load_prompt(self) -> str:
        try:
            return VERIFY_PROMPT_PATH.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("GemmaClient: could not load verify prompt from %s: %s", VERIFY_PROMPT_PATH, e)
            return ""

    def reload_prompt(self) -> None:
        """Re-read prompts/verify_post.txt from disk. Called by dashboard on save."""
        self._system_prompt = self._load_prompt()
        logger.info("GemmaClient: verify prompt reloaded")

    async def verify_post(self, title: str, post: str) -> tuple[str, str]:
        """
        Verify a generated Telegram post against editorial standards.

        Returns:
            (verdict, raw_output) where verdict is one of:
              PASS   — ready to publish
              REVISE — needs edits (blocked from Telegram)
              FAIL   — hard-fail violation (blocked from Telegram)
              ERROR  — API/network failure (treated as pass-through to avoid blocking)
        """
        if not self._system_prompt:
            logger.warning("GemmaClient: no verify prompt loaded — skipping verification (default PASS)")
            return "PASS", ""

        user_message = f"Title: {title}\n\nPost:\n{post}"

        try:
            response = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=self._config.max_tokens,
                temperature=0.1,
            )
            raw: str = response.choices[0].message.content or ""
            m = _VERDICT_RE.search(raw)
            if m:
                verdict = m.group(1).upper()
            else:
                logger.warning("GemmaClient: no VERDICT line found in response — treating as FAIL")
                verdict = "FAIL"
            return verdict, raw
        except Exception as e:
            logger.error("GemmaClient.verify_post failed: %s — treating as PASS to avoid blocking pipeline", e)
            return "ERROR", str(e)
