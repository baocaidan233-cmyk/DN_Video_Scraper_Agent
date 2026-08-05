"""
EditorReviewClient — DailyNews-only editor review branch (A/B experiment).

Runs a three-prompt editorial chain over a scored article and produces a *finished
Gettr post* (article.editor_post), which PublishAgent twins to the test Gettr account
alongside the standard post on the live account.

The chain output is used verbatim — it is deliberately NOT re-run through
ClaudeClient.generate_post. The editor prompts carry their own voice, structure and
length rules; a fourth rewrite under generate_post_system.txt strips the closing
device and the prosecutorial voice, which is exactly what the A/B is measuring.

Chain:
  1. intake triage      → structured brief + a QUALIFIES: yes/no gate
                          (no = no variant, article unaffected)
  2. CCP exposure       → drafts from the brief, with the source as fact reference
  3. ChinaX house style → final style pass

The triage prompt is written to hand its brief "unmodified to the drafting editor", so
the brief is the CCP step's PRIMARY input and the source article is attached beneath it
as a fact/quote reference — not the other way round.

Fails open in the safe direction: an unparseable triage verdict is treated as qualifying,
and any exception anywhere in the chain simply means "no editor variant for this
article" — ingestion and the live channel are never affected.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import anthropic

from core.models import Article

logger = logging.getLogger(__name__)

# The triage prompt's output contract (prompts/ai_editor_intake_triage_prompt.md):
#   QUALIFIES: yes    → build the variant
#   QUALIFIES: no     → story lacks a named actor / hard number / self-indictment angle
# Tolerates "**QUALIFIES:** No", "qualifies：yes", and a trailing explanation clause.
_QUALIFIES_RE = re.compile(r"QUALIFIES\s*[:：]\s*\**\s*(yes|no)\b", re.IGNORECASE)

# How far past the CCP draft's word count the style pass may go before it is retried.
_STYLE_LENGTH_TOLERANCE = 1.15


class EditorReviewClient:
    def __init__(self, config, claude_config, openai_client=None) -> None:
        self._config = config
        self._claude_config = claude_config
        self._openai = openai_client
        self._client = anthropic.Anthropic(api_key=claude_config.api_key)
        self._triage_prompt: str = ""
        self._ccp_prompt: str = ""
        self._style_prompt: str = ""
        self.reload_prompts()

    # ------------------------------------------------------------------ #
    # Config / prompt loading                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("EditorReviewClient: could not load prompt %s: %s", path, e)
            return ""

    def reload_prompts(self) -> None:
        """Re-read the three editor prompts from disk. Called by the dashboard on save."""
        self._triage_prompt = self._load(self._config.triage_prompt)
        self._ccp_prompt = self._load(self._config.ccp_prompt)
        self._style_prompt = self._load(self._config.style_prompt)
        logger.info(
            "EditorReviewClient: prompts reloaded (triage=%d ccp=%d style=%d chars)",
            len(self._triage_prompt), len(self._ccp_prompt), len(self._style_prompt),
        )

    def reload_config(self, config) -> None:
        """Hot-swap the editor_review config block (enabled / max_per_run / prompt paths)."""
        self._config = config
        self.reload_prompts()

    @property
    def enabled(self) -> bool:
        return bool(
            self._config.enabled
            and self._ccp_prompt
            and self._style_prompt
        )

    @property
    def max_per_run(self) -> int:
        return max(0, self._config.max_per_run)

    # ------------------------------------------------------------------ #
    # LLM plumbing (mirrors ClaudeClient: OpenAI first, Anthropic fallback) #
    # ------------------------------------------------------------------ #

    async def _complete(self, system: str, user: str) -> Optional[str]:
        max_tokens = self._config.max_tokens
        if self._openai:
            return await self._openai.chat_complete(
                system=system, user=user, max_tokens=max_tokens,
                temperature=0.3, model=self._config.model,
            )
        message = await asyncio.to_thread(
            self._client.messages.create,
            model=self._config.model or self._claude_config.scoring_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text.strip()

    async def _style_pass(self, title: str, draft: str) -> Optional[str]:
        """
        Step 3 — apply the house voice without changing the length.

        The style prompt is a *descriptive* document (reverse-engineered from the
        reference account, which posts ~150–300 word 4-paragraph briefs), so used as a
        system prompt it expands a compact draft to long form — measured at 78 → 161
        words. These per-call rules plus one retry keep it a voice pass, leaving the
        length target owned by the CCP prompt.

        The ceiling is the draft's own word count rather than a hardcoded number: change
        the target in the CCP prompt and this follows automatically.
        """
        draft_words = len(draft.split())

        def _msg(feedback: str = "") -> str:
            return (
                "Apply the house voice described in your instructions to the draft below.\n"
                "This is a STYLE PASS ONLY:\n"
                f"- The draft is {draft_words} words. Your output must be no longer than "
                f"{draft_words} words. Rewording is expected; expanding is not.\n"
                "- Keep the paragraph count. Do NOT expand it into the long-form "
                "4-paragraph brief — this is a compact social post, not a newsletter piece.\n"
                "- Do not add facts, figures, names, dates or citations that are not "
                "already in the draft.\n"
                "- Return only the styled post text, with no preamble or commentary.\n"
                f"{feedback}\n"
                f"Title: {title}\n\nDraft:\n{draft}"
            )

        styled = await self._complete(self._style_prompt, _msg())
        if not styled or not styled.strip():
            return None

        # One retry when the pass padded materially past the draft, mirroring the
        # retry in ClaudeClient.generate_post. As there, an over-length retry result
        # is used anyway — never trimmed mid-sentence.
        n = len(styled.split())
        if n > draft_words * _STYLE_LENGTH_TOLERANCE:
            logger.debug(
                "Editor style pass expanded %d → %d words — retrying", draft_words, n,
            )
            retry = await self._complete(
                self._style_prompt,
                _msg(
                    f"\nYour previous attempt was {n} words — too long. Here it is:\n"
                    f"{styled.strip()}\n\n"
                    f"Rewrite it to at most {draft_words} words. Same voice, same facts, "
                    f"no new material. Count the words before answering.\n"
                ),
            )
            if retry and retry.strip():
                retry_n = len(retry.split())
                if retry_n > draft_words * _STYLE_LENGTH_TOLERANCE:
                    logger.info(
                        "Editor style pass still %d words after retry (draft %d) — using anyway",
                        retry_n, draft_words,
                    )
                styled = retry
        return styled.strip()

    # ------------------------------------------------------------------ #
    # The chain                                                           #
    # ------------------------------------------------------------------ #

    async def revise(self, article: Article, claude_client) -> bool:
        """
        Run the editor chain over one article. Sets article.editor_post on success.
        Returns True if a variant post was produced.

        claude_client is used for body resolution so the scrape is shared with post_gen.
        """
        try:
            body = await claude_client.resolve_body(article)
            if not body:
                return False
            body = body[:4000]
            source = f"Title: {article.title}\n\nArticle:\n{body}"

            # Step 1 — intake triage: produces the brief and the QUALIFIES gate.
            # Fails open — an unreadable verdict is treated as qualifying.
            brief = ""
            if self._triage_prompt:
                triage_out = (await self._complete(self._triage_prompt, source) or "").strip()
                match = _QUALIFIES_RE.search(triage_out)
                if match and match.group(1).lower() == "no":
                    logger.info(
                        "Editor triage: does not qualify — no variant for '%s'",
                        article.title[:70],
                    )
                    return False
                if not match:
                    logger.debug(
                        "Editor triage verdict unparseable for '%s' — treating as qualifying",
                        article.title[:70],
                    )
                brief = triage_out

            # Step 2 — CCP exposure draft. The triage brief is the primary input (the
            # triage prompt is written to hand it "unmodified to the drafting editor");
            # the source is attached underneath so facts and quotes stay checkable.
            if brief:
                ccp_input = (
                    "EDITOR BRIEF (from intake triage — draft from this):\n"
                    f"{brief}\n\n"
                    "SOURCE ITEM (reference only — for facts, quotes and figures):\n"
                    f"{source}"
                )
            else:
                ccp_input = source
            draft = await self._complete(self._ccp_prompt, ccp_input)
            if not draft or not draft.strip():
                logger.warning("Editor CCP step returned nothing for %s", article.url)
                return False

            # Step 3 — house style pass. Its output IS the post that gets published.
            draft = draft.strip()
            styled = await self._style_pass(article.title, draft)
            revised = (styled or draft).strip()
            if not revised:
                return False

            article.editor_post = revised
            logger.info(
                "Editor variant ready for '%s' (%d words)",
                article.title[:60], len(revised.split()),
            )
            return True
        except Exception as e:
            logger.warning(
                "Editor review failed for %s: %s — no variant, live post unaffected",
                article.url, e,
            )
            return False

    async def revise_batch(self, articles: list[Article], claude_client, concurrency: int = 3) -> int:
        """Revise articles concurrently. Returns the number that produced a variant."""
        sem = asyncio.Semaphore(concurrency)

        async def _one(article: Article) -> bool:
            async with sem:
                return await self.revise(article, claude_client)

        results = await asyncio.gather(*[_one(a) for a in articles])
        return sum(1 for r in results if r)
