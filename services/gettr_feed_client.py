"""
Public, unauthenticated Gettr per-account posts feed.

Used by NotionTopicalDedupChecker.run_gettr_crosscheck_loop to compare
not-yet-sent Notion cards against what's already live on Gettr — catches
duplicates that never had a matching Notion candidate to diff against at
publish time (see notion_topical_dedup.py's module docstring).

Endpoint is undocumented (Gettr has no official public API); confirmed
working by direct testing:
  GET https://api.gettr.com/u/user/{handle}/posts?max=20&dir=fwd&incl=posts|stats|userinfo|shared|liked&fp=f_uo

Only fetches the single most recent page (up to `max` posts) — the cursor
query param for walking further back was never confirmed against a live
response, so this deliberately doesn't guess at it. For an hourly-cadence
channel this covers roughly the last day; if a deeper window turns out to
be needed, that pagination needs verifying against a real response before
being added.

**Response shape** (verified against a live 200 on 2026-07-29): `result.data.list`
does NOT contain posts — it contains *activity* records, whose `_id` is the
activity id (`dailynews_ak62bfuqbdc8`), not the post id. The post body lives in
`result.aux.post[<post_id>].txt`, keyed by the activity's `activity.pstid`
(`p429iuh4172`). Reading `txt`/`_id` off the list entries yields empty text and
an id that builds a broken `gettr.com/post/...` link, so both are resolved
through `aux.post` here.
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)

_BASE = "https://api.gettr.com/u/user"


class GettrFeedClient:
    def __init__(self, session: aiohttp.ClientSession, handle: str, max_posts: int = 20) -> None:
        self._session = session
        self._handle = handle
        self._max_posts = max_posts

    async def fetch_recent_posts(self) -> list[dict]:
        """Returns [{"id": ..., "text": ...}, ...] for the most recent posts.
        Fails open (empty list) on any error."""
        params = {
            "max": str(self._max_posts),
            "dir": "fwd",
            "incl": "posts|stats|userinfo|shared|liked",
            "fp": "f_uo",
        }
        try:
            async with self._session.get(f"{_BASE}/{self._handle}/posts", params=params) as resp:
                if resp.status != 200:
                    logger.warning("Gettr feed fetch failed (%d) for %s", resp.status, self._handle)
                    return []
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("Gettr feed fetch error for %s: %s", self._handle, e)
            return []

        result = data.get("result", {})
        activities = result.get("data", {}).get("list", []) or []
        posts_by_id = result.get("aux", {}).get("post", {}) or {}

        out: list[dict] = []
        seen: set[str] = set()
        for item in activities:
            activity = item.get("activity", {}) or {}
            post_id = activity.get("pstid") or activity.get("tgt_id") or ""
            if not post_id or post_id in seen:
                continue
            seen.add(post_id)
            text = (posts_by_id.get(post_id, {}) or {}).get("txt", "")
            out.append({"id": post_id, "text": text})

        if activities and not any(p["text"].strip() for p in out):
            logger.warning(
                "Gettr feed for %s returned %d activity record(s) but no post text — "
                "the aux.post response shape may have changed",
                self._handle, len(activities),
            )
        return out
