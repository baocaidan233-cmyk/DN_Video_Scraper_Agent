# Daily News Agent — System Design Document

**Status:** Production
**Architecture:** Single Python process, asyncio event loop, two parallel content pipelines
**Entry point:** `python3 main.py`

---

## 1. Overview

The Daily News Agent is an async Python pipeline that replaces 8 n8n workflows. It ingests content from two distinct source types, deduplicates it semantically, scores it with Claude, routes it through human Notion review, and publishes to Gettr.

Two hardcoded pipelines run concurrently in one process, sharing all infrastructure except their source agents and Gettr accounts:

| Pipeline | Sources | Dashboard Port | Gettr Account | Review UI |
|---|---|---|---|---|
| **DailyNews** | RSS feeds (via Notion DB) | 8080 | DailyNews account | Notion review DB |
| **Epic Fury** | X (Twitter) + websites | 8081 | Epic Fury account | Notion review DB (separate) |

Any number of **additional channels** can be added from `config.yaml` alone, with no code changes — each behaves like Epic Fury with its own sources, prompts, Redis namespace, Notion board, Gettr account, and port. See §19.

Shared: Redis, OpenAI embeddings, Claude, aiohttp session, GcpClient, `Pipeline` class, `SimilarityAgent`, `PublishAgent`, `NotionReviewAgent`.

---

## 2. Directory Structure

```
dailynews-agent/
├── main.py                          # Entry point — asyncio.gather of all loops
├── config.yaml                      # All credentials + settings
├── requirements.txt
│
├── core/
│   ├── config.py                    # Pydantic v2 models + ConfigHolder (hot-reload)
│   ├── models.py                    # Article, ReviewItem, PostResult dataclasses
│   ├── pipeline.py                  # Ingestion orchestrator (shared by both pipelines)
│   ├── channel_runtime.py           # build_channel() — spins up a config-driven channel (§19)
│   └── redis_client.py              # Async Redis wrapper (Upstash TLS)
│
├── agents/
│   ├── rss_agent.py                 # RSS feed fetching + URL dedup (DailyNews)
│   ├── similarity_agent.py          # Embeddings + cosine dedup (shared)
│   ├── notion_review_agent.py       # Active review agent — Notion page queue (shared)
│   ├── publish_agent.py             # Gettr posting + media upload (shared)
│   ├── x_agent.py                   # X/Twitter scraping (Epic Fury only)
│   ├── website_agent.py             # Website + auto-RSS scraping (Epic Fury only)
│   └── source_reader.py             # Parses sources/epicfury_sources.md
│
├── services/
│   ├── claude_client.py             # Claude scoring + post generation (shared)
│   ├── openai_client.py             # Embeddings + fallback post gen (shared)
│   ├── qdrant_client.py             # Vector store wrapper (shared)
│   ├── gcp_client.py                # CDN media upload, GCS resumable flow (shared)
│   ├── gettr_client.py              # Gettr API POST builder (shared)
│   ├── gemma_client.py              # Content verification via Google Gemma 4 (DailyNews)
│   ├── notion_client.py             # Notion RSS source DB reader (DailyNews)
│   ├── notion_topical_dedup.py      # Publish-time topical dedup via OpenAI embeddings vs daily_news (DailyNews)
│   ├── gettr_feed_client.py         # Public Gettr per-account posts feed, used by the topical dedup crosscheck
│   ├── metadata_client.py           # OG metadata fetcher with proxy fallbacks
│   └── pollinations_client.py       # AI image generation via Pollinations
│
├── utils/
│   ├── hashing.py                   # SHA256 URL hash + SHA1 post hash
│   └── text_cleaner.py              # Strip ASCII control chars
│
├── dashboard/
│   ├── app.py                       # aiohttp app factory
│   ├── state.py                     # DashboardState (SSE ring buffer, schedule)
│   ├── auth.py                      # Session middleware + HMAC password check
│   ├── db.py                        # SQLite run history (stdlib sqlite3 + asyncio)
│   ├── hot_topics.py                # HotTopicsStore — persists keyword priority list
│   ├── setup_password.py            # CLI: generate password hash
│   ├── handlers/
│   │   ├── pages.py                 # Serves index.html (injects pipeline_type)
│   │   ├── sse.py                   # Server-Sent Events stream
│   │   ├── api.py                   # Trigger, schedule, queue, cancel
│   │   ├── history.py               # Run history from SQLite
│   │   ├── health.py                # Service health checks
│   │   └── config_editor.py         # config.yaml + prompts editor (masks secrets)
│   └── templates/
│       └── index.html               # Alpine.js SPA (single file)
│
├── prompts/
│   ├── score_articles.txt           # Claude scoring prompt (DailyNews)
│   ├── epicfury_score_articles.txt  # Claude scoring prompt (Epic Fury)
│   ├── generate_post_system.txt     # Post generation — system prompt (DailyNews)
│   ├── generate_post_user.txt       # Post generation — user template (DailyNews)
│   ├── epicfury_generate_post_system.txt  # Post generation — system prompt (Epic Fury)
│   ├── epicfury_generate_post_user.txt    # Post generation — user template (Epic Fury)
│   ├── post_generation_rules.txt    # Shared rules appended to post gen prompts
│   └── verify_post.txt              # Gemma verification system prompt
│
├── sources/
│   ├── epicfury_sources.md          # X handles + website URLs (re-read each cycle)
│   └── dailynews_sources.md         # DailyNews source documentation
│
└── data/
    ├── schedule.json                # Persisted interval/minute/pause state (DailyNews)
    ├── schedule_ef.json             # Persisted interval/minute/pause state (Epic Fury)
    ├── hot_topics.json              # Persisted hot-topic keywords + threshold
    └── history.db                   # SQLite run history (auto-created)
```

---

## 3. Data Model

### `Article`
```python
url: str
title: str
description: str | None
author: str | None
published_at: datetime          # alias: publishedAt
url_to_image: str | None        # alias: urlToImage — thumbnail or OG image
source: str | None              # Feed/site name or @handle
url_hash: str                   # sha256(url_without_query)[:16]
cookie: str | None              # Per-source HTTP cookie for media fetch

# Set by SimilarityAgent
embedding: list[float] | None
is_duplicate: bool
cross_batch_score: float
cross_batch_matched_url: str | None

# Set by XAgent / WebsiteAgent (Epic Fury)
has_video: bool                 # True if tweet/page contains video
video_url: str | None           # Highest-quality MP4 URL (first/primary video)
video_urls: list[str]           # All MP4 URLs from tweet (multi-video tweets)

# Set by ClaudeClient
body: str | None                # Cached scraped source body ("" = unusable, don't re-fetch)
llm_score: float                # 0–10
llm_comment: str                # ≤120 chars
llm_post: str | None            # Generated Gettr post (55–75 words)

# Set by EditorReviewClient (DailyNews A/B branch only)
editor_post: str | None         # FINISHED post from the 3-prompt chain → test Gettr account
                                # Used verbatim; never re-run through generate_post
```

`body` exists purely as a scrape cache. `ClaudeClient.resolve_body()` populates it, and both the
editor_review stage and post generation read through it — without it, enabling the editor branch
would scrape every article twice per run.

### `ReviewItem` (stored in Redis hash, TTL 24h)
```python
article_id: str
url: str
title: str
description: str | None
source: str | None
published_at: str               # ISO string
url_to_image: str | None
url_hash: str
llm_score: float
llm_comment: str
post_content: str | None        # Generated post or editor override
editor_post: str | None         # A/B variant posted to the test Gettr account (may be absent)
telegram_message_id: int | None
media: list[str]                # Media URLs to upload at publish time
```

`article_id` is `article.url_hash` (falls back to a 12-char UUID slice if `url_hash` is empty).

**`notion_page_id` is NOT a `ReviewItem` field.** It is written directly into the Redis hash by
`_create_notion_page()` after the Notion API returns, and read back by `_process_one()` via
`data.get("notion_page_id")`. `_handle_decision()` also writes it into the hash it reconstructs when
the original key has expired. Adding it to the model would not populate it — the page doesn't exist
yet when the `ReviewItem` is built.

---

## 4. Shared Pipeline Infrastructure

Both pipelines use the same `Pipeline` class (`core/pipeline.py`), configured differently at construction time.

### Pipeline Constructor

```python
Pipeline(
    rss_agent=...,                        # RssAgent (DailyNews) or None (Epic Fury)
    source_agents=[x_agent, website_agent],  # list of source agents (Epic Fury) or []
    sources_md_path="sources/epicfury_sources.md",  # re-read each cycle (Epic Fury)
    similarity_agent=...,
    claude_client=...,
    claude_config=...,
    notion_client=...,                    # NotionClient (DailyNews) or None (Epic Fury)
    review_agent=...,                     # NotionReviewAgent instance
    state=...,                            # DashboardState for SSE emission
    hot_topics_store=...,                 # HotTopicsStore (DailyNews only)
    always_rewrite=True,                  # always run LLM post rewrite
    gemma_client=...,                     # GemmaClient (DailyNews only)
    video_score_boost=0.0,                # 1.0 for Epic Fury
    score_prompt_override=None,           # path to epicfury_score_articles.txt (Epic Fury)
    post_prompt_override=None,
    post_user_template_override=None,
)
```

### Concurrency Guard
An `asyncio.Lock()` prevents concurrent runs per pipeline instance. If the previous ingestion is still running when the next is scheduled, the new run is skipped.

### SSE Event Emission
Every pipeline step emits `step_start` + `step_done` events to `DashboardState`, which fans them out to all connected SSE clients. Each event includes `articles_in`, `articles_out`, `articles_dropped`, `duration_ms`. Steps are also persisted to SQLite.

### Runtime Mutability
- `pipeline.set_filter_score_threshold(n)` — update score cutoff without restart
- `pipeline.reload_prompts()` — re-read all prompt override files from disk
- `similarity_agent.set_thresholds(within, cross)` — update dedup thresholds

---

## 5. Pipeline Flow — DailyNews (RSS)

Runs every `rss_interval_s` seconds (default 600).

```
┌─────────────┐    ┌───────────┐    ┌──────────────────────────────┐
│ Notion Fetch│───▶│ RSS Fetch │───▶│ URL Dedup (atomic SET NX EX) │
└─────────────┘    └───────────┘    └──────────────┬───────────────┘
                                                   │
┌──────────────────────────────────────────────────▼───────────────┐
│ Claude Scoring (0–10, drop if < filter_score_threshold)          │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Editor Review (optional — editor_review.enabled, DailyNews only) │
│   3-prompt chain → article.editor_post. Never drops an article.  │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Post Generation (55–75 words, image pre-generation)              │
│   ├─ Hot Topics ranking decides the order candidates are tried   │
│   └─ English source under 40 words → dropped                     │
│      (the A/B variant is already finished — post_gen skips it)   │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Gemma Verification (PASS keeps; FAIL / REVISE drop)              │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Embeddings — only for surviving articles that HAVE a post        │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Stage 1: Within-Batch Cosine Dedup (threshold 0.70)              │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Stage 2: Cross-Batch Qdrant Dedup (threshold 0.80, 48h window)   │
└──────────────────────────────────────────┬───────────────────────┘
                                           │
┌──────────────────────────────────────────▼───────────────────────┐
│ Notion Enqueue → review:queue + review:pending:{id}              │
└──────────────────────────────────────────────────────────────────┘
```

**Scoring and post generation run *before* the similarity stages.** Embedding and dedup are the
expensive-per-article steps, so they only ever see articles that already cleared the score threshold
*and* produced a usable post. Any doc or diagram showing dedup ahead of scoring is out of date.

SSE step names, in emission order: `notion_fetch` → `rss_fetch` → `url_dedup` → `claude_score` →
`editor_review` (only when the branch is enabled) → `post_gen` → `verify_post` → `embeddings` →
`within_batch_dedup` → `cross_batch_dedup` → `telegram_enqueue`. The last name is historical — that
step enqueues to **Notion**, not Telegram.

The pipeline early-returns (run marked `success`) whenever a stage empties the candidate list: no
article met the score threshold, none produced a verified post, or all were duplicates.

### Step 1 — Notion Fetch
Query Notion RSS source DB (`in_use == true`). Returns `list[RssSource(url, name, cookie)]`.

### Step 2 — RSS Fetch + URL Dedup

Fetch feeds concurrently (semaphore 10, timeout 30s). Per entry:
- Time-filter: drop if `published_at < now - filter_feed_hours` (pydantic default 3h; `config.yaml` sets **2h**)
- Google News URL resolution at fetch time (two-strategy):
  - Strategy 1: base64-decode article ID → scan bytes for `http(s)://` prefix
  - Strategy 2: extract first non-Google `href` from description HTML
- Image extraction waterfall: `media_content[0].url` → `media_thumbnail[0].url` → enclosure with `image/*` type → `<img src>` in description HTML
- URL hash: `sha256(url_without_query_params).hexdigest()[:16]`
- Redis dedup via `RedisClient.batch_setnx_with_ttl` — **atomic `SET key "1" NX EX 10800`**, one
  command per key in a single pipeline. (Was `SETNX` + a separate `EXPIRE` pipeline; Upstash
  evicted keys between the two calls, so articles passed dedup on every cycle. Never split it
  back into two commands.) Returns `(articles, raw_count)`.
- **Invariant:** `redis.url_hash_ttl_s >= rss.filter_feed_hours * 3600`. If the TTL is shorter
  than the fetch window, an article falls out of the dedup cache while still inside the window
  and is re-ingested forever. Currently 10800s TTL vs a 7200s window — safe.

### Step 3 — Claude Scoring
Batch `claude.batch_size` articles per call to `claude-haiku-4-5` with `claude.max_tokens`.
Returns `llm_score` (0–10) + `llm_comment` (≤120 chars). Drop if `score < filter_score_threshold`.

Effective values (`config.yaml`): **batch 5, max_tokens 1500, threshold 6.0**. The pydantic
defaults in `core/config.py` are still 10 / 512 / 5.0.

⚠️ **Do not lower `max_tokens` or raise `batch_size`.** At 512/10 the response JSON was truncated
mid-object, `_parse_scores` failed to parse it, and every article in the batch was assigned score
`0.0` — silently dropping whole batches. `max_tokens` must cover a full batch of scores +
comments.

### Step 4 — Hot Topics Ranking
`Pipeline._rank_articles()`, called at the start of post generation — not a separate SSE step.
Articles sorted: hot-topic matches first (keyword substring OR embedding cosine ≥
`semantic_threshold`), both groups sorted by `llm_score` desc. With no `hot_topics_store` (Epic Fury
and all config-driven channels) this degrades to a plain `llm_score` descending sort. It only
prioritizes the *order* candidates are tried in — nothing is dropped here.

### Step 4b — Editor Review (DailyNews only, optional)

SSE step `editor_review`, emitted between `claude_score` and `post_gen`. Gated on
`not socials_mode and editor_client.enabled` — EpicFury and config-driven channels never run it.
Implemented in `services/editor_client.py`; see §21 for the full A/B design.

Per article (concurrency 3), `EditorReviewClient.revise()`:
1. `claude.resolve_body(article)` — the shared scrape cache. Unusable body → skip.
2. **Intake triage** (`prompts/ai_editor_intake_triage_prompt.md`) → returns a structured brief
   (source summary, topic lens, self-indictment anchor, escalation/undercut, recommended close,
   key-facts checklist) containing a `QUALIFIES: yes|no` line. `no` → no variant. An
   unrecognisable verdict **fails open** to qualifying, the same convention Gemma uses for API
   errors. Parsed by `_QUALIFIES_RE`; tolerates `**QUALIFIES:** No`, a full-width colon, and a
   trailing explanation clause.
3. **CCP exposure draft** (`prompts/ai_editor_ccp_exposure_system_prompt.md`). The triage brief is
   the **primary input** — that prompt is written to hand the brief "unmodified to the drafting
   editor" — with the source article attached beneath it under `SOURCE ITEM (reference only)` so
   facts, quotes and figures stay checkable. With no triage prompt configured, the source alone
   is used.
4. **House style pass** (`prompts/unveiled_chinax_style_prompt.md`) → `article.editor_post`,
   the finished post. Run via `_style_pass()`, which constrains it to a voice-only pass —
   see "The style-pass length guard" in §21.

Any exception at any step is caught and logged; the article simply gets no variant. **This stage
never filters the candidate list** — a rejection or failure costs the live channel nothing.

`editor_review.max_per_run` caps how many articles enter the branch (0 = all). Articles are still
in score order at this point, so a cap takes the top N.

### Step 5 — Post Generation
For each article (in ranked order, concurrency 3):
1. Fetch article body via self-hosted `extract-premium` endpoint first; fall back to `trafilatura.fetch_url()` in thread executor (20s timeout). Skip if bot-protection page detected. The result is cached on `article.body` by `resolve_body()`, so when the editor branch already scraped it this is a cache hit.
2. **Short-source drop:** if the body is English (`_is_english`) and under **40 words**, return
   `None` — the article is dropped, no post generated. Non-English bodies are never word-filtered:
   CJK/Arabic text has no spaces, so a 500-character Chinese article counts as ~1 "word" while its
   English translation runs 200+ words.
3. Generate a **55–75 word** Gettr post via OpenAI `gpt-4o-mini` (primary) or Claude (fallback),
   using `prompts/generate_post_system.txt` + `prompts/generate_post_user.txt`. **All** content goes
   through the LLM regardless of length or language (`always_rewrite=True`) — there is no
   "short English passes through unchanged" path any more. See §12 for the retry loop.
4. Pre-generate image via Pollinations if `url_to_image` is None or is a logo/placeholder. Upload directly to GCP CDN so Gettr gets a stable URL.

Post generation touches **only** `llm_post`. The A/B variant is already a finished post when it
leaves the editor chain and is never passed through `generate_post` — see §21.

### Step 6 — Gemma Verification (DailyNews only)
Each generated post sent to `GemmaClient.verify_post(title, post)` → Google AI Studio Gemma 4.
- `PASS` → enqueue as-is
- `REVISE` → **article dropped** (the revised text is *not* used)
- `FAIL` → article dropped, not enqueued
- `ERROR` (API failure / no `**VERDICT:**` line parseable as an API error) → treated as `PASS`,
  fails open so a Gemma outage cannot stall the pipeline

Only `PASS` and `ERROR` reach the enqueue step (`core/pipeline.py:425`). A response that returns
no `**VERDICT:**` line at all is treated as `FAIL`.

System prompt: `prompts/verify_post.txt`.

Verification runs against `llm_post` only, never `editor_post`. The editor branch exists to
foreground CCP exposure, so putting its output through the anti-CCP content filter would defeat
the experiment. An article dropped here loses both variants — correct, since the story itself is
being rejected.

### Step 7 — Embeddings
Text: `title + " " + description`. Batch to OpenAI `text-embedding-3-small` (1536 dims, batch 100, retry 3× exponential). Runs only on articles that survived scoring, generation, and verification.

An embedding failure is logged as a `step_error` and the **whole similarity block is skipped** —
articles pass through to enqueue undeduplicated rather than the run aborting.

### Step 8 — Stage 1: Within-Batch Cosine Dedup
Normalize embeddings, incremental O(n) comparison. Drop if `max_similarity ≥ 0.70`.

### Step 9 — Stage 2: Cross-Batch Qdrant Dedup
Search top-5 with `published_at_ts > now - 48h` filter. Drop if best score `≥ 0.80`. Survivors upserted to Qdrant (point ID: `int(md5(url_hash)[:16], 16) % 2^63`).

### Step 10 — Notion Enqueue
SSE step name `telegram_enqueue` (historical). Store `ReviewItem` to Redis hash
`review:pending:{id}` (TTL 24h), `LPUSH` to the review queue (manual) or the publish queue
(auto-pilot), and create the Notion page — see §9.

> **Removed:** an earlier "Stage 3 — Notion Dedup" ran at ingestion time, embedding queued Notion
> articles into a second Qdrant collection (`qdrant_notion`) and dropping near-matches. That code
> (`services/notion_dedup_client.py`) no longer exists. Its role is now filled by the
> **publish-time** `NotionTopicalDedupChecker` (§9), which — as of the 2026-07-28 rewrite — uses
> OpenAI embeddings against the `daily_news` Notion board (not Qdrant, and not Claude Haiku
> pairwise comparison, which is what an earlier version of that checker used before it was found
> to never actually run under auto-pilot — see §9). The `qdrant_notion` field in `core/config.py`
> is still an unused leftover; `notion_dedup` is very much live — it supplies both the credentials
> and the thresholds/flags (`similarity_threshold`, `recent_lookback_hours`, `enforce_recent_skip`,
> `gettr_handle`, `gettr_crosscheck_interval_minutes`) for the topical checker.

---

## 6. Pipeline Flow — Epic Fury

Identical to DailyNews except: Steps 1–2 are replaced by a sources fetch, scoring uses a separate
prompt plus a `+1.0` video boost, and there is no Gemma verification, no Hot Topics ranking, and no
publish-time topical dedup. The same `Pipeline` class runs both — the differences are all
constructor arguments (§4).

### Step 1 — Sources Fetch (replaces Notion Fetch + RSS Fetch)

Parse `sources/epicfury_sources.md` (re-read each cycle):
- `@handle` lines → X handles for XAgent
- `http...` lines → website URLs for WebsiteAgent
- `#` and blank lines → ignored

**XAgent** — see §7 for full detail. Returns `(articles, raw_count)`.

**WebsiteAgent:**
1. Auto-detect RSS: try `/feed`, `/rss`, `/rss.xml`, `/atom.xml` etc.
2. If RSS found → feedparser (same image waterfall as RssAgent, same time filter)
3. Fallback: fetch homepage → extract article-like links → top N → trafilatura per page
4. Video detection: `has_video=True` if page contains `<video>` tag or embed patterns
5. Keyword pre-filter (see below) before the Redis URL dedup (atomic `SET NX EX`)

Both agents share the same `epicfury:title_hash:` Redis dedup namespace.

### Epic Fury Keyword Pre-filter
Articles without any of these in title + description are dropped before embedding:
```
iran, iranian, irgc, epic fury, operation epic fury,
israel, israeli, idf, netanyahu,
centcom, u.s. military, us military, pentagon,
airstrike, strike, missile, f-35, b-2, carrier,
nuclear, natanz, fordow, khamenei, tehran,
middle east, persian gulf, hormuz, drone, munition,
war, offensive, military operation, air campaign,
ceasefire, cease-fire, negotiations, sanctions,
hezbollah, hamas, houthi, houthis,
lebanon, gaza, west bank, yemen, baghdad, beirut,
tel aviv, jerusalem, proxy
```
Keywords are configurable via `config.yaml` → `epicfury.keywords`.

### Post-Scoring Video Boost
After Claude scoring: if `article.has_video == True`, `llm_score += 1.0` (capped at 10.0). Applied before threshold filter.

### Separate Scoring Prompt
`prompts/epicfury_score_articles.txt` — scores for Operation Epic Fury relevance:
- 1–2: unrelated
- 3–5: background context / regional politics
- 6–8: directly about the operation, strikes, Iran response
- 9–10: breaking news, confirmed strikes, major escalation

---

## 7. XAgent — X/Twitter Scraping (Epic Fury)

Three-tier cascading fallback per handle. Primary API is selected by `state.x_scraper` (persisted in `schedule_ef.json`):

```
twitterapi.io OR socialdata.tools (configurable primary, paid)
        │ error / empty
        ▼
Twitter Syndication API (public, no auth)
        │ error / 429
        ▼
Nitter RSS (public instances)
        + CDN tweet-result fallback for video URLs
```

### Tier 1a — twitterapi.io
`GET https://api.twitterapi.io/twitter/user/last_tweets?userName={handle}`
- Auth: `X-API-Key` header
- Returns up to 20 tweets with full `extendedEntities.media` including `video_info.variants`
- Response path: `data.data.tweets[]`
- `createdAt` format: `"Sun Mar 22 16:31:43 +0000 2026"` (Twitter v1.1)
- Cost: ~$0.15 / 1,000 tweets
- Config: `epicfury.twitterapi.{api_key, base_url, tweets_per_account}`

### Tier 1b — socialdata.tools (alternative primary)
`GET https://api.socialdata.tools/twitter/user/tweets?user_id={id}&type=tweets`
- Auth: `Authorization: Bearer {api_key}`
- Returns tweet list with `extended_entities`
- `tweet_created_at` format: same Twitter v1.1 string
- Config: `epicfury.socialdata.{api_key, base_url, tweets_per_account}`
- Selected when `state.x_scraper == "socialdata"` (togglable from dashboard)

### Tier 2 — Twitter Syndication API
`GET https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}?count=N`
- No authentication; powers Twitter's own embed widget
- Parses `__NEXT_DATA__` JSON from HTML response
- Returns full `extended_entities` with `video_info.variants`
- Returns empty on 429 (rate-limited) → falls back to Nitter

### Tier 3 — Nitter RSS
Public Nitter instances tried in order (`nitter.privacyredirect.com`, `nitter.poast.org`, …)
- Returns RSS with image thumbnails only — no `video.twimg.com` URLs
- Video tweets detected by thumbnail URL patterns (`amplify_video_thumb`, `ext_tw_video_thumb`)
- For detected video tweets without an MP4 URL:
  → `GET https://cdn.syndication.twimg.com/tweet-result?id={id}&token={computed_token}`
  → Token: `round(int(tweet_id) / 1e15 * π).toString(base36)`
  → Parses `mediaDetails[].video_info.variants` for highest-bitrate MP4

### Media Extraction (shared across all tiers)

`_extract_media_from_extended_entities(ext_entities)` returns:
- `url_to_image` — first image URL, or video thumbnail for video tweets
- `video_url` — highest-bitrate MP4 of the first video (single, for backward compat)
- `video_urls` — list of highest-bitrate MP4 URLs for **all** videos in the tweet
- `has_video` — True if any video or GIF media item found

For tweets with multiple videos, **all** MP4 URLs are collected. All are uploaded to Gettr CDN. The first becomes the embedded video player; remaining videos' screen thumbnails appear as image attachments.

---

## 8. Publish Pipeline (shared by both pipelines)

Runs every `publish_interval_s` seconds (default 3600). Polled every 60s; fires as soon as the interval elapses (no fixed clock time). Drains publish queue FIFO.

```
publish:queue (list of article_ids)
        │
        ▼
55-Word Floor (DailyNews only) ── < 55 words ──▶ drop + Notion "Discarded"
        │ ok
        ▼
Article-ID Dedup ─── already published ──▶ skip + Notion "Discarded"
        │ unique
        ▼
SHA1 Post Hash Dedup ─── duplicate ──▶ skip (TTL still unexpired)
        │ unique
        ▼
    ┌────────────────────────────────────────────────────────────────┐
    │ media_urls not empty                                           │
    │   → per URL: skip source logos (utils/logo_detect)             │
    │              skip images < 120px / < 120,000 px²  (PIL)        │
    │   → upload survivors via GcpClient → POST to Gettr with media  │
    │   → nothing survived: AI video (§22), else DN drop / EF OG     │
    │                                                                │
    │ no media (DailyNews)                                           │
    │   → url_to_image, else resolve URL + fetch OG metadata:        │
    │       YouTube → Data API v3                                    │
    │       Others → caps.gettr.com proxy → urlmeta.org → self-hosted│
    │   → null the image if it is a source logo, or too small        │
    │   → still no image:  AI video (§22)  ── else ──▶ drop          │
    │                                                                │
    │ no media (EpicFury)                                            │
    │   → AI video (§22, switch off by default)                      │
    │   → else resolve URL + OG metadata → POST with preview fields  │
    └────────────────────────────────────────────────────────────────┘
        │
        ▼
SET post:{sha1} TTL 864000s
  + SET post:article:{article_id} TTL 864000s
  + DELETE review:pending:{id}
```

On the two DailyNews success paths, `_post_editor_twin()` fires between the Gettr POST and
`_mark_posted()` — see §21. A video post skips it (see §22).

`services/pollinations_client.py` (AI *image* generation) remains in the tree but is **dead
code** — nothing imports it, and `image_gen.enabled` is false. Its source-logo constants were
moved to `utils/logo_detect.py`, which the video fallback uses; the module re-exports them.

### Pre-Publish Gates

Run in `_process_one()` before any media work, in this order:

| # | Gate | Pipelines | On hit |
|---|---|---|---|
| 1 | **55-word floor** — `len(post_content.split()) < 55` | DailyNews only | `_cleanup()` + `_notify_dropped()` → `"skipped"` |
| 2 | **Article-ID dedup** — `EXISTS {post_hash_prefix}article:{article_id}` | both | same |
| 3 | **SHA1 post dedup** — `EXISTS {post_hash_prefix}{sha1_12}` | both | same |

**Gate 1 (`_DN_MIN_WORDS = 55`)** is the last line of defense against one-sentence posts. It catches
short LLM output *and* the publish-time raw-title fallback (which bypasses `generate_post()`
entirely). Epic Fury is intentionally exempt — it may legitimately post short items and OG previews.

**Gate 2** exists because the SHA1 key is `sha1(post_content[:500] + first_media_url)`, and each
re-ingestion re-runs the LLM, producing different post text and therefore a different SHA1 — so the
stored key from the previous publish never matched. The article-ID key is stable across
regeneration. Written by `_mark_posted()` alongside the SHA1 key.

Gates 2 and 3 both emit the SSE step name `sha1_dedup` — deliberate, so the dashboard graph keeps a
single publish-time-dedup node.

None of these gates apply to the editor A/B twin: it only runs after the live post for the same
article has already passed all three.

### Media URL List (`ReviewItem.media`)

Built by `_build_media_list(article)` at review/enqueue time:

| Priority | Condition | Result |
|---|---|---|
| 1 | `article.video_urls` non-empty | all video MP4 URLs |
| 2 | `article.video_url` set | `[video_url]` (single, Nitter/CDN path) |
| 3 | X/Twitter article + `url_to_image` | `[url_to_image]` (uploaded via GCP) |
| 4 | all other sources | `[]` (OG-preview branch handles image) |

X/Twitter articles are identified by URL containing `x.com`/`twitter.com` or source starting with `@`.

### Image Size Validation
Before GCP upload: fetch first 4 KB of image URL via `Range: bytes=0-4095`, parse with PIL. Two
thresholds, both must pass (`_check_image_size`):
- `width × height >= 120_000` px² (`_MIN_IMAGE_AREA`)
- `min(width, height) >= 120` px (`_MIN_IMAGE_SHORT_SIDE`)

If dimensions can't be determined, allow through. For DailyNews a too-small image means the article
is dropped (no text-only path); Epic Fury falls back to an OG-preview post.

### GCS Resumable Upload Flow (`GcpClient`)

1. `GET https://upload.gettr.com/media/get_upload_channel?scene=getter`
   Headers: `filename`, `authorization` (JWT), `userid`
   → Returns `{gcs: {url}, notify_url}` *(key is `gcs`, not `gcp`)*
2. `POST gcs.url` with `x-goog-resumable: start` → `Location` header (session URL)
3. Stream source → `PUT Location` (no buffering). Adds `Referer: https://x.com/` for `twimg.com` URLs.
   - On failure → retry via download proxy: `POST http://n8n-svr.gettr.fyi:7771/api/v1/media/download`
4. `GET https://upload.gettr.com/{notify_url}?uploadedurl={gcs_path}&result=ok` *(must be GET, not POST)*
   → Returns `{m3u8, nm_uri, screen, ori, play_uri, duration, width, height}` (video)
   → Returns `{ori}` with empty `m3u8`/`screen` (image)
5. Retry: 3 attempts, 5s backoff

### OG Metadata Fetching (`MetadataClient`)

URL resolution chain (for non-YouTube URLs):
1. Self-hosted resolver: `POST http://n8n-svr.gettr.fyi:7771/api/v1/url/final` → `{"final_url": ...}`
2. Base64 decode (Google News fallback)
3. HTTP redirect follow

Image fetch chain (for "OTHERS" — not YouTube, not x.com/facebook.com):
1. `GET https://caps.gettr.com/<full_article_url>` with `origin/referer: https://gettr.com/` — Gettr's own scraping proxy
2. urlmeta.org with `Authorization: Basic base64("apikey:")`
3. Self-hosted: `POST http://n8n-svr.gettr.fyi:7771/api/v1/website/metadata`

YouTube: YouTube Data API v3 (`youtube_api_key` in config).

### Gettr Payload — `_build_post_with_media`

```python
video_el = next((m for m in uploaded if m.get("m3u8")), None)
# imgs: non-video items + screen thumbnails of additional videos
imgs = []
for m in uploaded:
    if m.get("m3u8"):
        if m is not video_el:
            imgs.append(m.get("screen") or m.get("ori"))   # extra video → thumbnail
    else:
        imgs.append(m.get("screen") or m.get("ori"))        # image → direct
```

Fields: `vid`, `pvid`, `ovid`, `nmvid` = m3u8/nm_uri of first video; `vid_dur/wid/hgt`; `imgs[]` = all images + extra video thumbnails; `main` = first video/image thumbnail.

---

## 9. NotionReviewAgent

Articles pending review are created as pages in a Notion database. A polling loop detects when the editor changes the `Decision` property.

### Flow
1. `pipeline.run_ingestion()` calls `review_agent.enqueue_article(article)`
2. `_card_sender_loop()` drains asyncio.Queue → creates Notion page with all article fields + image block
3. Editor opens Notion, reads Post Content, optionally fills `Edit Override` field, sets `Decision`
4. `_poll_loop()` (every `poll_interval_s`, 30s) queries for pages whose `Decision` is
   `Approved`, `Rejected`, or `Publish Now` — **`Queued` and `Pending` are not queried** — and acts:
   - **Approved** → `LPUSH publish:queue article_id` (topical dedup no longer runs on this
     transition — see "Publish-Time Topical Dedup" below; this path is effectively dead
     under auto-pilot anyway, since autopilot never creates a `Pending` card)
   - **Publish Now** → mark page `Queued` first, then call `publish_agent.publish_one(article_id)`
     immediately (bypass queue)
   - **Rejected** → `DEL review:pending:{id}`
5. Page `Decision` updated to `Published` / `Discarded` to prevent re-processing

If the Redis hash has expired (articles can sit in review longer than the 24 h TTL), `_handle_decision`
rebuilds it from the Notion page properties — Title, Post Content, Source URL, image, `notion_page_id`
— with a 1 h TTL, enough to survive the publish.

### Publish-Time Topical Dedup (DailyNews only)
**Rewritten 2026-07-28** — this used to be an approval-time check (`is_duplicate()`, one Claude
Haiku call per candidate pair against `daily_news` + `agent_queue_dailynews`) hung off the
`Approved` transition above. That transition never fires under auto-pilot (both pipelines run
full auto-pilot), so the check silently never ran, and it only ever compared the bot against its
own review DB (`agent_queue_dailynews`) — never against `daily_news`, the board a human editor
actually uses (a separate, still-running hourly n8n workflow publishes from there).

`NotionTopicalDedupChecker` (`services/notion_topical_dedup.py`) now runs inside
`PublishAgent._process_one`/`_mark_posted` instead — the one code path both auto-pilot and manual
mode go through — using OpenAI embedding cosine similarity (`similarity_threshold`, default 0.80)
against `daily_news` only:
- `before_publish()`: candidate vs. `daily_news` cards with `send_status=True` and the built-in
  **Last edited time** (not `TimerForPub`, which no editor uses) within `recent_lookback_hours`
  (default 24h). A match means a human editor already published the same story.
  `enforce_recent_skip` (default **False**) gates whether a match actually skips publishing or
  just logs what it would have skipped — kept off until validated against live traffic, so this
  can't yet reduce production posting volume by mistake.
- `after_publish()`: candidate (just posted) vs. not-yet-sent `daily_news` cards (`send_status=False`,
  `status` in `2nd_eye` / `waiting for post`). A match sets that card's `Duplicate` select and
  appends the new Gettr post's link to `Notes` — informational for the editor; it does **not**
  stop the hourly n8n job from publishing that card anyway, since that workflow's own Notion query
  filter has no `Duplicate`-exclusion condition (a known, deliberately deferred gap).
- `run_gettr_crosscheck_loop()`: a separate, independent loop (every
  `gettr_crosscheck_interval_minutes`, default 15) that compares not-yet-sent `daily_news` cards
  against this pipeline's own recent Gettr posts (`services/gettr_feed_client.py`, the public
  unauthenticated `GET /u/user/{handle}/posts` endpoint) — catches duplicates that never had a
  live Notion candidate to diff against at either hook above.

Exception in any of the three → logged and **allowed through** (fails open). Notion page IDs for
matched pending cards are looked up fresh each call (small `daily_news` query, not cached);
per-page embeddings are cached in-process keyed by page id + Last edited time.

### Autopilot Mode
`review_agent.autopilot = True`: `enqueue_article()` pushes the article_id straight to the publish
queue, then creates the Notion page with **`Decision = "Queued"`**.

Creating it as `Queued` rather than `Approved` is load-bearing: the poll loop only queries
`Approved`/`Rejected`/`Publish Now`, so a `Queued` page is never picked up and the article is
enqueued exactly once. When these pages were created as `Approved`, the poll detected them and
pushed a second copy of every autopilot article to the publish queue.

In manual mode the page is created as `Pending` and nothing is queued until an editor approves.

### Notion Page Fields
- **Title** — article title
- **URL** — article URL
- **Source** — feed name / @handle
- **Score** — LLM score (number property)
- **Score Label** — 🔵/🟢/🟡/🟠/🔴 label
- **Comment** — LLM comment (≤120 chars)
- **Post Content** — generated post text (rich_text)
- **Edit Override** — editor fills this to override post on approval
- **Decision** — select: Pending / Approved / Rejected / Published / Discarded / Publish Now
- **Published At** — article publication date
- **Image** — OG image URL embedded as image block in page body

### Pipeline Isolation via Key Overrides
```python
NotionReviewAgent(config, redis, redis_key_overrides={
    "review_pending_prefix": "epicfury:review:pending:",
    "review_queue_key":      "epicfury:review:queue",
    "publish_queue_key":     "epicfury:publish:queue",
})
```

---

## 10. HotTopicsStore (DailyNews only)

Persists to `data/hot_topics.json`. Used by `Pipeline._rank_articles()` to prioritize which articles get post generation attempted.

- **Keywords**: case-insensitive substring match on `title + description`
- **Semantic threshold**: cosine similarity of article embedding vs embedded keywords (default 0.75)
- **Selection**: hot-topic matches (keyword OR semantic) → sorted by `llm_score` desc; non-matches follow sorted by `llm_score` desc
- **Fallback**: if no `hot_topics_store` or no keywords → all articles sorted by score desc
- **Embedding cache**: in-memory, invalidated whenever keywords change

Dashboard API: `GET/POST /api/hot_topics` to read/update keywords and threshold.

---

## 11. SimilarityAgent — Stage Architecture

Split into three separately-called stages so each emits its own SSE event with precise metrics:

```python
embeddings = await similarity.run_embed(articles)           # → OpenAI API call
articles, embeddings = await similarity.run_within_batch(articles, embeddings)  # cosine 0.70
articles = await similarity.run_cross_batch(articles, embeddings)               # Qdrant 0.80
```

A `run(articles)` convenience wrapper calls all three in sequence.

`set_thresholds(within, cross)` updates thresholds at runtime without restart.

`SimilarityAgent` has exactly two stages. An earlier embedding-based "Stage 3 — Notion Dedup" that
ran inside `run_cross_batch()` has been removed; the equivalent check now happens at publish time
via `NotionTopicalDedupChecker` (§9).

---

## 12. ClaudeClient

### Scoring
- Model: `claude-haiku-4-5`, **batch size 5, max_tokens 1500** (`config.yaml`; pydantic defaults
  are 10 / 512). See §5 Step 7 for why these must not be reverted.
- Prompt: loaded from `prompts/score_articles.txt` (default) or path override
- `reload_prompts()` re-reads all prompt files from disk

### Post Generation
- Primary: OpenAI `gpt-4o-mini` (temperature 0.1)
- Fallback: Claude (same model as scoring)
- Article body: self-hosted `extract-premium` endpoint first → `trafilatura.fetch_url()` thread executor (20s timeout) as fallback
- Bot-protection detection: pages matching Cloudflare/CAPTCHA/JS-disabled patterns are rejected; generator falls back to title + description
- `always_rewrite=True`: all posts go through LLM rewrite regardless of length/language
- Body truncated to 4000 chars before being sent to the model

#### Word-Count Enforcement (`_WORD_MIN = 55`, `_WORD_LIMIT = 75`)

```
source body
  ├── English and < 40 words ─────────────▶ return None (article dropped)
  └── otherwise ─▶ LLM call
        ├── 55–75 words ──────────────────▶ accept
        └── outside range ─▶ ONE retry with a targeted reminder message:
              > 75 words → _RETRY_TOO_LONG  ("exceeded the 75-word hard limit")
              < 55 words → _RETRY_TOO_SHORT ("too short … two full paragraphs")
              └── retry result is used regardless of its length (logged as a warning)
```

The retry is a real multi-turn exchange for the Anthropic path (`user` → `assistant` → `user`
reminder). The OpenAI path is single-turn only, so the prior response and the reminder are folded
into one combined user message.

Output is **never trimmed** programmatically — a post that survives the retry is published as the
model wrote it. The 55-word floor in `PublishAgent` (§8) is what actually stops a too-short post
from reaching Gettr.

#### Refusal Detection
`_REFUSAL_PATTERNS` catches models declining to write a post; a match makes `generate_post()`
return `None` so the article is dropped rather than posting the refusal text. Covered phrasings
include the explicit `[SKIP]` token, `does not provide/contain/include …`, `no suitable content`,
`cannot be generated based on`, `does not meet the criteria`, `please provide an article`, and
`I cannot/can't/am unable to generate|write|create|produce`.

### Image Pre-generation (Post Generation step)
If `url_to_image` is missing or a logo placeholder:
1. Generate via Pollinations (`flux` model, deterministic seed `int(url_hash[:8], 16)`)
2. Upload bytes directly to GCP CDN via `GcpClient.upload_bytes()` — avoids Pollinations cache eviction
3. Replace `url_to_image` with stable CDN URL

---

## 13. GemmaClient — Content Verification (DailyNews only)

Called after post generation, before Notion enqueue.

- API: Google AI Studio via OpenAI-compatible endpoint (`generativelanguage.googleapis.com`)
- Model: **`gemma-4-31b-it`** (configurable — `gemma.model`; also the `core/config.py` default)
- System prompt: `prompts/verify_post.txt`
- Input: article title + generated post
- Output verdicts, parsed from a `**VERDICT:** PASS|FAIL|REVISE` line:
  - `PASS` — enqueue as-is
  - `REVISE` — **drop article** (the revised text is not used)
  - `FAIL` — drop article
  - no `**VERDICT:**` line found → treated as `FAIL`
  - API exception → returns `PASS` (fails open, so an outage can't stall the pipeline)

Disabled if `gemma.enabled = false` in config or `gemma.api_key` is empty (logs a warning).

`gemma.model` is passed verbatim as the `model` field to the Google AI Studio OpenAI-compatible
endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`). `config.yaml` uses the bare
name `gemma-4-31b-it` (previously `models/gemma-4-31b-it`).

---

## 14. Redis Key Schema

Prefixes are **not** hardcoded — DailyNews reads `config.redis.*`, Epic Fury reads
`config.epicfury.redis_*`, and extra channels derive theirs from the channel `slug`. The values
below are what `config.yaml` currently sets.

### DailyNews Pipeline (`config.redis.*`)
| Key | Type | TTL | Purpose |
|---|---|---|---|
| `newsrooms:dailynews_v2_test:title_hash:{sha256_16}` | String | 10800s | URL dedup (RssAgent) |
| `review_test:pending:{article_id}` | Hash | 86400s | Article data |
| `review_test:queue` | List | — | Awaiting review |
| `publish_test:queue` | List | — | Approved for publish |
| `newsroom:dailynews_v2_test:post:{sha1_12}` | String | 864000s | Post dedup (content-based) |
| `newsroom:dailynews_v2_test:post:article:{article_id}` | String | 864000s | Post dedup (article-ID based) |
| `newsroom:dailynews_v2_test:post:videogen:24h` | **ZSet** | 172800s | AI video quota, rolling 24h (§22) |
| `newsroom:dailynews_v2_test:post:imgseen:{sha256_16}` | String | 604800s | Repeat-image counter for source-logo detection |

### Epic Fury Pipeline (`config.epicfury.redis_*`)
| Key | Type | TTL | Purpose |
|---|---|---|---|
| `epicfury:title_hash:{sha256_16}` | String | 10800s | URL dedup (X + websites) |
| `epicfury:review:pending:{article_id}` | Hash | 86400s | Article data |
| `epicfury:review:queue` | List | — | Awaiting review |
| `epicfury:publish:queue` | List | — | Approved for publish |
| `epicfury:post:{sha1_12}` | String | 864000s | Post dedup (content-based) |
| `epicfury:post:article:{article_id}` | String | 864000s | Post dedup (article-ID based) |
| `epicfury:post:videogen:24h` | **ZSet** | 172800s | AI video quota, rolling 24h (§22) |
| `epicfury:post:imgseen:{sha256_16}` | String | 604800s | Repeat-image counter for source-logo detection |

Both post-dedup keys share the `post_hash_key_prefix`; the article-ID variant just appends
`article:{article_id}` instead of the SHA1. Both are written together in `_mark_posted()`.
The `videogen:24h` and `imgseen:` keys derive from the same prefix, so each pipeline — and
each config-driven channel — gets its own counters with no extra configuration.

`videogen:24h` is a **sorted set**, not a counter: member = `article_id`, score = epoch
seconds. Every read first runs `ZREMRANGEBYSCORE key -inf now-86400`, which makes the window
genuinely rolling rather than "24h from the first video".

### Hash Algorithms
- **URL dedup:** `sha256(url_without_query_params).hexdigest()[:16]`
- **Post dedup:** `sha1(post_content[:500] + first_media_url).hexdigest()[:12]`
- **Article-ID dedup:** no new hash — `article_id` *is* `article.url_hash` (a random 12-char UUID
  slice only if `url_hash` is empty), so this key is effectively keyed to the source URL
- **Qdrant point ID:** `int(md5(url_hash).hexdigest()[:16], 16) % 2^63`

---

## 15. Dashboard

### Architecture
Single Alpine.js SPA (`dashboard/templates/index.html`). Real-time via SSE (`/api/sse`). Two separate `web.Application` instances — one per pipeline per port.

`pipeline_type` injected at render time (`"rss"` | `"epicfury"`) — determines graph node labels.

**Auth:** HMAC-SHA256 session tokens; password hash stored in `config.yaml`; no third-party auth library.

**Config editor:** reads/writes `config.yaml` live; masks sensitive keys (`api_key`, `bot_token`, `user_token`, `password_hash`, `smtp_password`) in the browser; restores originals on save using sequential positional matching.

### SSE Event Types

| Event | Payload |
|---|---|
| `run_start` | `{run_id, run_type, ts}` |
| `step_start` | `{run_id, step}` |
| `step_done` | `{run_id, step, articles_in, articles_out, articles_dropped, duration_ms}` |
| `step_error` | `{run_id, step, error}` |
| `run_done` | `{run_id, status, total_duration_ms}` |
| `heartbeat` | queue lengths, countdown timers, thresholds, autopilot state (every 30s) |
| `schedule_update` | `{rss_interval_s, publish_interval_s, rss_paused, publish_paused}` |
| `log` | `{ts, level, message}` |

### Routes

| Route | Purpose |
|---|---|
| `GET /` | SPA |
| `GET /api/sse` | SSE stream |
| `POST /api/trigger/{rss\|publish}` | Manual run |
| `POST /api/cancel/{rss\|publish}` | Cancel in-progress run |
| `POST /api/schedule` | Update interval / publish interval / pause |
| `GET /api/queue` | Review queue contents |
| `POST /api/queue/{id}/{approve\|reject}` | Approve/reject from browser |
| `GET /api/history` | SQLite run history |
| `GET /api/health` | Redis + Qdrant + API status |
| `GET/POST /api/config` | Read/write config.yaml |
| `GET/POST /api/prompts/{name}` | Read/edit prompt files |
| `GET/POST /api/hot_topics` | Read/update hot topics keywords + threshold |

### Pipeline Graph Nodes

**DailyNews (port 8080):**
`Notion → RSS Fetch → URL Dedup → Embeddings → Within Batch → Cross Batch → Score → Post Gen → Gemma → Notion`

**Epic Fury (port 8081):**
`X + Web → URL Dedup → Embeddings → Within Batch → Cross Batch → Score → Post Gen → Notion`

---

## 16. Configuration Reference

Hot-reloadable via `sudo systemctl kill -s SIGUSR1 dailynews-agent` or the dashboard Config tab.
SIGUSR1 reloads `config.yaml` credentials/settings only — it does **not** reload Python code.

### ⚠️ Precedence: `data/schedule*.json` overrides `config.yaml`

For thresholds, intervals and toggles, `config.yaml` is **not** the runtime source of truth.
`DashboardState.__init__` reads `data/schedule.json` (DailyNews) or `data/schedule_ef.json`
(Epic Fury) and those values win; `main.py:403,417` then pushes them into the live agents via
`similarity_agent.set_thresholds()` and `pipeline.set_filter_score_threshold()`.

`__init__` also calls `save_schedule()` unconditionally, so the JSON file is written on the first
ever startup. From then on the `config.yaml` entries for these fields are only consulted as the
fallback for a key missing from the JSON — in normal operation they are dead.

| Field | Fallback when the key is absent from the JSON |
|---|---|
| `rss_interval_s` | **600, hardcoded in `state.py`** — not read from config.yaml |
| `publish_interval_s` | **3600, hardcoded** — not read from config.yaml |
| `rss_paused`, `publish_paused` | `False` |
| `filter_score_threshold` | `claude.filter_score_threshold` / `epicfury.filter_score_threshold` |
| `within_batch_threshold`, `cross_batch_threshold` | the matching `qdrant*` section |
| `notion_dedup_threshold` | `qdrant_notion.cross_batch_threshold` — **inert**: it only assigns `SimilarityAgent._notion_dedup_threshold`, which no code path reads since Stage-3 dedup was removed (§5 Step 10) |
| `autopilot` | `False` |
| `verify_enabled` | `True` — gates the Gemma step at `core/pipeline.py:410`, so verification can be OFF at runtime even with `gemma.enabled: true` |
| `x_scraper` | `"twitterapi"` |

All of these are editable live from the dashboard, which rewrites the JSON. Editing the JSON by hand
has no effect until a restart. **To determine what is actually running, read the JSON files or the
dashboard — not `config.yaml`.**

### Sections

| Section | Key Fields |
|---|---|
| `app` | `log_level` |
| `notion` | `api_key`, `rss_database_id` (RSS source DB) |
| `notion_dedup` | `api_key`, `article_database_id`, `similarity_threshold` (0.80), `recent_lookback_hours` (24), `enforce_recent_skip` (False), `gettr_handle` ("dailynews"), `gettr_crosscheck_interval_minutes` (15) — credentials + thresholds for the publish-time topical dedup checker (§9) |
| `notion_review` | `api_key`, `review_database_id`, `poll_interval_s` (DailyNews review DB) |
| `notion_review_epicfury` | Same as `notion_review` for Epic Fury |
| `rss` | `filter_feed_hours`, `max_feed_items`, `max_feed_per_source`, `concurrency` |
| `redis` | `url`, `url_hash_key_prefix`, `review_pending_prefix`, `review_queue_key`, `publish_queue_key`, `post_hash_key_prefix` |
| `openai` | `api_key`, `embedding_model`, `scoring_model`, `post_gen_model`, `embedding_batch_size` |
| `qdrant` | `url`, `api_key`, `collection`, `within_batch_threshold`, `cross_batch_threshold`, `cross_batch_hours` |
| `qdrant_notion` | **Unused** — leftover from the removed ingestion-time Stage-3 Notion dedup |
| `qdrant_epicfury` | Same as `qdrant`, separate collection + credentials for Epic Fury |
| `claude` | `api_key`, `filter_score_threshold`, `batch_size`, `max_tokens` |
| `gemma` | `api_key`, `model`, `max_tokens`, `enabled` |
| `channels` | top-level **list** of extra EF-style channels (see §19) |
| `image_gen` | `pollinations_api_key`, `enabled` |
| `metadata_api` | `urlmeta_api_key`, `youtube_api_key`, `self_hosted_url`, `self_hosted_api_key`, `url_resolver_url`, `extract_premium_url`, `no_preview_domains` |
| `telegram` | `bot_token`, `editor_chat_id` (unused — kept for reference) |
| `gettr` | `api_url`, `user_id`, `user_token` (DailyNews) |
| `gettr_epicfury` | `api_url`, `user_id`, `user_token` (Epic Fury) |
| `gcp` | `user_agent`, `resumable_upload_timeout_s`, `download_timeout_s`, `download_proxy_url`, `download_proxy_api_key` |
| `dashboard` | `port` (8080), `port2` (8081), `password_hash`, `session_secret`, `enabled` |
| `epicfury` | `sources_md_path`, `filter_feed_hours`, `filter_score_threshold`, `keywords[]`, `x.*`, `twitterapi.*`, `socialdata.*`, `x_scraper`, `redis_*` keys |

### `epicfury.twitterapi` Fields

| Field | Default | Purpose |
|---|---|---|
| `api_key` | `""` | twitterapi.io key (empty = skip to Syndication API) |
| `base_url` | `https://api.twitterapi.io` | API base URL |
| `tweets_per_account` | `20` | Max tweets per handle per cycle |

### `epicfury.socialdata` Fields

| Field | Default | Purpose |
|---|---|---|
| `api_key` | `""` | socialdata.tools key (empty = skip to Syndication API) |
| `base_url` | `https://api.socialdata.tools` | API base URL |
| `tweets_per_account` | `20` | Max tweets per handle per cycle |

### `epicfury.x_scraper`
`"twitterapi"` (default) or `"socialdata"` — selects which paid API is tried first. Toggleable from the Epic Fury dashboard without restart; persisted to `data/schedule_ef.json`.

### Critical Constants

These are the values from `config.yaml`. Two caveats:

1. Everything in the **"runtime-tunable" group below is overridden by `data/schedule*.json`** — see
   the precedence section above. Those rows are startup fallbacks, not what is running.
2. The rest (TTLs, word counts, timeouts, image limits) are genuinely fixed at these values —
   they are code constants or config-only, with no dashboard control.

Runtime-tunable — **`config.yaml` value shown; the live value is in `data/schedule*.json`**:

| Constant | config.yaml | live as of 2026-07-25 (DN / EF) |
|---|---|---|
| Ingestion interval | n/a (hardcoded 600s fallback) | 30 min / 15 min |
| Publish interval | n/a (hardcoded 3600s fallback) | 30 min / 15 min |
| Score threshold | 6.0 / 6.0 | **6.5** / 6.0 |
| Within-batch cosine threshold | 0.70 / 0.70 | 0.70 / **0.65** |
| Cross-batch cosine threshold | 0.80 / 0.75 | **0.70** / **0.65** |
| Autopilot | n/a (`False` fallback) | **ON** / **ON** |
| Gemma verification | `enabled: true` | ON / ON (EF never calls it) |

Fixed:

| Constant | Value |
|---|---|
| RSS fetch window | 2 hours (`filter_feed_hours`; pydantic default 3) |
| URL dedup TTL | 10800s (3h) — must be `>=` fetch window |
| Post dedup TTL | 864000s (10 days) — both SHA1 and article-ID keys |
| Cross-batch time window | 48 hours |
| Topical dedup window | 24 hours |
| Epic Fury video score boost | +1.0, applied before the threshold test |
| Scoring batch size | 5 (pydantic default 10) |
| Scoring max_tokens | 1500 (pydantic default 512) |
| Post length target | 55–75 words |
| Post generation hard max | 75 words (one retry, then accepted as-is) |
| Source-article drop floor | 40 words (English bodies only) |
| Publish-time word floor | 55 words (`_DN_MIN_WORDS`, DailyNews only) |
| Article body fetch timeout | 20s |
| Article body truncation | 4000 chars |
| GCS upload timeout | 60s |
| GCS download timeout | 30s |
| Min image area | 120,000 px² |
| Min image short side | 120 px |
| Notion review poll interval | 30s |

---

## 17. Self-Hosted Server Endpoints

All three endpoints run on `n8n-svr.gettr.fyi:7771` with `X-API-Key` header authentication.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/url/final` | POST `{"url": ...}` | Resolve redirect chain → `{"final_url": ...}` |
| `/api/v1/media/download` | POST `{"url": ...}` | Proxy-download media → raw bytes |
| `/api/v1/website/metadata` | POST `{"url": ...}` | Scrape OG metadata → `{"image": ..., "title": ..., "description": ...}` |
| `/api/v1/article/extract-premium` | POST `{"url": ...}` | Extract article body → plain text |

Config keys: `metadata_api.self_hosted_url`, `metadata_api.self_hosted_api_key`, `metadata_api.url_resolver_url`, `metadata_api.extract_premium_url`, `gcp.download_proxy_url`, `gcp.download_proxy_api_key`.

---

## 18. Process Management

### Startup Sequence
1. Load `config.yaml`
2. Connect Redis (Upstash TLS)
3. Init OpenAI, Qdrant, Claude, GcpClient, Gettr, Notion
4. Init `NotionTopicalDedupChecker` if `notion_dedup.api_key` is set (DailyNews only) and assign it
   to `publish_agent._topical_dedup` — this is what makes §9's before_publish/after_publish hooks
   active; if `notion_dedup.api_key` is unset, `PublishAgent` just skips both hooks (`None` check)
5. Init GemmaClient if `gemma.enabled` + `gemma.api_key` set
6. Init DailyNews pipeline (RssAgent, SimilarityAgent, NotionReviewAgent, PublishAgent, Pipeline, HotTopicsStore)
7. Init SQLite run history DB
8. If `config.epicfury` present → create `state_ef`; if EF credentials present → init XAgent, WebsiteAgent, SimilarityAgent (EF collection), NotionReviewAgent (EF keys), PublishAgent (EF), Pipeline (EF). Failures crash the process (so process manager can restart).
9. For each entry in `config.channels`: `channel_runtime.build_channel()` (§19)
10. Register SIGINT/SIGTERM/SIGUSR1 handlers
11. `asyncio.gather(all tasks)` — includes `NotionTopicalDedupChecker.run_gettr_crosscheck_loop()`
    as its own task when the checker was initialized in step 4 (DailyNews only)

### Running Tasks
- `review_agent.start_card_sender()` — drains Notion card queue, also runs `_poll_loop()`
- `rss_loop(pipeline, state)` — DailyNews ingestion every N seconds
- `publish_loop(publish_agent, state)` — DailyNews publish, polled every 60s
- `dashboard_loop(state, ..., port=8080)` — aiohttp + SSE heartbeat
- *(if ef_enabled)*: `review_ef.start_card_sender()`, `rss_loop(pipeline_ef, state_ef)`, `publish_loop(publish_ef, state_ef)`
- *(if config.epicfury)*: `dashboard_loop_ef(state_ef, ..., port=8081)`

### Commands

The process is managed by **systemd** (unit `dailynews-agent.service`, `User=leon`,
`Restart=always`, enabled at boot). Do not start a manual `nohup` instance — it collides on port
8080 with the managed process, and killing the managed process only makes systemd relaunch it 10s
later.

```bash
# Status
systemctl status dailynews-agent
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login   # 200/302 = healthy

# Restart (required after any .py edit)
sudo systemctl restart dailynews-agent

# Live logs (also appended to data/main.log, which the dashboard tails)
journalctl -u dailynews-agent -f

# Reload credentials only — SIGUSR1 does NOT reload Python code
sudo systemctl kill -s SIGUSR1 dailynews-agent

# Set dashboard password
python3 -m dashboard.setup_password
```

Prompt files (`prompts/*.txt`) need no restart at all: the dashboard's
`POST /api/prompts/{name}` calls `pipeline.reload_prompts()`.

⚠️ `pgrep`/`pkill -f "python.*main.py"` self-matches the shell running it (the pattern appears in
its own cmdline). Use systemd rather than ad-hoc kills.

### Host Notes
3.8 GiB RAM plus a 4 GB `/swapfile` (persisted in `/etc/fstab`, `vm.swappiness=10`), added after
an OOM kill took the process down.

---

## 19. Config-Driven Channels

DailyNews and Epic Fury are hardcoded in `main.py`. **Any additional channel is pure config** — no
Python edits. Each entry in the top-level `channels:` list of `config.yaml` becomes an independent
Epic-Fury-style pipeline: its own X/website sources, prompts, dedup namespace, Notion review board,
Gettr account, and dashboard port.

`main.py` iterates `config.channels` and calls `core/channel_runtime.build_channel()`, which mirrors
the Epic Fury init block (`pipeline_type="epicfury"` behaviour throughout).

### `ChannelConfig` (`core/config.py`)

| Field | Default | Purpose |
|---|---|---|
| `slug` | *required* | Unique id, lowercase letters/digits (Python-ident + Redis-safe) |
| `title` | `slug.title()` | Dashboard header |
| `dashboard_port` | *required* | Must be free and unique across channels |
| `enabled` | `true` | `false` keeps the config but doesn't start the pipeline |
| `schedule_path` | `data/schedule_<slug>.json` | Persisted thresholds / pause / autopilot state |
| `score_prompt` | `prompts/<slug>_score_articles.txt` | Scoring prompt |
| `post_system_prompt` | `prompts/<slug>_generate_post_system.txt` | Post-gen system prompt |
| `post_user_prompt` | `prompts/<slug>_generate_post_user.txt` | Post-gen user template |
| `video_score_boost` | `1.0` | Same as Epic Fury |
| `source` | `EpicFuryConfig()` | `sources_md_path`, `keywords`, `x.*`, TTLs … |
| `qdrant` | shared `config.qdrant` | Optional per-channel vector store |
| `gettr` | *required* | The channel's Gettr account |
| `notion_review` | `NotionReviewConfig()` | The channel's review board |

### Slug-Derived Namespacing

`ChannelConfig.redis_keys()` builds every Redis prefix from the slug, so two channels can never
collide as long as their slugs differ:

```
<slug>:title_hash:{sha256_16}       <slug>:review:queue
<slug>:review:pending:{article_id}  <slug>:publish:queue
<slug>:post:{sha1_12}               <slug>:post:article:{article_id}
<slug>:tg:msg:
```

SQLite run-history `run_type`s are likewise slug-based: `<slug>` for ingestion, `<slug>_publish` for
publishing.

### Dashboard Reuse

`make_app` takes per-channel identity (`channel_title`, `prompt_files`, `sources_path`, `run_types`).
When those are `None` the handlers fall back to legacy `pipeline_type` behaviour, which is how
DailyNews and Epic Fury keep working unchanged.

Full recipe and copy-paste templates: **`templates/channel/`** (`README.md` + `DEPLOY.md`).

---

## 20. Dependencies

```
aiohttp              # Async HTTP client + web server (all network I/O)
feedparser           # RSS/Atom parsing (run in thread pool executor)
pydantic>=2          # Config + data models
pydantic-settings    # BaseSettings
PyYAML               # config.yaml
redis                # Async Redis client (Upstash)
openai               # Embeddings + post generation + Gemma (OpenAI-compat endpoint)
qdrant-client        # Vector database
anthropic            # Claude API
numpy                # Cosine similarity
structlog            # Structured logging
orjson               # Fast JSON
trafilatura          # Article body extraction + URL fetching
lxml_html_clean      # trafilatura HTML cleaning dependency
Pillow               # Image dimension validation (publish_agent)
```

### X.com Scraping — No Extra Library

| Source | Transport | Auth |
|---|---|---|
| twitterapi.io | `aiohttp` | `X-API-Key` header |
| socialdata.tools | `aiohttp` | `Authorization: Bearer` header |
| Twitter Syndication API | `aiohttp` | None (public) |
| Nitter RSS | `aiohttp` + `feedparser` | None (public) |
| CDN tweet-result (video fallback) | `aiohttp` | Computed token |

---

## 21. Editor Review A/B Branch (DailyNews only)

### Purpose

Trial a different editorial voice — a three-prompt editor chain — without disturbing the live
channel. Every story DailyNews publishes goes out **twice**: the standard treatment to the live
`gettr` account, the editor treatment to a second `gettr_test` account. Same story, same image,
same moment, so the editorial voice is the only variable.

Off by default (`editor_review.enabled: false`). EpicFury and config-driven channels never touch it.

### Flow

```
claude_score (qualifying articles)
    │
    ├── editor_review ──── triage ──REJECT──▶ no variant (article unaffected)
    │      (DN only)          │ PUBLISH
    │                    ccp_exposure
    │                         │
    │                   chinax_style ──▶ article.editor_post (FINISHED post)
    │                         │
    └────────┬────────────────┘
             │
         post_gen
           body → llm_post   (standard post only — the variant is already done)
             │
        verify (llm_post only) → dedup → Notion enqueue
             │
        Redis review:pending:{id} carries `editor_post` alongside `post_content`
             │
        PUBLISH RUN — one article, best score first
           POST llm_post + image    → live Gettr    ✓ marks posted, updates Notion
           POST editor_post + image → test Gettr    (best-effort twin, no side effects)
```

### Components

| Piece | Location |
|---|---|
| Three-prompt chain | `services/editor_client.py` — `EditorReviewClient` |
| Prompt files | `prompts/ai_editor_intake_triage_prompt.md`, `ai_editor_ccp_exposure_system_prompt.md`, `unveiled_chinax_style_prompt.md` |
| Ingestion stage | `core/pipeline.py` — `editor_review` step (§5, Step 4b) |
| Style pass + length guard | `EditorReviewClient._style_pass()` |
| Transport to publish | `ReviewItem.editor_post` → Redis review hash |
| Twin post | `PublishAgent._post_editor_twin()` |
| Config | `Config.editor_review` (`EditorReviewConfig`), `Config.gettr_test` (`GettrConfig`) |

### Design decisions and why

**A finished post, published verbatim.** The chain's output goes straight to the test account and is
never re-run through `generate_post`. The editor prompts carry their own voice, structure and length
rules; a fourth rewrite under `generate_post_system.txt` measurably strips the signature closing
question and the prosecutorial voice — the very thing the A/B is measuring.

### The style-pass length guard

`unveiled_chinax_style_prompt.md` is a **descriptive** document — a reverse-engineered analysis of an
account that posts ~150–300 word four-paragraph briefs. Used directly as a system prompt it obeys its
own description and inflates a compact draft: measured at **78 → 161 words**, roughly double the
55–75 target set in the CCP prompt.

`EditorReviewClient._style_pass()` therefore wraps step 3 in per-call rules that make it a voice pass:
a word ceiling, "keep the paragraph count", "add no new facts", plus one retry when the output exceeds
`_STYLE_LENGTH_TOLERANCE` (1.15×) of the draft. As in `generate_post`, an over-length retry result is
used anyway rather than trimmed mid-sentence.

The ceiling is **the draft's own word count, not a hardcoded number** — the length target stays owned
by the CCP prompt, so changing it there propagates automatically with no code edit.

Measured on the same story, five runs: **152/153/156 → 77/80/75/78/76 words**.

**Twin, not an independent lane.** DailyNews publishes exactly one article per run, best-score
first, and drops on no-image. A second queue with its own selection would post *different* stories
and the comparison would be meaningless. Slaving the variant to the live post guarantees a 1:1 pair.

**Best-effort, strictly downstream.** `_post_editor_twin()` runs after the live POST returned
success. It catches every exception and returns `None`. It touches no Redis dedup key, no Notion
page, and no return value. A dead test account produces one log line and nothing else.

**No dedup keys of its own.** The twin can only fire for an article that already cleared the 55-word
floor, article-ID dedup and SHA1 dedup on the live path, so it cannot double-post.

**The image is uploaded twice.** `GcpClient` requests its upload channel with the account's own
Gettr auth (`_get_upload_channel`), so live-account CDN metadata is not valid for the test account.
The test lane gets its own `GcpClient` built from `config.gettr_test`.

**No Notion involvement.** The variant rides in the existing Redis review hash. No extra cards, no
schema change, and no risk of the twin tripping the publish-time topical dedup against itself.
*Known gap:* `_handle_decision` rebuilding an expired hash from Notion cannot restore `editor_post`,
so an article resurrected that way publishes to the live account only.

**Exempt from `_DN_MIN_WORDS`.** The 55-word floor is a live-channel quality gate. The twin logs its
word count instead, so short variants are visible without being silently dropped.

**Gemma verifies `llm_post` only** — see §5, Step 6.

### Configuration

```yaml
gettr_test:                   # omit the block entirely to disable twin posting
  api_url: "https://gettr.com/api/u/post"
  user_id: "..."
  user_token: "..."

editor_review:
  enabled: false
  triage_prompt: "prompts/ai_editor_intake_triage_prompt.md"
  ccp_prompt: "prompts/ai_editor_ccp_exposure_system_prompt.md"
  style_prompt: "prompts/unveiled_chinax_style_prompt.md"
  max_per_run: 0              # 0 = all qualifying articles; N = top N by score
  max_tokens: 2000
  model: null                 # null → openai.post_gen_model / claude.scoring_model
```

Both blocks are edited in the dashboard **Config** tab (validated raw `config.yaml`; `user_token` is
auto-masked). The three prompts are edited in the **Prompts** tab and reload through
`EditorReviewClient.reload_prompts()` — routed by `_EDITOR_PROMPTS` in
`dashboard/handlers/config_editor.py`, *not* the `claude_client` fallback branch.

**Restart is required only when adding a `gettr_test` block that was absent at boot** — the
`GettrClient` / `GcpClient` pair is constructed in `main.py`. Credential edits, `enabled`,
`max_per_run` and prompt edits all hot-reload (`_reload_live_clients()`, also wired to SIGUSR1).

### Cost

3 LLM calls per qualifying article for the chain, plus one extra post generation — while only one
article per publish run is actually posted. `max_per_run` caps the branch to the top N by score if
that becomes material. A startup log line reports whether the branch and the test account are live:

```
Editor A/B branch: review=ON, test account=<user_id>
```

---

## 22. AI Video Fallback (posts with no usable image)

DailyNews' oldest rule is "no image = no post": a story without a picture is silently
discarded. This turns that terminal drop into a second chance — the post text becomes a
~25s narrated motion-news video. Gated by a dashboard switch **and** a rolling 24h cap,
because each render costs ~90s of near-100% CPU on both cores of a 2-vCPU box.

### 22.1 Components

| Piece | Role |
|---|---|
| `video/scripts/` | The generator — a vendored Claude Code skill (`nfsctech/short-news-video`). See `video/UPSTREAM.md` for provenance and every local patch. |
| `video/brand/<slug>/` | Per-channel `brand.json` + `logo.png` + `outro.mp4`; falls back to `video/brand/dn/`. |
| `services/video_client.py` | Async wrapper: builds the brief, runs the render as a subprocess, returns MP4 bytes. |
| `utils/logo_detect.py` | Decides that a masthead-only image counts as "no image". |
| `prompts/video_brief.txt` | LLM prompt producing the chyron headline, media search subjects and card labels. Dashboard-editable; hot-reloads via `VideoClient.reload_prompts()`. |
| `agents/publish_agent.py` | `_try_video()` — switch, quota, generate, upload. |

### 22.2 Flow

```
_try_video(article_id, data, post_content, reason)
   │
   ├─ video client absent, switch off, or max_24h == 0 ──▶ None
   ├─ ZREMRANGEBYSCORE + ZCARD >= max_24h              ──▶ None   (quota spent)
   │
   ├─ brief: OpenAI chat_complete(prompts/video_brief.txt)
   │     → { headline (<=8 words), subjects [4-5], labels [2-4] }
   │     → on ANY failure, falls back to the article title
   │
   ├─ render: nice -n 10 python video/scripts/make_news_video.py
   │     under Semaphore(1) + asyncio.wait_for(timeout_s)
   │     → TTS (edge-tts) → subtitles → Wikimedia media per subject
   │       → music bed → production.json → ffmpeg → final.mp4
   │     → timeout kills the whole process group (start_new_session=True)
   │     → workdir data/videogen/<article_id>/ removed in `finally`
   │
   ├─ reject if any scene has rights_verified == false ──▶ None
   ├─ ZADD videogen:24h  (charged on RENDER, not on post)
   │
   └─ gcp.upload_bytes(mp4, "video/mp4", media_type="video",
                       extra_meta={duration, vid_wid, vid_hgt})
         → Gettr media metadata, or None on failure
```

`_try_video` never raises. Every `None` path leaves the caller free to do exactly what it
did before this feature existed.

### 22.3 Call sites

| Location | Trigger | If the video declines |
|---|---|---|
| `_publish_dailynews` | `img_url` is absent, a source logo, or too small — all three collapse into one `if not img_url:` exit | source logo → **post the logo**; otherwise drop + Notion "Discarded" |
| `_publish_with_media` | every media URL was skipped or failed to upload | set-aside logos → **upload and post them**; otherwise DN drop · EF OG preview |
| `_publish_without_media` | EpicFury has no media (switch off by default) | OG preview post |

**The source-logo path deliberately differs from the other two.** Before this feature a
masthead image was simply published. If a logo caused a *drop* whenever no video was made,
turning the feature off would start discarding articles that previously went out. So the
logo is preserved (`logo_fallback` / `logo_skipped`) and posted if the video declines.
"No image" and "too small" always dropped, so they keep dropping.

A video post **skips `_post_editor_twin`**. The A/B measures editorial voice, and the twin
re-uploads from source URLs, which a generated video does not have.

The SHA1 dedup key is computed before any of this, from `first_media = ""`, so the dedup
identity is the same whether or not a video was generated.

### 22.4 Output format and why

960x720 (4:3), H.264 `veryfast`, AAC, sentence-level burned-in subtitles, animated
lower-third chyron, Ken Burns stills, ducked music bed, branded outro.

- **Karaoke subtitles are disabled.** The upstream renderer adds one looped full-frame PNG
  ffmpeg input *and* one `overlay` filter **per spoken word** — ~70 extra inputs and a
  70-deep alpha chain for a 30s read.
- **`subjects` is a list, not one query string.** Wikimedia Commons ANDs every term against
  file metadata, so `"Vatican Beijing Catholic bishops China"` returns **zero** results while
  `"Pope Francis"`, `"Vatican City"`, `"Xi Jinping"` each return plenty. `fetch_media.py`
  searches each subject separately with a per-query cap (which also buys visual variety).
  This one detail is the difference between a video of photographs and a video of five plain
  text cards.
- **`--article-url` is never passed**, so no source-article photos are used. Every asset is
  Wikimedia licence-verified or self-produced, credits are burned in automatically, and
  nothing needs human licensing review.

Measured on this host (2 vCPU / 3.9 GB): ~91s wall, ~814 MB peak RSS, 2-10 MB output.

### 22.5 Narration

`edge-tts` (`en-US-AndrewNeural`) — free Microsoft neural voices over an **unofficial**
endpoint. An OpenAI provider exists in `generate_tts.py` but the current key's project has
no speech model enabled (403 on `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts`); enabling one in the
OpenAI dashboard and setting `provider: "openai"` + an OpenAI voice in the brand file is all
it takes to switch. If edge-tts breaks, video generation stops and each pipeline reverts to
its pre-feature behavior — visible only in the logs (`[tts]`, `Video render exited`).

### 22.6 Source-logo detection

`utils/logo_detect.is_source_logo()` — two independent signals, either sufficient:

1. **URL shape** — path/filename matches logo, masthead, placeholder, fallback, favicon,
   avatar, `no-image`, `og-image`, `default-image`, … Anchored on separators so "iconic"
   does not match "icon". Also honours the pre-existing `GENERIC_IMAGE_PATTERNS` (Reuters
   defaults) and `LOGO_ONLY_SOURCE_DOMAINS` (`cls.cn`) discovered for Pollinations.
2. **Repeat use** — `INCR {prefix}imgseen:{sha256_url_hash}`, 7-day TTL; >= 3 distinct
   articles sharing one image marks it a house graphic. Keyed on the URL with query params
   stripped, so the same image at several sizes still collides.

Fails open on Redis errors. Every trip is logged with the URL, so false positives are
visible rather than silent.

### 22.7 Configuration

`VideoGenConfig` appears twice: `Config.video_gen` (DailyNews) and `EpicFuryConfig.video_gen`.
Because `ChannelConfig.source` **is** an `EpicFuryConfig`, the second covers EpicFury and every
config-driven channel, and a channel can override it under its own `source:`.

| Field | Default | Notes |
|---|---|---|
| `enabled` | `false` | first-boot default for the dashboard switch |
| `max_24h` | `0` | rolling cap per pipeline; **0 = off regardless of the switch** |
| `timeout_s` | `480` | process-group kill past this |
| `width` / `height` | `960` / `720` | |
| `brief_model` | `""` | `""` = OpenAI client default |

As with every other tunable, `data/schedule*.json` **overrides** these once it exists —
`enabled` and `max_24h` are live dashboard state (`video_gen_enabled`, `video_gen_max_24h`).
The remaining fields are read from config at construction time and need a restart.

### 22.8 Dashboard

A toggle beside Auto-Pilot showing `🎬 AI Video <used>/<cap>`, and a Settings slider for the
24h cap with a live "N used in the last 24h" readout. Both appear on every dashboard —
config-driven channels report `pipeline_type="epicfury"`, so `x-show` cannot distinguish them
from EpicFury. The usage figure comes from `PublishAgent.video_quota_used()` via the SSE
snapshot and both heartbeats, so the ZSET key and window stay defined in one place.
