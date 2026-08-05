"""
Qdrant vector database client — async wrapper.
Used by SimilarityAgent for cross-batch deduplication.
Collection: dailynews_embeddings (text-embedding-3-small, 1536 dims)
"""

from __future__ import annotations

import logging
import warnings
from datetime import datetime, timezone, timedelta

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    Range,
    MatchAny,
    PayloadSchemaType,
)

from core.config import QdrantConfig

logger = logging.getLogger(__name__)


class QdrantWrapper:
    def __init__(self, config: QdrantConfig, collection_override: str | None = None) -> None:
        self._config = config
        self._collection = collection_override or config.collection
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._client = AsyncQdrantClient(
                url=config.url,
                api_key=config.api_key,
            )

    async def ensure_collection(self) -> None:
        """Create collection if it doesn't exist."""
        collections = await self._client.get_collections()
        names = [c.name for c in collections.collections]
        if self._collection not in names:
            try:
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=self._config.vector_size,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection: %s", self._collection)
            except Exception as e:
                # 403 Forbidden = collection-scoped key can't create, but collection likely exists.
                # 409 Conflict = already exists. Either way, continue — operations will fail
                # loudly if the collection truly doesn't exist.
                logger.warning("Could not create Qdrant collection %s (assuming it exists): %s", self._collection, e)

        # Ensure payload index on published_at_ts for range filtering
        try:
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name="published_at_ts",
                field_schema=PayloadSchemaType.FLOAT,
            )
        except Exception:
            pass  # Index already exists — ignore

        # Ensure payload index on created_at_ts (used by Notion dedup collection)
        try:
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name="created_at_ts",
                field_schema=PayloadSchemaType.FLOAT,
            )
        except Exception:
            pass  # Index already exists — ignore

    async def search_similar(
        self,
        embedding: list[float],
        published_after: datetime,
        top_k: int = 5,
    ) -> list[dict]:
        """
        Search for similar articles published within the cross_batch_hours window.
        Returns list of {score, url, title, published_at}.
        """
        cutoff_ts = published_after.timestamp()

        results = await self._client.search(
            collection_name=self._collection,
            query_vector=embedding,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="published_at_ts",
                        range=Range(gte=cutoff_ts),
                    )
                ]
            ),
            limit=top_k,
            with_payload=True,
        )

        return [
            {
                "score": hit.score,
                "url": hit.payload.get("url", ""),
                "title": hit.payload.get("title", ""),
                "published_at": hit.payload.get("published_at", ""),
            }
            for hit in results
        ]

    async def search_notion(
        self,
        embedding: list[float],
        hours: int = 24,
        top_k: int = 5,
    ) -> list[dict]:
        """
        Search the Notion dedup collection for similar articles.
        Filters by created_at_ts within the last `hours` hours so old entries
        are automatically excluded even if they haven't been cleaned up yet.
        Returns list of {score, title, page_id}.
        """
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()

        results = await self._client.search(
            collection_name=self._collection,
            query_vector=embedding,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="created_at_ts",
                        range=Range(gte=cutoff_ts),
                    )
                ]
            ),
            limit=top_k,
            with_payload=True,
        )

        return [
            {
                "score": hit.score,
                "title": hit.payload.get("title", ""),
                "page_id": hit.payload.get("page_id", ""),
            }
            for hit in results
        ]

    async def scroll_all_ids(self) -> set[str]:
        """
        Return the set of all page_id payload values currently in the collection.
        Used to diff against the current Notion 'waiting for post' set.
        """
        ids: set[str] = set()
        offset = None

        while True:
            results, next_offset = await self._client.scroll(
                collection_name=self._collection,
                offset=offset,
                limit=100,
                with_payload=["page_id"],
                with_vectors=False,
            )
            for point in results:
                pid = (point.payload or {}).get("page_id")
                if pid:
                    ids.add(pid)
            if next_offset is None:
                break
            offset = next_offset

        return ids

    async def delete_by_page_ids(self, page_ids: set[str]) -> None:
        """Delete all points whose payload page_id is in the given set."""
        if not page_ids:
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="page_id",
                        match=MatchAny(any=list(page_ids)),
                    )
                ]
            ),
        )

    async def upsert_notion(
        self,
        page_id: str,
        embedding: list[float],
        title: str,
        created_at: datetime,
    ) -> None:
        """Insert or update a Notion article embedding in the Notion dedup collection."""
        import hashlib
        point_id = int(hashlib.md5(page_id.encode()).hexdigest()[:16], 16) % (2**63)

        await self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "page_id": page_id,
                        "title": title,
                        "created_at_ts": created_at.timestamp(),
                    },
                )
            ],
        )

    async def upsert(
        self,
        article_id: str,
        embedding: list[float],
        url: str,
        title: str,
        published_at: datetime,
    ) -> None:
        """Insert or update an article embedding in Qdrant."""
        # Use a stable integer ID derived from the url_hash
        # Qdrant requires integer or UUID point IDs
        import hashlib
        point_id = int(hashlib.md5(article_id.encode()).hexdigest()[:16], 16) % (2**63)

        await self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "article_id": article_id,
                        "url": url,
                        "title": title,
                        "published_at": published_at.isoformat(),
                        "published_at_ts": published_at.timestamp(),
                    },
                )
            ],
        )
