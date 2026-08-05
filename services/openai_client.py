"""
OpenAI embeddings client — async, batched, with retry.
Used by SimilarityAgent for text-embedding-3-small.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Sequence

from openai import AsyncOpenAI

from core.config import OpenAIConfig

logger = logging.getLogger(__name__)


class OpenAIClient:
    def __init__(self, config: OpenAIConfig) -> None:
        self._config = config
        self._client = AsyncOpenAI(api_key=config.api_key)

    async def chat_complete(self, system: str, user: str, max_tokens: int = 300, temperature: float = 0.1, model: str | None = None) -> str:
        """Single chat completion. Defaults to post_gen_model."""
        response = await self._client.chat.completions.create(
            model=model or self._config.post_gen_model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            timeout=60,
        )
        return response.choices[0].message.content.strip()

    def _needs_translation(self, text: str) -> bool:
        """True if >15% of alphabetic characters are outside Latin script (U+0000–U+024F)."""
        alpha = [c for c in text if c.isalpha()]
        if not alpha:
            return False
        non_latin = sum(1 for c in alpha if ord(c) > 0x024F)
        return non_latin / len(alpha) > 0.15

    async def translate_to_english(self, texts: list[str]) -> list[str]:
        """
        Translate any non-Latin-script texts to English in a single batched API call.
        Already-English (Latin-script) texts are returned unchanged.
        Fail-open: on any error returns the originals.
        """
        indices = [i for i, t in enumerate(texts) if self._needs_translation(t)]
        if not indices:
            return texts

        to_translate = [texts[i] for i in indices]
        numbered = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(to_translate))
        prompt = (
            "Translate each text to English. "
            "Return ONLY a JSON array of translated strings, same order, no extra text.\n\n"
            + numbered
        )
        try:
            raw = await self.chat_complete(
                system="You are a translator. Output only valid JSON.",
                user=prompt,
                max_tokens=2000,
                temperature=0.0,
            )
            translated: list[str] = json.loads(raw)
            result = list(texts)
            for idx, eng in zip(indices, translated):
                result[idx] = eng
            logger.info(
                "Translated %d non-English text(s) before embedding", len(indices)
            )
            return result
        except Exception as e:
            logger.warning("translate_to_english failed: %s — using originals", e)
            return texts

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for a list of texts.
        Non-Latin-script texts are translated to English first so embeddings
        are comparable across languages.
        Processes in batches of embedding_batch_size with retry on failure.
        Returns list of embedding vectors in the same order as input.
        """
        texts = await self.translate_to_english(texts)
        all_embeddings: list[list[float]] = []
        batch_size = self._config.embedding_batch_size

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embedding = await self._embed_with_retry(batch)
            all_embeddings.extend(embedding)

        return all_embeddings

    async def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        last_err: Exception | None = None
        for attempt in range(self._config.max_retries):
            try:
                response = await self._client.embeddings.create(
                    model=self._config.embedding_model,
                    input=texts,
                    timeout=60,
                )
                # Sort by index to preserve order
                sorted_data = sorted(response.data, key=lambda d: d.index)
                return [d.embedding for d in sorted_data]
            except Exception as e:
                last_err = e
                wait = self._config.retry_delay_s * (2 ** attempt)
                logger.warning("OpenAI embed attempt %d failed: %s — retrying in %.1fs", attempt + 1, e, wait)
                await asyncio.sleep(wait)

        raise RuntimeError(f"OpenAI embedding failed after {self._config.max_retries} attempts: {last_err}")
