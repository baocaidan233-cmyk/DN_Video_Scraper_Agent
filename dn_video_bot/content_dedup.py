"""
Content-similarity dedup — catches two DIFFERENT X links covering the same
underlying story. Compares only against THIS bot's own recent publishes.

2026-07-31: switched off Qdrant. This channel has nothing to do with
EpicFury/dailynews-agent, and at a max of 30 posts/day this never needed a
full vector database — a small Redis sorted set + in-process cosine
similarity is enough, and needs no external account of any kind (reuses the
Redis instance this bot already owns for URL dedup).
"""

from __future__ import annotations

import json
import logging
import time

from core.redis_client import RedisClient
from services.openai_client import OpenAIClient

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class ContentDedup:
    def __init__(
        self,
        redis: RedisClient,
        openai: OpenAIClient,
        threshold: float,
        window_hours: int,
        redis_key: str = "dn_video:content_embeddings",
    ) -> None:
        self._redis = redis
        self._openai = openai
        self._threshold = threshold
        self._window_s = window_hours * 3600
        self._key = redis_key

    async def is_duplicate(self, caption: str) -> bool:
        """Fails open — an OpenAI/Redis error here should never block a publish."""
        if not caption.strip():
            return False
        try:
            embedding = (await self._openai.embed_texts([caption]))[0]
            cutoff = time.time() - self._window_s
            # Trim anything outside the lookback window before reading, so the
            # set never grows past what this window ever needs.
            await self._redis.zremrangebyscore(self._key, 0, cutoff)
            raw_entries = await self._redis.client.zrangebyscore(self._key, cutoff, "+inf")
        except Exception as e:
            logger.warning("Content dedup check failed (%s) — allowing through", e)
            return False

        best_score, best_caption = 0.0, ""
        for raw in raw_entries:
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            score = _cosine(embedding, entry.get("embedding", []))
            if score > best_score:
                best_score, best_caption = score, entry.get("caption", "")

        if best_score >= self._threshold:
            logger.info(
                "Content dedup: caption matches recent post (score=%.3f): %s",
                best_score, best_caption[:80],
            )
            return True
        return False

    async def record(self, article_id: str, url: str, caption: str) -> None:
        """Best-effort — failing to record shouldn't undo an already-successful publish."""
        try:
            embedding = (await self._openai.embed_texts([caption]))[0]
            entry = json.dumps({"embedding": embedding, "caption": caption[:200], "url": url})
            await self._redis.zadd(self._key, {entry: time.time()})
        except Exception as e:
            logger.warning("Content dedup record failed for %s (%s) — continuing", article_id, e)
