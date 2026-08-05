"""
Claude client for article quality scoring and Gettr post generation.
Uses claude-haiku-4-5 for scoring; post generation uses the same model
with the editorial prompt from V1.1_daily_news_rss workflow.

Prompts are loaded from the prompts/ directory at import time so they
can be edited without touching code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import aiohttp
import anthropic
import trafilatura

from core.config import ClaudeConfig, MetadataApiConfig
from core.models import Article

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


_ENGLISH_FUNCTION_WORDS = frozenset([
    "the", "is", "are", "was", "were", "has", "have", "had", "be", "been",
    "it", "its", "this", "that", "these", "those",
    "and", "or", "but", "not", "no",
    "in", "on", "at", "to", "of", "by", "with", "from", "for", "as", "after",
    "a", "an", "amid", "over", "into", "against", "about", "between",
    "he", "she", "they", "we", "his", "her", "their", "our",
    "said", "says", "told",
])


# Characters that carry no script information and so must never make English text
# look foreign.  Everything above U+024F has to be listed explicitly, because the
# ord() ceiling in _is_english() rejects the rest.  Typographic punctuation belongs
# here: RSS descriptions routinely contain \u2019 and \u2026, and treating those as non-Latin
# used to bypass the short-body guard at resolve_body(), sending 15-word stubs to
# post generation instead of dropping them.
_ALLOWED_NON_ALPHA = frozenset(
    ' \t\n\r'                                          # whitespace
    '0123456789'                                       # digits
    '.,;:!?-()[]{}"\'\\/@#$%^&*+=<>|~`'                # ASCII punctuation
    '\u2013\u2014\u2010\u2011\u2012\u2015'             # en/em dash, hyphens, figure dash, bar
    '\u201c\u201d\u2018\u2019\u201a\u201b'             # curly double + single quotes
    '\u2026\u2022\u2032\u2033\u2039\u203a'             # ellipsis, bullet, primes, angle quotes
    '\u20ac\u2122\u2020\u2021'                         # euro, trademark, daggers
    '\u200b\u200c\u200d\u200e\u200f\ufeff'             # zero-width joiners / marks / BOM
)


def _is_english(text: str) -> bool:
    """True only if every character is Latin-script (or whitespace/punctuation/digits).

    Any single CJK, Arabic, Cyrillic, or other non-Latin character causes False,
    ensuring mixed text (e.g. one Chinese name in an English sentence) is sent to
    the LLM for translation rather than passed through as-is.
    """
    if not text:
        return True
    for c in text:
        if c in _ALLOWED_NON_ALPHA:
            continue
        if ord(c) > 0x024F:  # outside Basic Latin + Latin Extended-A/B
            return False
    return True


# Any remaining HTML tags — safety net for description fallback or partial trafilatura output
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)

_JSONLD_RE = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.DOTALL | re.IGNORECASE,
)


def _jsonld_article_body(html: str | bytes) -> str:
    """Pull the longest JSON-LD `articleBody` out of a page, or "" if there is none.

    News sites that render a shortened article in HTML often still ship the full text
    in JSON-LD — SCMP yields 410 words this way against trafilatura's 238 — so this is
    used as a second opinion whenever it beats what trafilatura extracted.
    """
    if not html:
        return ""
    if isinstance(html, bytes):
        html = html.decode("utf-8", "ignore")
    best = ""
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1).strip())
        except Exception:
            continue  # one malformed block must not lose the others
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict):
                body = node.get("articleBody")
                if isinstance(body, str) and len(body) > len(best):
                    best = body
    return best

# Patterns that indicate the LLM refused to generate a post instead of writing one.
# These must never reach the Telegram queue — generate_post() returns None when matched.
_REFUSAL_PATTERNS = re.compile(
    r"^\[SKIP\]$"                              # explicit skip token (prompt-instructed)
    r"|does not (provide|contain|include)"     # "does not provide relevant information", etc.
    r"|no suitable content"
    r"|cannot be generated based on"
    r"|does not meet the criteria"
    r"|please provide an article"
    r"|I (cannot|can't|am unable to) (generate|write|create|produce)"
    r"|I'm unable to",
    re.IGNORECASE,
)


# "This is an entry from: Live: ..." — matches anywhere in the text (not just line-start)
# because the fallback body is built as "{title}. {description}" which puts this phrase
# mid-line after the title, so a ^ anchor would silently miss it.
_LIVE_BLOG_ENTRY_RE = re.compile(
    r"This is an entry from\s*:[^\n]*",
    re.IGNORECASE,
)

# Standalone timestamp lines, e.g. "23 March 2026 14:45 GMT" or "March 23, 2026 2:45 PM GMT"
# Uses line anchors (MULTILINE) — these always appear on their own line.
_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{4}\s+\d{1,2}:\d{2}(?:\s+[A-Z]{2,5})?\s*$"
    r"|^\s*(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}(?:\s*[AP]M)?(?:\s+[A-Z]{2,5})?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _trim_post(text: str, max_words: int = 100) -> str:
    """Trim an LLM-generated post to the last complete sentence at or under max_words.

    Splits on sentence boundaries (. ! ?) and accumulates sentences until adding
    the next one would exceed max_words.  Avoids false matches on abbreviations
    like U.S., Maj. Gen., Lt. Col. by requiring the boundary to be followed by a
    space + uppercase letter or end-of-string.
    If the very first sentence already exceeds max_words, the text is returned
    as-is (better a long post than a bizarrely short one).
    """
    if len(text.split()) <= max_words:
        return text

    # Split into sentences: break after . ! ? when followed by whitespace + capital
    # or end of string.  This skips abbreviation periods (U.S., Maj. etc.).
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c])', text)

    kept: list[str] = []
    word_count = 0
    for sent in sentences:
        sent_words = len(sent.split())
        if word_count + sent_words > max_words:
            break
        kept.append(sent)
        word_count += sent_words

    if not kept:
        # Even the first sentence is over max_words — return full text unchanged
        return text

    return " ".join(kept).strip()


def _clean_scraped_body(text: str) -> str:
    """Strip HTML tags and remove CMS metadata lines from scraped/RSS body text."""
    # Strip residual HTML tags first
    text = _HTML_TAG_RE.sub(" ", text)
    # Remove live-blog breadcrumb anywhere it appears (mid-line or line-start)
    text = _LIVE_BLOG_ENTRY_RE.sub("", text)
    # Remove standalone timestamp lines
    text = _TIMESTAMP_LINE_RE.sub("", text)
    # Collapse whitespace left by removals
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()


class ClaudeClient:
    def __init__(
        self,
        config: ClaudeConfig,
        session: Optional[aiohttp.ClientSession] = None,
        openai_client=None,
        metadata_config: Optional[MetadataApiConfig] = None,
    ) -> None:
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.api_key)
        self._session = session  # used for article body fetching
        self._openai = openai_client  # used for post generation (gpt-4o-mini)
        cfg = metadata_config or MetadataApiConfig()
        self._extract_premium_url: str = cfg.extract_premium_url
        self._extract_premium_api_key: str = cfg.self_hosted_api_key
        self.reload_prompts()

    def reload_prompts(self) -> None:
        """Load/reload prompts from disk. Call after editing prompt files."""
        self._post_system_prompt = _load_prompt("generate_post_system.txt")
        self._post_user_template = _load_prompt("generate_post_user.txt")
        self._score_prompt = _load_prompt("score_articles.txt")
        logger.info("Claude prompts reloaded")

    # Patterns that indicate a bot-protection / JS-required page — not real article content
    _BOT_BLOCK_PATTERNS = re.compile(
        r"javascript\s+is\s+disabled|enable\s+javascript|javascript\s+must\s+be\s+enabled"
        r"|please\s+enable\s+javascript|supported\s+browser|switch\s+to\s+a\s+supported\s+browser"
        r"|captcha|cloudflare\s+ray\s+id|access\s+denied|checking\s+your\s+browser",
        re.IGNORECASE,
    )

    _FETCH_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # Domains the self-hosted extract-premium endpoint implements, per its own
    # OpenAPI description at {extract_premium_url_host}/openapi.json. Any other
    # host returns HTTP 500 "Domain X is not supported for content download".
    _EXTRACT_PREMIUM_DOMAINS = ("nytimes.com", "washingtonpost.com", "bloomberg.com")

    async def _fetch_article_body(self, url: str, cookie: str | None = None) -> Optional[str]:
        """
        Fetch article URL and extract main text.

        Mirrors n8n 'scape article' node logic:
          1. If cookie is present (paywalled source like FT), try the self-hosted
             extract-premium service first — passes the cookie so the scraper can
             authenticate and return full article text.
          2. Fall back to trafilatura via urllib (with cookie) or fetch_url.
        Returns None if content is a bot-protection page or extraction fails.
        """
        host = urllib.parse.urlparse(url).netloc.lower()

        # Step 1: self-hosted extract-premium (mirrors n8n 'scape article' node).
        # Only for the domains the service actually implements — anything else comes
        # back HTTP 500 "Domain X is not supported for content download", so sending
        # ft.com/wsj.com there was a guaranteed-wasted round trip.
        if (
            cookie
            and self._session
            and self._extract_premium_url
            and any(d in host for d in self._EXTRACT_PREMIUM_DOMAINS)
        ):
            try:
                async with self._session.post(
                    self._extract_premium_url,
                    json={"url": url},
                    headers={
                        "X-API-Key": self._extract_premium_api_key,
                        "cookie": cookie,
                    },
                    # Succeeds in 0.1–5s when it works; 20s only ever bought us a
                    # slower drop on hosts the service cannot reach at all.
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        text = data.get("extracted_text") or ""
                        if text and not self._BOT_BLOCK_PATTERNS.search(text):
                            logger.debug("extract-premium succeeded for %s", url)
                            return _clean_scraped_body(text)
                    else:
                        logger.debug(
                            "extract-premium HTTP %d for %s", resp.status, url
                        )
            except Exception as e:
                logger.debug("extract-premium failed for %s: %s", url, e)

        # Step 2: trafilatura fallback
        loop = asyncio.get_event_loop()
        try:
            def _fetch() -> tuple[str | bytes | None, Optional[str]]:
                """Returns (html, error_label). error_label is set on a failed fetch."""
                if cookie:
                    req = urllib.request.Request(
                        url,
                        headers={"Cookie": cookie, "User-Agent": ClaudeClient._FETCH_UA},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=18) as resp:
                            return resp.read(), None
                    except urllib.error.HTTPError as e:
                        return None, f"HTTP {e.code}"
                    except Exception as e:
                        return None, type(e).__name__
                return trafilatura.fetch_url(url), None

            html, err = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch),
                timeout=20,
            )
            if err or not html:
                if cookie:
                    # A paywalled source whose cookie no longer authenticates fails
                    # silently otherwise: the body falls back to a ~15-word RSS teaser
                    # and the article is dropped for being too short. Say so out loud.
                    logger.warning(
                        "Paywalled fetch failed for %s: %s — cookie may be expired",
                        url, err or "empty response",
                    )
                else:
                    logger.debug("Article fetch returned nothing for %s: %s", url, err)
                return None

            text = trafilatura.extract(
                html, include_comments=False, include_tables=False, favor_precision=True
            ) or ""
            # Second opinion: JSON-LD often carries the full body where the rendered
            # HTML is truncated. Only take it when it is clearly the better extraction.
            jsonld = _jsonld_article_body(html)
            if len(jsonld.split()) > len(text.split()):
                logger.debug(
                    "Using JSON-LD body for %s (%d words vs trafilatura's %d)",
                    url, len(jsonld.split()), len(text.split()),
                )
                text = jsonld
            if not text:
                return None
            if self._BOT_BLOCK_PATTERNS.search(text):
                logger.debug("Bot-protection page detected for %s — ignoring fetched body", url)
                return None
            return _clean_scraped_body(text)
        except Exception as e:
            logger.debug("Article fetch failed for %s: %s", url, e)
            return None

    async def resolve_body(self, article: Article) -> Optional[str]:
        """
        Fetch + clean the article's source body, caching it on `article.body`.

        Called by generate_post and by the DailyNews editor_review stage, which runs
        first — the cache is what stops the same article being scraped twice per run.
        Returns None when the body is unusable (too short to make a post from);
        `article.body` is set to "" in that case so we don't re-fetch.
        """
        if article.body is not None:
            return article.body or None

        # For X/Twitter sources, never fetch the article URL — x.com requires login.
        # The tweet text is already fully captured in article.title.
        is_x_source = bool(article.source and article.source.startswith("@"))
        if is_x_source:
            body = article.title.strip()
        else:
            body = await self._fetch_article_body(article.url, cookie=article.cookie)
            if not body:
                # Use description only — title is already in the template's Title: field.
                body = _clean_scraped_body(article.description or article.title or "")
                logger.debug("Using description fallback for %s", article.url)

        # Trafilatura often extracts text that starts with the article headline.
        # If the body's first sentence duplicates the title, strip it to avoid
        # the same sentence appearing twice in the LLM input (Title: X \n Article: X ...).
        # Skip for X sources: body IS the tweet text, not a scraped article body.
        if not is_x_source and body and article.title:
            title_norm = article.title.strip().rstrip(".").lower()
            first_line = body.split("\n")[0].strip().rstrip(".").lower()
            if first_line == title_norm or first_line.startswith(title_norm[:60].lower()):
                body = body[body.index("\n"):].strip() if "\n" in body else body

        word_count = len(body.split())

        # Drop articles whose English body is too short to generate a meaningful post.
        # Non-English content is never filtered here — CJK text has no spaces so word_count
        # is meaningless (e.g. a 500-char Chinese article counts as ~1 "word").
        if _is_english(body) and word_count < 40:
            logger.info("Dropping short English article (%d words): %s", word_count, article.url)
            article.body = ""
            return None

        article.body = body
        return body or None

    async def generate_post(
        self,
        article: Article,
        system_prompt_override: Optional[str] = None,
        user_template_override: Optional[str] = None,
        always_rewrite: bool = False,
    ) -> Optional[str]:
        """
        Fetch article body and generate a 60-95 word Gettr post.
        Replicates 'Gen Gettr Post' node from V1.1_daily_news_rss workflow.
        Falls back to title+description if article body fetch fails.
        system_prompt_override: use this system prompt instead of the default.
        """
        body = await self.resolve_body(article)
        if not body:
            return None

        # All content goes through LLM rewrite to enforce the 55-75 word target.
        body = body[:4000]
        template = user_template_override if user_template_override is not None else self._post_user_template
        user_msg = template.replace("{title}", article.title).replace("{article}", body)
        system_prompt = system_prompt_override if system_prompt_override is not None else self._post_system_prompt

        _WORD_LIMIT = 75
        _WORD_MIN = 55
        _RETRY_TOO_LONG = (
            "Your previous response exceeded the 75-word hard limit. "
            "Rewrite it now as exactly two paragraphs: target 55–75 words total. "
            "Count every word. Do not exceed 75 words under any circumstance."
        )
        _RETRY_TOO_SHORT = (
            "Your previous response was too short. "
            "Rewrite it now as exactly two full paragraphs: target 55–75 words total. "
            "Do not write fewer than 55 words."
        )

        async def _call_llm(user: str, prior_assistant: Optional[str] = None, retry_msg: str = _RETRY_TOO_LONG) -> Optional[str]:
            if self._openai:
                # OpenAI client only supports single-turn; fold retry context into user msg.
                if prior_assistant:
                    combined = f"{user_msg}\n\n[Your previous response was {len(prior_assistant.split())} words]\n{prior_assistant}\n\n{retry_msg}"
                else:
                    combined = user
                return await self._openai.chat_complete(
                    system=system_prompt,
                    user=combined,
                    max_tokens=600,
                    temperature=0.1,
                )
            else:
                messages: list[dict] = [{"role": "user", "content": user}]
                if prior_assistant:
                    messages = [
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": prior_assistant},
                        {"role": "user", "content": retry_msg},
                    ]
                message = self._client.messages.create(
                    model=self._config.scoring_model,
                    max_tokens=600,
                    system=system_prompt,
                    messages=messages,
                )
                return message.content[0].text.strip()

        try:
            post = await _call_llm(user_msg)
            if not post:
                return None

            if _REFUSAL_PATTERNS.search(post.strip()):
                logger.warning("Post generation returned a refusal — dropping article: %s", article.url)
                return None

            word_count_out = len(post.split())
            if word_count_out > _WORD_LIMIT or word_count_out < _WORD_MIN:
                retry_msg = _RETRY_TOO_LONG if word_count_out > _WORD_LIMIT else _RETRY_TOO_SHORT
                logger.warning(
                    "Post for %s is %d words (%s 55–75 range) — retrying",
                    article.url, word_count_out, "over" if word_count_out > _WORD_LIMIT else "under",
                )
                retry_post = await _call_llm(user_msg, prior_assistant=post, retry_msg=retry_msg)
                if retry_post:
                    retry_wc = len(retry_post.split())
                    if retry_wc > _WORD_LIMIT:
                        logger.warning(
                            "Retry post for %s still %d words (over limit) — using anyway (no trim)",
                            article.url, retry_wc,
                        )
                    elif retry_wc < _WORD_MIN:
                        logger.warning(
                            "Retry post for %s still only %d words (under min) — using anyway",
                            article.url, retry_wc,
                        )
                    post = retry_post

            return post
        except Exception as e:
            logger.warning("Post generation failed for %s: %s", article.url, e)
            return None


    async def generate_posts_batch(
        self,
        articles: list[Article],
        concurrency: int = 3,
        system_prompt_override: Optional[str] = None,
        user_template_override: Optional[str] = None,
        always_rewrite: bool = False,
    ) -> None:
        """
        Generate Gettr posts for a list of articles concurrently.
        Sets article.llm_post in-place.
        """
        sem = asyncio.Semaphore(concurrency)

        async def _gen(article: Article) -> None:
            async with sem:
                article.llm_post = await self.generate_post(
                    article,
                    system_prompt_override=system_prompt_override,
                    user_template_override=user_template_override,
                    always_rewrite=always_rewrite,
                )

        await asyncio.gather(*[_gen(a) for a in articles])

    async def score_batch(
        self, articles: list[Article], prompt_override: str | None = None
    ) -> list[Article]:
        """
        Score articles in batches. Adds llm_score (0–10) and llm_comment to each.
        Articles that fail scoring retain llm_score=0 and pass through.
        prompt_override: use this prompt string instead of the default score_articles.txt.
        """
        batch_size = self._config.batch_size
        for i in range(0, len(articles), batch_size):
            batch = articles[i : i + batch_size]
            await self._score_one_batch(batch, prompt_override=prompt_override)
        return articles

    async def _score_one_batch(
        self, batch: list[Article], prompt_override: str | None = None
    ) -> None:
        articles_data = [
            {
                "idx": i,
                "title": a.title,
                "description": (a.description or "")[:300],
                "source": a.source or "",
                "url": a.url,
            }
            for i, a in enumerate(batch)
        ]
        articles_json = json.dumps(articles_data, ensure_ascii=False)

        score_prompt = prompt_override if prompt_override is not None else self._score_prompt

        # Split prompt into system (instructions) and user (articles) parts
        if "\nArticles:\n" in score_prompt:
            system_part, _ = score_prompt.split("\nArticles:\n", 1)
        else:
            system_part = score_prompt.replace("{articles_json}", "")
        user_part = articles_json

        try:
            if self._openai:
                raw = await self._openai.chat_complete(
                    system=system_part,
                    user=user_part,
                    max_tokens=self._config.max_tokens,
                    temperature=0.3,
                    model=self._openai._config.scoring_model,
                )
            else:
                # Fallback to Claude if OpenAI not available
                prompt = score_prompt.replace("{articles_json}", articles_json)
                message = self._client.messages.create(
                    model=self._config.scoring_model,
                    max_tokens=self._config.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = message.content[0].text.strip()
            scores = self._parse_scores(raw, len(batch))
        except Exception as e:
            from openai import APITimeoutError, APIConnectionError
            if isinstance(e, APITimeoutError):
                logger.warning("Scoring TIMED OUT for batch of %d (all scores→0, run will produce no posts): %s", len(batch), e)
            elif isinstance(e, APIConnectionError):
                logger.warning("Scoring CONNECTION ERROR for batch of %d (all scores→0, run will produce no posts): %s", len(batch), e)
            else:
                logger.warning("Scoring failed for batch of %d: %s", len(batch), e)
            return  # leave llm_score=0, articles pass through

        for article, score_obj in zip(batch, scores):
            article.llm_score = max(0.0, min(10.0, float(score_obj.get("llm_score", 0))))
            article.llm_comment = score_obj.get("llm_comment", "")

    def _reorder_by_idx(self, data: list[dict], expected: int) -> list[dict]:
        """Reorder score objects by their 'idx' field if present and valid.

        If every object carries a valid, unique idx in [0, expected) the list is
        reordered so that data[idx] lands at position idx — correcting any LLM
        reordering.  Falls back to the original positional order if idx is absent,
        malformed, out-of-range, or duplicated.
        """
        if not data or "idx" not in data[0]:
            return data  # no idx field → positional fallback
        ordered: list[dict] = [{"llm_score": 0, "llm_comment": ""}] * expected
        seen: set[int] = set()
        for obj in data:
            try:
                idx = int(obj["idx"])
            except (KeyError, TypeError, ValueError):
                return data  # malformed idx → positional fallback
            if idx < 0 or idx >= expected or idx in seen:
                return data  # out-of-range or duplicate → positional fallback
            seen.add(idx)
            ordered[idx] = obj
        return ordered

    def _parse_scores(self, raw: str, expected: int) -> list[dict]:
        """Extract JSON array from Claude response, handling markdown fences."""
        # Strip markdown code fences if present
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
        try:
            data = json.loads(raw)
            if isinstance(data, list) and len(data) == expected:
                return self._reorder_by_idx(data, expected)
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in the text
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, list) and len(data) == expected:
                    return self._reorder_by_idx(data, expected)
            except json.JSONDecodeError:
                pass

        logger.warning("Could not parse Claude response as JSON array (expected %d items)", expected)
        return [{"llm_score": 0, "llm_comment": ""}] * expected
