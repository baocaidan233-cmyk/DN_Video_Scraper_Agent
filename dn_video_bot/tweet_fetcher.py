"""
Given one X/Twitter post URL, fetch that single tweet via Twitter's own
public embed-widget backend (cdn.syndication.twimg.com/tweet-result) — free,
unauthenticated, no API key. Confirmed live 2026-07-30 that this endpoint
returns everything needed (text, created_at, mediaDetails with video
variants), not just media as x_agent.py's narrower use of it assumed.

Deliberately does NOT use twitterapi.io here — that's a paid, per-call API
and this free endpoint covers single-tweet-by-id lookups just as well. (It's
still the right choice for x_agent.py's own timeline monitoring, which needs
to poll whole accounts, not just one already-known tweet.)

Caveat: this is an unofficial/undocumented endpoint (the same one Twitter's
own embed widget calls) — no SLA, could break or get rate-limited without
notice. Only works for public tweets Twitter allows to be embedded.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import aiohttp

from agents.x_agent import (
    _SYNDICATION_HEADERS,
    _extract_media_from_extended_entities,
    _tweet_embed_token,
)

logger = logging.getLogger(__name__)

_STATUS_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
_TWEET_RESULT_URL = "https://cdn.syndication.twimg.com/tweet-result"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


def parse_tweet_id(url: str) -> str | None:
    m = _STATUS_ID_RE.search(url)
    return m.group(1) if m else None


class TweetData:
    def __init__(
        self, text: str, created_at: datetime, video_url: str | None,
        account_name: str, account_handle: str, account_label: str,
        mentions: list[tuple[str, str]] | None = None,
    ) -> None:
        self.text = text
        self.created_at = created_at
        self.video_url = video_url
        self.account_name = account_name      # display name, e.g. "Secretary Sean Duffy"
        self.account_handle = account_handle  # e.g. "SecDuffy"
        self.account_label = account_label    # e.g. "U.S. Department of Transportation", or "" if none
        # Other accounts @-mentioned in the tweet text, as (screen_name, their own X display name),
        # e.g. [("Doranimated", "Mike")] — comes free from the same tweet-result response, no extra
        # API call. The posting account itself is filtered out.
        self.mentions = mentions or []

    @property
    def hours_old(self) -> float:
        now = datetime.now(timezone.utc)
        return (now - self.created_at).total_seconds() / 3600.0


class TweetFetchError(Exception):
    pass


class TweetFetcher:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch(self, url: str) -> TweetData:
        tweet_id = parse_tweet_id(url)
        if not tweet_id:
            raise TweetFetchError(f"Could not parse a tweet id out of URL: {url}")

        endpoint = f"{_TWEET_RESULT_URL}?id={tweet_id}&lang=en&token={_tweet_embed_token(tweet_id)}"
        try:
            async with self._session.get(endpoint, headers=_SYNDICATION_HEADERS, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    raise TweetFetchError(f"tweet-result endpoint {resp.status} for tweet {tweet_id}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise TweetFetchError(f"tweet-result request failed for tweet {tweet_id}: {e}") from e

        text = (data.get("text") or "").strip()
        if not text:
            raise TweetFetchError(f"Tweet {tweet_id}: empty/missing text in response — {data}")

        _url_to_image, video_url, _all_media_urls, has_video = _extract_media_from_extended_entities(
            {"media": data.get("mediaDetails") or []}
        )
        if not has_video:
            raise TweetFetchError(f"Tweet {tweet_id} has no video attached")

        try:
            created_at = datetime.fromisoformat((data.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            raise TweetFetchError(f"Tweet {tweet_id}: could not parse created_at {data.get('created_at')!r}")

        user = data.get("user") or {}
        own_handle = (user.get("screen_name") or "").lower()
        raw_mentions = (data.get("entities") or {}).get("user_mentions") or []
        mentions = [
            (m.get("screen_name", ""), m.get("name", ""))
            for m in raw_mentions
            if m.get("screen_name") and m.get("screen_name", "").lower() != own_handle
        ]

        return TweetData(
            text=text, created_at=created_at, video_url=video_url,
            account_name=user.get("name", ""),
            account_handle=user.get("screen_name", ""),
            account_label=(user.get("highlighted_label") or {}).get("description", ""),
            mentions=mentions,
        )
