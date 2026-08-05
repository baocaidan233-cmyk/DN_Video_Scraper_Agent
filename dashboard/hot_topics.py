"""
Hot Topics store — persists keywords and semantic threshold to data/hot_topics.json.
Used by the DailyNews pipeline to prioritise article selection:
  - Keyword match: case-insensitive substring of title/description
  - Semantic match: cosine similarity of article embedding vs embedded keywords
  - Both methods are combined (union); among matches, pick highest-scored article.
  - Fallback: pick highest-scored article overall when no matches.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "data/hot_topics.json"
_DEFAULT_THRESHOLD = 0.75  # cosine similarity threshold for semantic matching


class HotTopicsStore:
    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        # In-memory embedding cache — invalidated whenever keywords change
        self._emb_cache: list[list[float]] | None = None
        self._emb_cache_for: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("HotTopicsStore: failed to save: %s", e)
            raise

    # ------------------------------------------------------------------ #
    # Keywords                                                             #
    # ------------------------------------------------------------------ #

    def get_keywords(self) -> list[str]:
        """Return current keyword list (empty list if file missing or unreadable)."""
        return self._load().get("keywords", [])

    def set_keywords(self, keywords: list[str]) -> None:
        """Persist keyword list and invalidate the embedding cache."""
        data = self._load()
        data["keywords"] = keywords
        self._save(data)
        self._emb_cache = None          # embeddings are now stale
        self._emb_cache_for = ()

    # ------------------------------------------------------------------ #
    # Semantic threshold                                                   #
    # ------------------------------------------------------------------ #

    def get_semantic_threshold(self) -> float:
        return float(self._load().get("semantic_threshold", _DEFAULT_THRESHOLD))

    def set_semantic_threshold(self, threshold: float) -> None:
        data = self._load()
        data["semantic_threshold"] = threshold
        self._save(data)

    # ------------------------------------------------------------------ #
    # Embedding cache (in-memory only, keyed by keyword tuple)            #
    # ------------------------------------------------------------------ #

    def get_embedding_cache(self) -> tuple[list[str], list[list[float]] | None]:
        """
        Returns (current_keywords, cached_embeddings_or_None).
        None means the caller must (re-)compute the embeddings.
        """
        keywords = self.get_keywords()
        key = tuple(keywords)
        if key == self._emb_cache_for and self._emb_cache is not None:
            return keywords, self._emb_cache
        return keywords, None

    def set_embedding_cache(self, keywords: list[str], embeddings: list[list[float]]) -> None:
        self._emb_cache_for = tuple(keywords)
        self._emb_cache = embeddings
