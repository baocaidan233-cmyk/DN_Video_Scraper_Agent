"""
Similarity Agent — faithful Python port of 'similarity check flow v3'.

Two-stage deduplication:
  Stage 1: Within-batch cosine similarity (threshold 0.70)
           Incremental row-by-row dot product (memory efficient, no full N×N matrix)
  Stage 2: Cross-batch Qdrant vector DB search (threshold 0.80, 48h window)
           Survivors are upserted to Qdrant for future cross-batch checks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import numpy as np

from core.config import QdrantConfig
from core.models import Article
from services.openai_client import OpenAIClient
from services.qdrant_client import QdrantWrapper

logger = logging.getLogger(__name__)


def _cosine_within_batch(
    embeddings: np.ndarray, threshold: float
) -> list[int]:
    """
    Incremental cosine dedup — O(n) memory vs O(n²) for full matrix.
    Returns indices of articles to KEEP.
    Replicates BatchDedup node logic (threshold 0.70).
    """
    if len(embeddings) == 0:
        return []

    # Normalize all vectors once
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1e-9, norms)  # avoid div-by-zero
    normed = embeddings / norms

    keep_indices: list[int] = []
    kept_vectors: list[np.ndarray] = []

    for i, vec in enumerate(normed):
        if kept_vectors:
            kept_matrix = np.array(kept_vectors)  # shape: (k, dim)
            sims = kept_matrix @ vec               # dot products
            if float(np.max(sims)) >= threshold:
                continue  # too similar to a kept article — drop
        keep_indices.append(i)
        kept_vectors.append(vec)

    return keep_indices


class SimilarityAgent:
    def __init__(
        self,
        openai: OpenAIClient,
        qdrant: QdrantWrapper,
        qdrant_config: QdrantConfig,
        state=None,  # DashboardState — if set, emits dedup log events to the UI
    ) -> None:
        self._openai = openai
        self._qdrant = qdrant
        self._config = qdrant_config
        self._state = state

    async def _emit_log(self, level: str, msg: str) -> None:
        """Emit a log event to the dashboard UI if state is configured."""
        if self._state is not None:
            await self._state.emit({"type": "log", "level": level, "msg": msg})

    # ------------------------------------------------------------------ #
    # Stage methods (called individually by Pipeline for SSE granularity) #
    # ------------------------------------------------------------------ #

    async def run_embed(self, articles: list[Article]) -> list[list[float]]:
        """
        Stage 0: Generate OpenAI embeddings for all articles.
        Returns raw embedding list (parallel to articles).
        Raises on failure so the caller can decide whether to pass through.
        """
        texts = [f"{a.title} {a.description or ''}" for a in articles]
        return await self._openai.embed_texts(texts)

    async def run_within_batch(
        self,
        articles: list[Article],
        raw_embeddings: list[list[float]],
    ) -> tuple[list[Article], list[list[float]]]:
        """
        Stage 1: Within-batch cosine dedup (threshold 0.70).
        Returns (survivors, their_embeddings).
        """
        emb_array = np.array(raw_embeddings, dtype=np.float32)
        keep_indices = _cosine_within_batch(emb_array, self._config.within_batch_threshold)
        logger.info(
            "Similarity stage 1: %d → %d articles (within-batch threshold %.2f)",
            len(articles), len(keep_indices), self._config.within_batch_threshold,
        )
        kept_articles = [articles[i] for i in keep_indices]
        kept_embeddings = [raw_embeddings[i] for i in keep_indices]
        return kept_articles, kept_embeddings

    async def run_cross_batch(
        self,
        articles: list[Article],
        raw_embeddings: list[list[float]],
    ) -> list[Article]:
        """
        Stage 2: Cross-batch Qdrant dedup (threshold 0.80, 48h window).
        Survivors are upserted to Qdrant. Returns unique articles.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._config.cross_batch_hours)
        survivors: list[Article] = []

        for article, embedding in zip(articles, raw_embeddings):
            # Search with up to 2 retries before treating as fail-open
            similar = []
            last_search_exc: Exception | None = None
            for attempt in range(3):
                try:
                    similar = await self._qdrant.search_similar(embedding, cutoff, top_k=5)
                    last_search_exc = None
                    break
                except Exception as e:
                    last_search_exc = e
                    if attempt < 2:
                        await asyncio.sleep(1)
            if last_search_exc is not None:
                logger.warning("Qdrant search failed for %s after 3 attempts: %s — keeping article", article.url_hash, last_search_exc)
                await self._emit_log(
                    "WARNING",
                    f"[Dedup] Qdrant search FAILED after 3 attempts for {article.url_hash} "
                    f"({article.title[:55]}) — passed through (fail-open): {last_search_exc}",
                )

            best_score = max((s["score"] for s in similar), default=0.0)
            if best_score >= self._config.cross_batch_threshold:
                matched = next((s for s in similar if s["score"] == best_score), {})
                logger.debug(
                    "Cross-batch dup dropped: %s (score=%.3f, matched=%s)",
                    article.url_hash, best_score, matched.get("url", ""),
                )
                await self._emit_log(
                    "WARNING",
                    f"[Dedup] DROP '{article.title[:55]}' "
                    f"score={best_score:.3f} matched={matched.get('url', '?')[:80]}",
                )
                article.is_duplicate = True
                article.cross_batch_score = best_score
                article.cross_batch_matched_url = matched.get("url")
                continue

            article.embedding = embedding
            article.cross_batch_score = best_score
            survivors.append(article)

            if best_score > 0.0:
                await self._emit_log(
                    "INFO",
                    f"[Dedup] PASS '{article.title[:55]}' "
                    f"best_score={best_score:.3f} (threshold={self._config.cross_batch_threshold:.2f})",
                )

            # Upsert with up to 2 retries before logging failure
            last_upsert_exc: Exception | None = None
            for attempt in range(3):
                try:
                    pub_dt = article.published_at
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    await self._qdrant.upsert(
                        article_id=article.url_hash,
                        embedding=embedding,
                        url=article.url,
                        title=article.title,
                        published_at=pub_dt,
                    )
                    last_upsert_exc = None
                    break
                except Exception as e:
                    last_upsert_exc = e
                    if attempt < 2:
                        await asyncio.sleep(1)
            if last_upsert_exc is not None:
                logger.warning("Qdrant upsert failed for %s after 3 attempts: %s", article.url_hash, last_upsert_exc)
                await self._emit_log(
                    "WARNING",
                    f"[Dedup] Qdrant upsert FAILED after 3 attempts for {article.url_hash} "
                    f"({article.title[:55]}): {last_upsert_exc}",
                )

        logger.info(
            "Similarity stage 2: %d → %d articles (cross-batch threshold %.2f)",
            len(articles), len(survivors), self._config.cross_batch_threshold,
        )
        return survivors

    # ------------------------------------------------------------------ #
    # Convenience wrapper (used by tests / any caller that doesn't need   #
    # per-stage SSE events)                                               #
    # ------------------------------------------------------------------ #

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Public helper — embed arbitrary texts with the pipeline's OpenAI client."""
        return await self._openai.embed_texts(texts)

    def set_thresholds(self, within_batch: float, cross_batch: float) -> None:
        """Update dedup thresholds at runtime (called by dashboard API)."""
        self._config = self._config.model_copy(update={
            "within_batch_threshold": within_batch,
            "cross_batch_threshold": cross_batch,
        })
        logger.info(
            "SimilarityAgent: thresholds updated — within=%.2f cross=%.2f",
            within_batch, cross_batch,
        )

    async def run(self, articles: list[Article]) -> list[Article]:
        """Run all stages in sequence. Returns unique articles."""
        if not articles:
            return []
        try:
            embeddings = await self.run_embed(articles)
        except Exception as e:
            logger.error("Embedding generation failed: %s — skipping similarity check", e)
            return articles
        articles, embeddings = await self.run_within_batch(articles, embeddings)
        articles = await self.run_cross_batch(articles, embeddings)
        return articles
