"""
Writes the final Gettr caption as an edited/tightened rewrite of the tweet's
own text (no video frame analysis, no transcription — dropped per updated
requirement), then prepends "JUST IN - " or "BREAKING - ".

BREAKING is used when the tweet is within `breaking_window_hours` AND the
editor flagged Urgency (either tier) — i.e. we defer "is this actually big
news" to the human editor's own Urgency flag rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def _mentions_block(mentions: list[tuple[str, str]]) -> str:
    if not mentions:
        return ""
    lines = "\n".join(f'- @{handle} -> "{name}"' for handle, name in mentions if name)
    if not lines:
        return ""
    return (
        "\nMentioned accounts in this tweet (X handle -> that account's own display name on X):\n"
        f"{lines}\n"
    )


class CaptionWriter:
    def __init__(self, openai_api_key: str, model: str, breaking_window_hours: int) -> None:
        self._client = AsyncOpenAI(api_key=openai_api_key)
        self._model = model
        self._breaking_window_hours = breaking_window_hours
        self._system_prompt = _load_prompt("dn_video_caption_system.txt")
        self._user_template = _load_prompt("dn_video_caption_user.txt")

    def _prefix(self, tweet_created_at: datetime, urgency: str) -> str:
        hours_old = (datetime.now(tweet_created_at.tzinfo) - tweet_created_at).total_seconds() / 3600.0
        is_breaking = hours_old <= self._breaking_window_hours and urgency in ("🔥", "🔥🔥🔥")
        return "BREAKING - " if is_breaking else "JUST IN - "

    async def write(
        self,
        tweet_text: str,
        tweet_created_at: datetime,
        urgency: str,
        account_name: str,
        account_handle: str,
        account_label: str,
        mentions: list[tuple[str, str]] | None = None,
    ) -> str:
        account_desc = account_name or f"@{account_handle}"
        if account_label:
            account_desc += f" ({account_label})"
        user_text = self._user_template.format(
            tweet_text=tweet_text or "(no text)",
            account_desc=account_desc,
            mentions_block=_mentions_block(mentions or []),
        )

        response = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=200,
            temperature=0.4,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_text},
            ],
            timeout=60,
        )
        caption = (response.choices[0].message.content or "").strip()
        return self._prefix(tweet_created_at, urgency) + caption
