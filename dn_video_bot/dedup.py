"""
URL-hash dedup — catches an editor accidentally logging the same X link on two
different Notion rows. Own `dn_video:` Redis prefix. TTL 24h (changed
2026-08-02) — a repeat of the same link past that window is treated as new.
"""

from __future__ import annotations

import logging

from core.redis_client import RedisClient
from utils.hashing import sha256_url_hash

logger = logging.getLogger(__name__)


class UrlDedup:
    def __init__(self, redis: RedisClient, key_prefix: str, ttl_hours: int = 24) -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._ttl_s = ttl_hours * 3600

    def _key(self, url: str) -> str:
        return f"{self._prefix}{sha256_url_hash(url)}"

    async def already_seen(self, url: str) -> bool:
        return await self._redis.exists(self._key(url))

    async def mark_seen(self, url: str) -> None:
        await self._redis.set(self._key(url), "1", ex=self._ttl_s)
