from __future__ import annotations

from typing import Any, Optional

import redis.asyncio as aioredis

from core.config import RedisConfig


class RedisClient:
    def __init__(self, config: RedisConfig) -> None:
        self._config = config
        self._pool: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._pool = aioredis.from_url(
            self._config.url,
            encoding="utf-8",
            decode_responses=True,
            ssl_cert_reqs=None,  # Upstash TLS on port 6379 uses self-signed cert
        )
        # Verify connection
        await self._pool.ping()

    async def close(self) -> None:
        if self._pool:
            await self._pool.aclose()

    @property
    def client(self) -> aioredis.Redis:
        if self._pool is None:
            raise RuntimeError("RedisClient not connected. Call connect() first.")
        return self._pool

    # --- Basic operations ---

    async def get(self, key: str) -> Optional[str]:
        return await self.client.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        await self.client.set(key, value, ex=ex)

    async def setnx(self, key: str, value: str) -> bool:
        """Set if not exists. Returns True if set, False if already existed."""
        return await self.client.setnx(key, value)

    async def expire(self, key: str, seconds: int) -> None:
        await self.client.expire(key, seconds)

    async def delete(self, *keys: str) -> None:
        await self.client.delete(*keys)

    async def exists(self, key: str) -> bool:
        return bool(await self.client.exists(key))

    # --- Hash operations ---

    async def hset(self, key: str, mapping: dict[str, Any]) -> None:
        await self.client.hset(key, mapping=mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return await self.client.hgetall(key)

    async def hget(self, key: str, field: str) -> Optional[str]:
        return await self.client.hget(key, field)

    async def incr(self, key: str) -> int:
        """Increment a counter. Returns the new value (1 on first increment)."""
        return await self.client.incr(key)

    # --- Sorted-set operations (rolling time windows) ---

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        return await self.client.zadd(key, mapping)

    async def zcard(self, key: str) -> int:
        return await self.client.zcard(key)

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        return await self.client.zremrangebyscore(key, min_score, max_score)

    # --- List operations ---

    async def lpush(self, key: str, *values: str) -> int:
        return await self.client.lpush(key, *values)

    async def rpop(self, key: str) -> Optional[str]:
        return await self.client.rpop(key)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return await self.client.lrange(key, start, end)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        await self.client.ltrim(key, start, end)

    async def llen(self, key: str) -> int:
        return await self.client.llen(key)

    # --- Batch URL hash dedup (Redis pipeline) ---

    async def batch_setnx_with_ttl(
        self, key_value_pairs: list[tuple[str, str]], ttl_s: int
    ) -> list[bool]:
        """
        Check and set multiple keys atomically using a pipeline.
        Uses SET NX EX (single atomic command) instead of SETNX + EXPIRE.
        Returns list of booleans: True = key was new (set), False = already existed.
        """
        pipe = self.client.pipeline()
        for key, _ in key_value_pairs:
            pipe.set(key, "1", nx=True, ex=ttl_s)
        results = await pipe.execute()
        # SET NX returns True/"OK" when set, None when key already existed
        return [bool(r) for r in results]
