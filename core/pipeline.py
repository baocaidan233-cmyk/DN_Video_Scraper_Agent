"""
Pipeline Orchestrator — ties RSS → Similarity → Claude → Review together.
Called by the rss_loop in main.py every 10 minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from agents.rss_agent import RssAgent
from agents.similarity_agent import SimilarityAgent
from core.config import ClaudeConfig, GemmaConfig
from services.claude_client import ClaudeClient
from services.notion_client import NotionClient
from pathlib import Path

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        rss_agent: Optional[RssAgent],
        similarity_agent: SimilarityAgent,
        claude_client: ClaudeClient,
        claude_config: ClaudeConfig,
        notion_client: Optional[NotionClient],
        review_agent,  # ReviewAgent — imported at runtime to avoid circular import
        state=None,    # DashboardState (optional)
        source_agents: list | None = None,           # replaces rss_agent for socials pipeline
        sources_md_path: str | None = None,           # .md file parsed each cycle
        video_score_boost: float = 0.0,               # added to llm_score when has_video=True
        score_prompt_override: str | None = None,     # alternate scoring prompt file path
        post_prompt_override: str | None = None,      # alternate post generation system prompt file path
        post_user_template_override: str | None = None,  # alternate post user template file path
        hot_topics_store=None,  # HotTopicsStore — DailyNews only; triggers single-card-per-run mode
        always_rewrite: bool = False,  # always send post through LLM rewrite (skip short-English bypass)
        gemma_client=None,  # GemmaClient — DailyNews only; content verification before Telegram enqueue
        run_type_override: Optional[str] = None,  # history DB run_type/run_id prefix; None → "epicfury"/"rss"
        editor_client=None,  # EditorReviewClient — DailyNews only; A/B variant generation
    ) -> None:
        self._rss = rss_agent
        self._similarity = similarity_agent
        self._claude = claude_client
        self._claude_config = claude_config
        self._notion = notion_client
        self._review = review_agent
        self._state = state
        self._source_agents = source_agents or []
        self._sources_md_path = sources_md_path
        self._video_score_boost = video_score_boost
        self._hot_topics_store = hot_topics_store
        self._always_rewrite = always_rewrite
        self._gemma = gemma_client
        self._editor = editor_client
        self._run_type_override = run_type_override
        self._lock = asyncio.Lock()  # prevents concurrent runs

        # Paths stored for hot-reload
        self._score_prompt_override_path = score_prompt_override
        self._post_prompt_override_path = post_prompt_override
        self._post_user_template_override_path = post_user_template_override

        # Load override texts from disk
        self._score_prompt_text: Optional[str] = self._load_file(score_prompt_override)
        self._post_prompt_text: Optional[str] = self._load_file(post_prompt_override)
        self._post_user_template_text: Optional[str] = self._load_file(post_user_template_override)

    def _load_file(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("Could not load prompt file %s: %s", path, e)
            return None

    def reload_prompts(self) -> None:
        """Re-read all override prompt files from disk. Called by dashboard on save."""
        self._score_prompt_text = self._load_file(self._score_prompt_override_path)
        self._post_prompt_text = self._load_file(self._post_prompt_override_path)
        self._post_user_template_text = self._load_file(self._post_user_template_override_path)
        if self._editor:
            self._editor.reload_prompts()
        logger.info("Pipeline: prompt overrides reloaded")

    async def _rank_articles(self, articles: list) -> list:
        """
        Return articles ordered by priority for sequential post_gen + verify attempts.

        Hot-topic matches (keyword + semantic) come first, sorted by llm_score desc.
        Non-matches follow, sorted by llm_score desc.
        If no hot_topics_store or no keywords, returns all sorted by llm_score desc.
        All candidates have already passed the filter_score_threshold.
        """
        if not self._hot_topics_store:
            ranked = sorted(articles, key=lambda a: a.llm_score, reverse=True)
            logger.info(
                "Pipeline: ranked %d candidates by score, top: '%s' (%.1f)",
                len(ranked), ranked[0].title[:60] if ranked else "", ranked[0].llm_score if ranked else 0,
            )
            return ranked

        raw_keywords = self._hot_topics_store.get_keywords()
        keywords = [k.strip() for k in raw_keywords if k.strip()]

        if not keywords:
            ranked = sorted(articles, key=lambda a: a.llm_score, reverse=True)
            logger.info(
                "Pipeline: no hot topics — ranked %d candidates by score, top: '%s' (%.1f)",
                len(ranked), ranked[0].title[:60] if ranked else "", ranked[0].llm_score if ranked else 0,
            )
            return ranked

        matched_ids: set = set()

        # --- 1. Keyword substring matching ---
        kws_lower = [k.lower() for k in keywords]
        for a in articles:
            text = f"{a.title} {a.description or ''}".lower()
            if any(k in text for k in kws_lower):
                matched_ids.add(id(a))

        # --- 2. Semantic matching ---
        articles_with_emb = [a for a in articles if a.embedding]
        if articles_with_emb:
            threshold = self._hot_topics_store.get_semantic_threshold()

            cached_kws, cached_embs = self._hot_topics_store.get_embedding_cache()
            if cached_embs is None:
                try:
                    cached_embs = await self._similarity.embed_texts(keywords)
                    self._hot_topics_store.set_embedding_cache(keywords, cached_embs)
                    logger.debug("Pipeline: embedded %d hot topic keywords", len(keywords))
                except Exception as e:
                    logger.warning("Pipeline: hot topic embedding failed: %s — skipping semantic match", e)
                    cached_embs = []

            if cached_embs:
                topic_matrix = np.array(cached_embs, dtype=np.float32)
                t_norms = np.linalg.norm(topic_matrix, axis=1, keepdims=True)
                t_norms = np.where(t_norms == 0, 1e-9, t_norms)
                topic_normed = topic_matrix / t_norms

                for a in articles_with_emb:
                    art_vec = np.array(a.embedding, dtype=np.float32)
                    art_norm = float(np.linalg.norm(art_vec))
                    if art_norm == 0:
                        continue
                    art_normed = art_vec / art_norm
                    sims = topic_normed @ art_normed
                    if float(np.max(sims)) >= threshold:
                        matched_ids.add(id(a))

        matched   = sorted([a for a in articles if id(a) in matched_ids],     key=lambda a: a.llm_score, reverse=True)
        unmatched = sorted([a for a in articles if id(a) not in matched_ids], key=lambda a: a.llm_score, reverse=True)
        ranked = matched + unmatched

        if matched:
            logger.info(
                "Pipeline: ranked %d candidates — %d hot topic match(es), top: '%s' (%.1f)",
                len(ranked), len(matched), ranked[0].title[:60], ranked[0].llm_score,
            )
        else:
            logger.info(
                "Pipeline: no hot topic matches — ranked %d candidates by score, top: '%s' (%.1f)",
                len(ranked), ranked[0].title[:60] if ranked else "", ranked[0].llm_score if ranked else 0,
            )
        return ranked

    def set_filter_score_threshold(self, threshold: float) -> None:
        """Update the scoring threshold at runtime (called by dashboard API)."""
        self._claude_config = self._claude_config.model_copy(
            update={"filter_score_threshold": threshold}
        )
        logger.info("Pipeline: filter_score_threshold updated to %.1f", threshold)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _emit(self, event: dict) -> None:
        if self._state:
            await self._state.emit(event)

    async def _step_start(self, run_id: str, step: str) -> None:
        await self._emit({"type": "step_start", "run_id": run_id, "step": step})

    async def _step_done(
        self,
        run_id: str,
        step: str,
        articles_in: int,
        articles_out: int,
        duration_ms: int,
    ) -> None:
        await self._emit({
            "type": "step_done",
            "run_id": run_id,
            "step": step,
            "articles_in": articles_in,
            "articles_out": articles_out,
            "articles_dropped": articles_in - articles_out,
            "duration_ms": duration_ms,
        })
        # Persist to SQLite
        try:
            from dashboard import db
            await db.save_step(
                run_id=run_id,
                step_name=step,
                articles_in=articles_in,
                articles_out=articles_out,
                articles_dropped=articles_in - articles_out,
                duration_ms=duration_ms,
            )
        except Exception:
            pass

    async def _step_error(self, run_id: str, step: str, error: Exception) -> None:
        await self._emit({"type": "step_error", "run_id": run_id, "step": step, "error": str(error)})
        try:
            from dashboard import db
            await db.save_step(run_id=run_id, step_name=step, error_msg=str(error))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Main ingestion cycle                                                 #
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def run_ingestion(self, run_id: Optional[str] = None) -> None:
        """Full ingestion cycle: RSS → dedup → score → enqueue for review."""
        if self._lock.locked():
            logger.warning("Pipeline: run_ingestion skipped — already running")
            return

        async with self._lock:
            await self._run_ingestion_inner(run_id)

    def _is_cancelled(self) -> bool:
        return bool(self._state and self._state.rss_cancel_event.is_set())

    async def _run_ingestion_inner(self, run_id: Optional[str] = None) -> None:
        socials_mode = bool(self._source_agents)

        if run_id is None:
            prefix = self._run_type_override or ("ef" if socials_mode else "rss")
            run_id = f"{prefix}-{uuid.uuid4().hex[:12]}"

        # Clear any leftover cancel signal from a previous run
        if self._state:
            self._state.rss_cancel_event.clear()

        started_at = datetime.now(timezone.utc).isoformat()
        t_total = time.monotonic()
        run_type = self._run_type_override or ("epicfury" if socials_mode else "rss")
        logger.info("Pipeline: starting ingestion cycle run_id=%s type=%s", run_id, run_type)

        await self._emit({"type": "run_start", "run_id": run_id, "run_type": run_type, "ts": started_at})

        try:
            from dashboard import db
            await db.save_run_start(run_id, run_type, started_at)
        except Exception:
            pass

        try:
            if socials_mode:
                # --- Socials (Epic Fury) mode: source_agents replace RSS+Notion ---
                x_handles: list[str] = []
                website_urls: list[str] = []
                if self._sources_md_path:
                    from agents.source_reader import parse_sources
                    x_handles, website_urls = parse_sources(self._sources_md_path)
                    logger.info(
                        "Pipeline: sources loaded — %d X handles, %d websites",
                        len(x_handles), len(website_urls),
                    )

                # Emit no-op for notion_fetch (skipped in socials mode)
                await self._step_start(run_id, "notion_fetch")
                await self._step_done(run_id, "notion_fetch", 0, 0, 0)

                # Run all source agents and merge results
                await self._step_start(run_id, "sources_fetch")
                t0 = time.monotonic()
                all_articles: list = []
                raw_count_total = 0
                for agent in self._source_agents:
                    try:
                        agent_articles, agent_raw = await agent.run(x_handles, website_urls)
                        all_articles.extend(agent_articles)
                        raw_count_total += agent_raw
                    except Exception as e:
                        logger.warning("Pipeline: source agent %s failed: %s", type(agent).__name__, e)

                articles = all_articles
                raw_count = raw_count_total
                await self._step_done(
                    run_id, "sources_fetch",
                    raw_count, len(articles),
                    int((time.monotonic() - t0) * 1000),
                )

            else:
                # --- RSS mode: Notion + RssAgent ---
                await self._step_start(run_id, "notion_fetch")
                t0 = time.monotonic()
                sources = await self._notion.get_rss_sources()
                await self._step_done(run_id, "notion_fetch", 0, len(sources), int((time.monotonic() - t0) * 1000))

                if not sources:
                    logger.warning("Pipeline: no active RSS sources in Notion")
                    await self._finish_run(run_id, "success", t_total)
                    return

                if self._is_cancelled():
                    await self._finish_run(run_id, "cancelled", t_total)
                    return

                # Step 2a: Fetch feeds (raw) + time-filter
                await self._step_start(run_id, "rss_fetch")
                t0 = time.monotonic()
                articles, raw_count = await self._rss.run(sources)
                await self._step_done(run_id, "rss_fetch", len(sources), raw_count, int((time.monotonic() - t0) * 1000))

            if raw_count == 0 or not articles:
                logger.info("Pipeline: no new articles from sources")
                await self._finish_run(run_id, "success", t_total)
                return

            # Step 2b: URL hash dedup (already done inside agents, emit counts here)
            await self._step_start(run_id, "url_dedup")
            await self._step_done(run_id, "url_dedup", raw_count, len(articles), 0)

            if not articles:
                logger.info("Pipeline: all articles already seen (URL dedup)")
                await self._finish_run(run_id, "success", t_total)
                return

            if self._is_cancelled():
                await self._finish_run(run_id, "cancelled", t_total)
                return

            # Step 3: Claude quality scoring
            await self._step_start(run_id, "claude_score")
            t0 = time.monotonic()
            scored_articles = await self._claude.score_batch(
                articles,
                prompt_override=self._score_prompt_text,
            )

            # Apply video score boost (Epic Fury: +1.0 for video content)
            if self._video_score_boost > 0:
                for article in scored_articles:
                    if article.has_video:
                        article.llm_score = min(10.0, article.llm_score + self._video_score_boost)
                        logger.debug("Video boost applied: %s → %.1f", article.title[:60], article.llm_score)

            min_score = self._claude_config.filter_score_threshold
            qualifying = [a for a in scored_articles if a.llm_score >= min_score]
            await self._step_done(
                run_id, "claude_score",
                len(scored_articles), len(qualifying),
                int((time.monotonic() - t0) * 1000),
            )

            for article in scored_articles:
                if article.llm_score < min_score:
                    logger.debug(
                        "Article dropped (score %.1f < %.1f): %s",
                        article.llm_score, min_score, article.title[:60],
                    )

            if not qualifying:
                logger.info("Pipeline: no articles met min_score threshold")
                await self._finish_run(run_id, "success", t_total)
                return

            if self._is_cancelled():
                await self._finish_run(run_id, "cancelled", t_total)
                return

            # Step 3b: Editor review — DailyNews only, A/B variant generation.
            # Produces a finished article.editor_post; never filters `qualifying`, so a
            # triage rejection costs the live channel nothing.
            if not socials_mode and self._editor and self._editor.enabled:
                await self._step_start(run_id, "editor_review")
                t0 = time.monotonic()
                targets = (
                    qualifying[: self._editor.max_per_run]
                    if self._editor.max_per_run else qualifying
                )
                n_revised = await self._editor.revise_batch(targets, self._claude)
                logger.info(
                    "Pipeline: editor review revised %d/%d articles", n_revised, len(targets),
                )
                await self._step_done(
                    run_id, "editor_review", len(targets), n_revised,
                    int((time.monotonic() - t0) * 1000),
                )

            # Step 4: Post generation (both pipelines)
            await self._step_start(run_id, "post_gen")
            t0 = time.monotonic()
            n_before_post_gen = len(qualifying)
            logger.info("Pipeline: generating posts for %d qualifying articles", len(qualifying))
            await self._claude.generate_posts_batch(
                qualifying,
                system_prompt_override=self._post_prompt_text,
                user_template_override=self._post_user_template_text,
                always_rewrite=self._always_rewrite,
            )

            # DailyNews: no generated post means there is nothing to publish — drop here.
            # Carrying llm_post=None forward handed Gemma a title with no body (which it
            # auto-FAILs), so the real drop reason (LLM refusal, or an English source body
            # under 40 words) was hidden behind a verify_post failure and post_gen recorded
            # N->N. EpicFury is exempt: short tweets legitimately produce no post and publish
            # via the raw-title + OG-preview path (publish_agent.py:651).
            if not socials_mode:
                no_post = [a for a in qualifying if not (a.llm_post or "").strip()]
                if no_post:
                    logger.info(
                        "Pipeline: dropping %d article(s) with no generated post: %s",
                        len(no_post), "; ".join(a.title[:60] for a in no_post),
                    )
                    qualifying = [a for a in qualifying if (a.llm_post or "").strip()]

            await self._step_done(run_id, "post_gen", n_before_post_gen, len(qualifying), int((time.monotonic() - t0) * 1000))

            if not qualifying:
                logger.info("Pipeline: no articles produced a usable post")
                await self._finish_run(run_id, "success", t_total)
                return

            if self._is_cancelled():
                await self._finish_run(run_id, "cancelled", t_total)
                return

            # Step 4b: Gemma content verification — DailyNews only
            if not socials_mode:
                verify_enabled = not self._state or getattr(self._state, "verify_enabled", True)
                _do_verify = (
                    self._gemma
                    and getattr(self._gemma._config, "enabled", True)
                    and verify_enabled
                )
                if _do_verify:
                    await self._step_start(run_id, "verify_post")
                    t0 = time.monotonic()
                    n_before_verify = len(qualifying)
                    verified = []
                    for candidate in qualifying:
                        verdict, raw_output = await self._gemma.verify_post(candidate.title, candidate.llm_post)
                        candidate.verification_verdict = verdict
                        candidate.verification_output = raw_output
                        if verdict in ("PASS", "ERROR"):
                            if verdict == "ERROR":
                                logger.warning(
                                    "Pipeline: Gemma API error — passing '%s' through\n%s",
                                    candidate.title[:80], raw_output,
                                )
                            verified.append(candidate)
                        else:
                            logger.warning(
                                "Pipeline: content verification %s — dropping '%s'\n"
                                "--- Generated post ---\n%s\n"
                                "--- Gemma feedback ---\n%s",
                                verdict, candidate.title[:80],
                                candidate.llm_post, raw_output,
                            )
                    qualifying = verified
                    await self._step_done(run_id, "verify_post", n_before_verify, len(qualifying), int((time.monotonic() - t0) * 1000))

                if not qualifying:
                    logger.info("Pipeline: no articles passed content generation/verification")
                    await self._finish_run(run_id, "success", t_total)
                    return

            # Step 5a: Generate embeddings (only for qualifying articles with generated posts)
            await self._step_start(run_id, "embeddings")
            t0 = time.monotonic()
            try:
                raw_embeddings = await self._similarity.run_embed(qualifying)
            except Exception as e:
                await self._step_error(run_id, "embeddings", e)
                logger.error("Embedding generation failed: %s — skipping similarity check", e)
            else:
                await self._step_done(run_id, "embeddings", len(qualifying), len(qualifying), int((time.monotonic() - t0) * 1000))

                # Step 5b: Within-batch cosine dedup
                await self._step_start(run_id, "within_batch_dedup")
                t0 = time.monotonic()
                qualifying, batch_embeddings = await self._similarity.run_within_batch(qualifying, raw_embeddings)
                await self._step_done(run_id, "within_batch_dedup", len(raw_embeddings), len(qualifying), int((time.monotonic() - t0) * 1000))

                # Step 5c: Cross-batch Qdrant dedup
                await self._step_start(run_id, "cross_batch_dedup")
                t0 = time.monotonic()
                n_before_cross = len(qualifying)
                qualifying = await self._similarity.run_cross_batch(qualifying, batch_embeddings)
                await self._step_done(run_id, "cross_batch_dedup", n_before_cross, len(qualifying), int((time.monotonic() - t0) * 1000))

            if not qualifying:
                logger.info("Pipeline: all articles were duplicates")
                await self._finish_run(run_id, "success", t_total)
                return

            if self._is_cancelled():
                await self._finish_run(run_id, "cancelled", t_total)
                return

            # Step 6: Enqueue for Notion review
            await self._step_start(run_id, "telegram_enqueue")
            t0 = time.monotonic()
            enqueued = 0
            for article in qualifying:
                await self._review.enqueue_article(article)
                enqueued += 1
            await self._step_done(run_id, "telegram_enqueue", len(qualifying), enqueued, int((time.monotonic() - t0) * 1000))

            logger.info(
                "Pipeline: ingestion complete — %d scored, %d enqueued for review",
                len(scored_articles), enqueued,
            )

            await self._finish_run(run_id, "success", t_total)

        except Exception as e:
            logger.error("Pipeline error in run %s: %s", run_id, e)
            await self._emit({"type": "run_done", "run_id": run_id, "status": "failed", "error": str(e)})
            try:
                from dashboard import db
                await db.update_run(
                    run_id, "failed",
                    datetime.now(timezone.utc).isoformat(),
                    str(e),
                )
            except Exception:
                pass
            raise

    async def _finish_run(self, run_id: str, status: str, t_start: float) -> None:
        ended_at = datetime.now(timezone.utc).isoformat()
        total_ms = int((time.monotonic() - t_start) * 1000)
        await self._emit({
            "type": "run_done",
            "run_id": run_id,
            "status": status,
            "total_duration_ms": total_ms,
        })
        try:
            from dashboard import db
            await db.update_run(run_id, status, ended_at)
        except Exception:
            pass
