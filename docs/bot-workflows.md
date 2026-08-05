# News Bot — Workflow Documentation

**Audience:** Technical teams and management  
**Last updated:** May 2026  
**System:** Automated news curation and publication pipeline running 24/7 on a Google Cloud VM

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [External Services & Accounts](#2-external-services--accounts)
3. [DailyNews Pipeline](#3-dailynews-pipeline)
4. [EpicFury Pipeline](#4-epicfury-pipeline)
5. [Human Review Workflow](#5-human-review-workflow)
6. [Deduplication System](#6-deduplication-system)
7. [Scheduling & Auto-Pilot](#7-scheduling--auto-pilot)
8. [Admin Dashboard](#8-admin-dashboard)
9. [Data Flow Summary](#9-data-flow-summary)

---

## 1. System Overview

The News Bot is a Python-based automation system that runs two concurrent news pipelines:

| Pipeline | Purpose | Source Type | Publication Account |
|---|---|---|---|
| **DailyNews** | U.S.-China / CCP-focused geopolitical news | 109 RSS feeds | Gettr @dailynews |
| **EpicFury** | Military / Middle East breaking news | 11 X/Twitter accounts + 17 news websites | Gettr @epicfury |

Both pipelines share the same software infrastructure but operate independently, with separate Redis queues, Notion databases, and Gettr accounts.

### What the bot does
1. Automatically fetches news from configured sources every 10 minutes
2. Scores each article using an AI model (Claude) to determine relevance
3. Generates a short social media post (55–75 words) for qualifying articles
4. Removes duplicate or near-duplicate content
5. Sends candidate articles to human editors for review via Notion
6. Upon editor approval, publishes the post to Gettr with associated media

---

## 2. External Services & Accounts

### Publication Platform
| Service | Purpose | Account |
|---|---|---|
| **Gettr** | Social media platform where posts are published | @dailynews (DailyNews), @epicfury (EpicFury) |

### AI / LLM Services
| Service | Model Used | Purpose |
|---|---|---|
| **Anthropic Claude** | Claude Haiku 4.5 | Article relevance scoring, post text generation |
| **Google AI Studio** | Gemma 4 (31B) | Content verification — checks posts for editorial quality (DailyNews only) |
| **OpenAI** | text-embedding-3-small | Converts article titles/descriptions into vector embeddings for semantic dedup |

### Data Storage
| Service | Purpose |
|---|---|
| **Upstash Redis** | Message bus between pipeline stages; URL dedup cache; publish queues |
| **Qdrant Cloud** | Vector database for semantic (cross-batch) duplicate detection; 48-hour window |
| **Notion** | Human review interface; RSS source configuration database |
| **SQLite (local)** | Dashboard history — records of every pipeline run and publication |

### Infrastructure
| Service | Purpose |
|---|---|
| **Google Cloud Platform (GCP)** | Streaming media upload before Gettr publication |
| **Google Cloud VM** | The server where the bot process runs |

### Notion Databases
| Database | Purpose |
|---|---|
| **RSS Source DB** | Master list of 109 RSS feeds for DailyNews; bot reads this every 10 minutes |
| **DailyNews Review DB** | One card per candidate article awaiting editor decision (DailyNews) |
| **EpicFury Review DB** | Same structure, separate database for EpicFury articles |
| **daily_news published DB** | Human-published posts (pre-bot); used for topical duplicate detection |

---

## 3. DailyNews Pipeline

### 3.1 Sources

The bot reads its RSS feed list from the **Notion RSS Source Database** (not a static config file). Each active feed (`in_use = true`) is fetched every run. There are approximately 109 active feeds including:

- Reuters, BBC, CNBC, Al Arabiya, Breitbart, ABC News, Axios, VOA, Radio Free Asia, South China Morning Post, Taiwan News, Hong Kong Free Press, and many others.

This allows editors to add or remove sources from Notion without restarting the bot.

### 3.2 Ingestion — Step by Step

```
Every 10 minutes:

[1] Fetch RSS feeds
    → feedparser reads each active feed from Notion RSS DB
    → Articles published within the last 2 hours are collected
    → Articles outside the 2-hour window are discarded

[2] URL Dedup (Redis cache check)
    → For each article URL, compute a 16-character SHA256 hash
    → Check Redis: if hash exists → article already processed → SKIP
    → If new → atomically write hash to Redis with 3-hour expiry (TTL 10,800s)
    → Only new articles proceed

[3] Claude Scoring
    → Send batch of new articles to Claude (Haiku 4.5)
    → Claude acts as a "senior geopolitical editor" with a 0–10 scale
    → SCORING CRITERIA:
        - CCP/China linkage is required to score above 4.9
        - Track A (score 5–10): Direct CCP actions — propaganda, Taiwan pressure,
          espionage, economic coercion, PLA movements
        - Track B (score 5–8): Indirect global developments — U.S. policy,
          Taiwan defense posture, allied responses, trade war
        - Score ≤ 4.9: Not sufficiently CCP-linked → DROPPED
    → Articles below threshold 6.0 are dropped

[3b] Editor Review — OPTIONAL, DailyNews only, OFF by default
    → Only runs when "editor_review" is switched on in the Config tab. Used to
      trial a different editorial voice side by side with the current one.
    → Each qualifying article is passed through three editor prompts in sequence:
        1. Intake triage    — does the story have a named actor, a hard number and
                              a self-indictment angle? Produces an editor's brief
                              and answers QUALIFIES: yes / no
        2. CCP exposure     — drafts from that brief, with the source article
                              attached underneath for facts and quotes
        3. ChinaX style     — apply the house voice
    → The result is a FINISHED ALTERNATIVE POST, kept alongside the normal one.
      It is published exactly as the editor prompts wrote it — it is NOT put
      through the normal post writer afterwards, which would strip the voice.
    → If triage says the story does not qualify, or anything goes wrong, the article
      simply gets no revised version. IT IS NEVER DROPPED because of this step —
      the normal post is produced exactly as it would have been.

[4] Post Generation
    → Claude writes a 55–75 word social media post for each qualifying article
    → Source filtering, before any AI rewrite:
        - English source article under 40 words → article DROPPED
          (too thin to write a meaningful post from)
        - Non-English articles are never length-filtered — Chinese, Japanese and
          Arabic have no spaces between words, so a word count is meaningless
          for them (a 500-character Chinese article "counts" as 1 word)
    → Rules:
        - EVERY article is rewritten by the AI — regardless of its language or
          length. There is no longer a "short English text passes through
          unchanged" shortcut.
        - Non-English content → translated and rewritten in English
        - Target 55–75 words, two paragraphs. If the AI comes back outside that
          range it is asked once to rewrite; whatever it returns the second time
          is accepted as-is.
        - Never trim existing output; never duplicate sentences
        - No hashtags, no sycophantic language
    → If the AI refuses to write a post ("I cannot generate…", "does not provide
      relevant information", etc.) the article is dropped rather than posting the
      refusal text
    → This step writes the NORMAL post only. If step [3b] produced an alternative
      post, it is left untouched here — see section 3.5.

[5] Gemma Verification (DailyNews only)
    → The generated post is sent to Gemma 4 (31B model via Google AI Studio)
    → Gemma checks against 7 criteria:
        1. Does not wrongly apply CCP/China criticism to ordinary Chinese people
        2. Maintains appropriate editorial posture
        3. Factual accuracy (no unverifiable claims)
        4. Meets OSINT/intelligence quality standards
        5. Accessible to general audience
        6. Appropriate tone (not inflammatory, not apologetic)
        7. Strategic relevance to U.S.-China competition
    → Verdict:
        - PASS → article continues
        - REVISE → article DROPPED
        - FAIL → article DROPPED
        - ERROR (model failure) → treated as PASS (fail-open, no blocking)

[6] Semantic Dedup — Within Batch
    → OpenAI generates vector embeddings for all surviving articles
    → Articles in the current batch are compared against each other
    → If cosine similarity ≥ 0.70 → keep first, drop later duplicates
    → Prevents publishing two very similar articles in the same run

[7] Semantic Dedup — Cross Batch (Qdrant)
    → Each article is compared against all articles published in the last 48 hours
      (stored in Qdrant Cloud vector database)
    → If similarity ≥ 0.80 → DROPPED as duplicate of a recent article
    → Survivors are added to Qdrant for future comparisons

[8] Enqueue for Review
    → Each surviving article is sent to NotionReviewAgent
    → A card is created in the DailyNews Notion Review Database
    → Card fields: Title, Score, Generated Post, Source URL, Media URLs
    → Auto-pilot ON: Decision = "Queued", article pushed directly to publish queue
    → Auto-pilot OFF: Decision = "Pending", article awaits editor review
    → Article data is also stored in Redis (TTL 24 hours) for fast access during publication
```

### 3.3 Publication — Step by Step

```
Every hour (at configured minute):

[1] Check publish queue
    → Redis list "publish_test:queue" is checked
    → If empty → run fallback (see 3.4)

[2] Select article to publish
    → Articles sorted by AI score (highest score first)
    → Bot attempts to publish the top-scoring article

    The next three checks all run BEFORE any image work, so a doomed article is
    discarded without spending an upload.

[3] Minimum length check (DailyNews only)
    → If the post is under 55 words → article DROPPED
    → Notion card updated to "Discarded"
    → This is the final safety net against one-sentence posts. It catches both a
      too-short AI result and the emergency "use the headline as the post" path,
      which skips post generation entirely.
    → EpicFury is deliberately exempt (it may post short items and link previews)

[4] Article-ID dedup check
    → Redis key "{prefix}article:{url_hash}" checked
    → If exists → this URL was already successfully published → SKIP
    → Notion card updated to "Discarded"
    → Catches re-ingested articles even when the AI regenerates different post text

[5] Post dedup check (SHA1)
    → 12-character SHA1 hash of post text + first media URL is checked in Redis
    → If hash exists → article already posted (within 10 days) → SKIP
    → Notion card updated to "Discarded"

[6] Media handling
    → Bot fetches the image/video attached to the article
    → Rejects images smaller than 120,000 px² in area or 120 px on the short side
    → Rejects images that are just the publisher's logo (see below)
    → Streams media to Google Cloud Platform (GCP) for upload

[6b] No usable image? Make a video instead (optional, off by default)
    → Only if the "AI Video" switch is ON and the daily quota is not yet spent
    → The bot writes a ~25-second news video from the post text: real photographs
      from Wikimedia with slow zoom, a spoken voiceover, subtitles, background
      music, the DailyNews logo bar and the animated end card
    → That video is posted in place of the missing picture
    → If the switch is off, the quota is spent, or anything goes wrong, the bot
      does exactly what it always did: article DROPPED, Notion card "Discarded"
    → See 3.6 for the full explanation

[7] Publish to Gettr
    → Post text + GCP-hosted media sent to Gettr API
    → On success: both dedup keys written to Redis with 10-day expiry (864,000s)
    → On success: Notion card updated to "Published" with timestamp
    → Stop — only 1 successful post per hourly run

[8] On failure
    → Article requeued? No — DailyNews drops and moves to next in queue
```

### 3.4 Fallback (Empty Queue)

If the publish queue is empty when the hourly run triggers:

1. Bot queries Notion for articles with Decision = Approved or Queued, created in the last 6 hours
2. Articles are sorted by score (highest first)
3. Bot reconstructs these articles from Notion data and attempts to publish

### 3.5 Editor Review A/B Test (optional, off by default)

**What it is for.** We want to try a different editorial voice on DailyNews without gambling the
live channel on it. So we run both at once and compare the results.

**How it works.** When the feature is switched on, every story the bot publishes goes out **twice**:

| | Account | Text |
|---|---|---|
| Standard | the normal DailyNews Gettr account | written the way it always has been |
| Editor version | a separate test Gettr account | written by the three editor prompts, published word for word as they wrote it |

Both posts use the **same story and the same image, published seconds apart**, so the only thing
that differs between the two accounts is the editorial voice. You read them side by side and decide.

**What it cannot do.**

- It cannot stop a story reaching the live account. If the editor chain rejects a story or errors
  out, the story still publishes normally — it just has no counterpart on the test account.
- It cannot post a story the live account did not post. The test post is triggered *by* a
  successful live post, so the two feeds can never drift apart.
- It cannot post the same story twice. It rides on a live post that has already passed every
  duplicate check.
- If the test account is broken or misconfigured, the live post still succeeds. The failure is
  written to the log and nothing else happens.

**Human review.** Unchanged — one Notion card per story, showing the standard post, exactly as
today. The editor version is not reviewed in Notion; it exists to be compared on Gettr.

**Turning it on.** Two switches, both in the dashboard **Config** tab:
`editor_review.enabled: true`, and a `gettr_test` block with the test account's credentials.
The three editor prompts are edited in the **Prompts** tab like any other prompt.
Adding the test account for the first time needs a service restart; everything else takes effect
immediately.

### 3.6 AI Video for Image-less Stories (optional, off by default)

**The problem it solves.** DailyNews has always refused to post without a picture. Every
day a number of perfectly good stories are thrown away for no reason other than the source
site not offering a usable photograph. This feature gives those stories a second route to
publication.

**What gets made.** A roughly 25-second news video, 960×720, built entirely from the post
the bot already wrote:

- **Pictures** — real photographs pulled from Wikimedia Commons, chosen from four or five
  subjects the AI picks out of the story (the people, buildings, cities and institutions it
  mentions). Each gets a slow zoom. If a subject has no suitable photograph, a plain
  branded caption card fills the gap.
- **Voice** — a synthetic newsreader voice reading the post text word for word. Nothing is
  added; the narration *is* the post.
- **Subtitles** — burned in, so the video reads with the sound off.
- **Branding** — the red DailyNews bar across the bottom with the headline and logo,
  background music that ducks under the voice, and the animated logo + FOLLOW end card.
- **Credits** — photographer credits are burned into the top corner automatically.

**When it triggers.** Only where the article would otherwise have been thrown away:

| Situation | Before | Now |
|---|---|---|
| No image anywhere | Dropped | Video |
| Image too small (a thumbnail or icon) | Dropped | Video |
| Image is just the outlet's logo | Posted the logo | Video (falls back to posting the logo if no video is made) |
| Every image failed to upload | Dropped | Video |

A story that *does* have a good photograph is completely unaffected — it publishes as an
ordinary image post, exactly as before.

**The two dashboard controls.**

- **🎬 AI Video** — the on/off switch, next to Auto-Pilot. It shows how much of today's
  allowance is used, e.g. `🎬 AI Video 3/6`.
- **AI Videos per 24h** — a slider in Settings. This is a rolling 24-hour allowance, not a
  midnight reset: a video made at 3pm stops counting at 3pm the next day. **Setting it to 0
  disables the feature even when the switch is on** — the switch and the allowance both
  have to be set.

The allowance exists because each video takes about a minute and a half of heavy work on
the server, and because you generally don't want every image-less story becoming a video.
Each pipeline has its own separate allowance.

**Costs.** Roughly a minute and a half of server CPU per video, plus a fraction of a cent
for the AI call that picks the headline and the photo subjects. The voice and the
photographs are free.

**What happens when something goes wrong.** Nothing bad. If the switch is off, the
allowance is spent, the voice service is down, or the video fails for any reason at all,
the bot falls back to precisely what it did before — DailyNews drops the article and marks
the Notion card "Discarded". A broken video generator can never cost you a post you would
otherwise have had.

**Known limits.**

- Only openly licensed Wikimedia photographs are used, never the source article's own
  images. That keeps the licensing clean with no human review needed, but Wikimedia's
  choice for a given subject is sometimes literal or dated — an emblem rather than a news
  photo, or a map with foreign-language labels. Everything shown is on-topic, but it will
  not look like agency wire photography.
- Stories about wholly abstract subjects (market sentiment, legal doctrine) yield fewer
  photographs and more caption cards.
- The voice service is a free unofficial one. If it stops working, videos quietly stop
  being made and image-less stories go back to being dropped.

---

## 4. EpicFury Pipeline

### 4.1 Sources

EpicFury monitors two types of sources:

**X/Twitter Accounts (11 accounts):**
- @OSINTdefender, @BabakTaghvaee1, @GeoConfirmed, @CENTCOM, @IranIntl_En, @IDF, @ELINTNews, @ImageSatIntl, @Bellingcat, @Nrg8000, @Shayan86

**News Websites (17 sites):**
- Defense One, Breaking Defense, Politico, War on the Rocks, Task and Purpose, FDD, Al Arabiya, Middle East Eye, Times of Israel, Haaretz, Jerusalem Post, Rudaw, Reuters, AP, BBC Middle East, MEI, Axios

Content is filtered by keywords related to Iran, military operations, and regional conflicts before scoring.

### 4.2 Ingestion — Step by Step

The ingestion stages are identical to DailyNews with these differences:

| Stage | DailyNews | EpicFury |
|---|---|---|
| Sources | RSS feeds from Notion DB | X accounts + websites (static config) |
| Score focus | CCP/China geopolitics | Iran/military/Middle East |
| Score threshold | 6.0 | 6.0 |
| Gemma verification | YES | NO (skipped) |
| Redis key prefix | `newsrooms:dailynews_v2_test:...` | `epicfury:...` |
| Notion Review DB | DailyNews Review DB | EpicFury Review DB |

### 4.3 Publication — Step by Step

```
Every hour:

[1] Check EpicFury publish queue (epicfury:publish:queue)

[2] Process ALL articles in queue (FIFO order, unlike DailyNews best-score-first)

[3] For each article:
    a. Post dedup check (SHA1) — same logic as DailyNews
    b. Attempt media upload to GCP
       → If media upload fails: FALLBACK to OG preview (no drop)
    c. Publish to Gettr (EpicFury account)
       → Success: Notion card updated to "Published" (note: no callback — Notion
         update is best-effort, not guaranteed for EpicFury)
       → Failure: Article requeued (up to 3 total attempts)

[4] EpicFury publishes all queued articles per run (not stop-after-1)
```

### Key Difference: Image-less Posts
- **DailyNews:** No image = no post. Article is dropped — unless the AI Video switch is on and quota remains, in which case a generated video takes the picture's place (see 3.6).
- **EpicFury:** No image = post published anyway using the article's OG preview (Open Graph metadata from the source page). Text-only posts are acceptable. The AI Video switch exists on the EpicFury dashboard too but is off by default; the feature is being proven on DailyNews first.

---

## 5. Human Review Workflow

Both pipelines use Notion as the human review interface. Editors review article cards and set a "Decision" field to control what the bot does.

### 5.1 Decision State Machine

```
Article enqueued
      │
      ├─ Auto-pilot ON  ──────────────────────────────────────────────────┐
      │                                                                    │
      │                                                                    ▼
      │                                                               [Queued]  ← Created directly as Queued;
      │                                                                    │        already in publish queue
      │                                                                    ▼
      │                                                             [Published]  ← Gettr post confirmed
      │
      └─ Auto-pilot OFF
            │
            ▼
        [Pending]  ← Default state; awaiting editor decision
            │
            ├─ Editor sets → [Approved]
            │                     │
            │                     │   (no dedup check here any more — topical dedup
            │                     │    moved to publication time, where both auto-pilot
            │                     │    and manual mode actually pass through)
            │                     │
            │                     └─ article pushed to publish queue
            │                                            │
            │                                            ▼
            │                                        [Queued]  ← In publish queue or being published
            │                                            │
            │                                            ▼
            │                                      [Published]  ← Gettr post confirmed
            │
            ├─ Editor sets → [Rejected]
            │                     └─ Redis data deleted → Notion set to [Discarded]
            │
            ├─ Editor sets → [Publish Now]
            │                     └─ Bot publishes immediately on next poll → [Queued]
            │
            └─ Bot drops silently → [Discarded]
                  (reasons: post under 55 words, article-ID dedup hit,
                   SHA1 post dedup hit, no image, image too small)
```

Gemma FAIL/REVISE and both ingestion-time similarity dedups happen *before* a card is ever created,
so those articles never appear in Notion at all. A topical duplicate becomes [Rejected], not
[Discarded].

### 5.2 Poll Interval
The bot checks Notion every 30 seconds for decision changes. When it detects a change, it acts immediately.

### 5.3 Redis + Notion Relationship
Each article has two storage locations:
- **Redis hash** (fast, TTL 24h): stores post text, score, media URLs, Notion page ID
- **Notion card** (persistent, visible to editors): shows full article with decision controls

If the Redis hash expires (after 24 hours) but the Notion card still exists, the bot reconstructs the data from Notion before publishing.

---

## 6. Deduplication System

**Updated — see below.** Both pipelines run full auto-pilot with no human review step,
which means the old Layer 6 (a Claude Haiku pairwise comparison) never actually ran: it
only fired on the manual "Approved" Notion transition, a path auto-pilot skips entirely by
creating cards directly as "Queued". This went unnoticed because Layers 1–5 all compare the
bot against *itself* — none of them can see the human-editor "daily_news" Notion board,
where editors independently queue and publish (via a separate, still-running hourly n8n
workflow) stories that this bot's RSS pipeline may also pick up on its own.

The bot uses six independent deduplication layers to prevent publishing duplicate content:

| Layer | When | Method | Threshold | Scope |
|---|---|---|---|---|
| **1. URL dedup** | Ingestion | SHA256 hash of URL (atomic SET NX EX) | Exact match | 3-hour TTL per pipeline |
| **2. Within-batch cosine** | Ingestion | Vector similarity | 0.70 | Same run only |
| **3. Cross-batch Qdrant** | Ingestion | Vector similarity | 0.80 | Last 48 hours |
| **4. Article-ID dedup** | Publication | Redis key keyed to URL hash | Exact match | 10-day TTL per pipeline |
| **5. Post hash (write-only)** | Publication | SHA1 hash of post text + image URL | Exact match | 10-day TTL per pipeline |
| **6. Topical embedding dedup** | Publication (before + after) | OpenAI embedding cosine similarity vs. the `daily_news` Notion board | 0.80 | 24h (before-publish) / unbounded not-yet-sent (after-publish) |

Layer 5 no longer gates this bot's own publish decision (exact 500-char text matching missed
same-story-different-wording duplicates). It's kept as a **write only** — the legacy n8n
"waiting for post" hourly workflow checks this same Redis key/namespace before it publishes,
so removing the write would silently break that workflow's only cross-system dedup signal.

Layer 6 is implemented in `services/notion_topical_dedup.py` and now runs at two points
inside `PublishAgent._process_one`/`_mark_posted` (`agents/publish_agent.py`) — the one path
that executes regardless of auto-pilot state, unlike the old Approved-transition hook:

- **Before publishing** (`before_publish`): compares the candidate against `daily_news` cards
  already published in the last `recent_lookback_hours` (default 24h, `send_status=True`,
  filtered on the built-in **Last edited time** — not `TimerForPub`, which no editor actually
  uses). A match means a human editor already published the same story. Whether a match
  actually skips publishing is gated by `notion_dedup.enforce_recent_skip` (default **false**
  — shadow mode: logs what it would have skipped, doesn't skip yet, until the threshold is
  validated against live traffic).
- **After publishing** (`after_publish`): compares the just-published post against
  not-yet-sent `daily_news` cards (`send_status=False`, `status` in `2nd_eye` /
  `waiting for post`). A match marks that card's `Duplicate` select and appends the new
  Gettr post's link to `Notes`, so the editor sees the bot already covered it before the
  next hourly n8n run would otherwise publish it again. **This does not yet stop that n8n
  run** — its own Notion query filter has no `Duplicate`-exclusion condition, so marking
  Duplicate is informational only until that filter is updated separately.

A third, independent loop (`NotionTopicalDedupChecker.run_gettr_crosscheck_loop`, every
`gettr_crosscheck_interval_minutes` — default 15) periodically compares not-yet-sent
`daily_news` cards against this pipeline's own recent Gettr posts directly (via the
undocumented `GET /u/user/{handle}/posts` endpoint, `services/gettr_feed_client.py`) — this
catches the case where the bot published a story that never had a live Notion candidate to
diff against at either publish-time hook above.

Layers 3 and 6 depend on external services (Qdrant/OpenAI and Notion respectively). If those
services are unavailable, those layers fail open (article passes through) and the remaining
layers still apply.

### How They Work Together

```
New article URL arrives
  → Layer 1 (URL hash): "Have we seen this exact URL in the last 3 hours?"
    YES → skip (fast, cheap)
    NO  → continue to scoring

Article scores above threshold, post generated
  → Layer 2 (within-batch): "Is this article similar to another article in THIS run?"
    YES (≥70% similar) → drop the later one
    NO  → continue

  → Layer 3 (cross-batch Qdrant): "Is this article similar to anything published
    in the last 48 hours?"
    YES (≥80% similar) → drop
    NO  → add to Qdrant, enqueue for review

Auto-queued (auto-pilot mode — the only mode currently in use)
  → pushed straight to publish queue (no Approved-transition hook to wait for)

At publication time:
  → Layer 4 (article-ID): "Has this article URL been successfully published before?"
    YES → skip
    NO  → continue

  → Layer 6a (topical embedding, before-publish): "Has a human editor already published
    the same story (daily_news, send_status=True) in the last 24h?"
    YES → skip if enforce_recent_skip else log-only
    NO  → continue

  → Layer 5 (post hash, write-only — no longer a gate): compute the hash for the n8n
    workflow's benefit

  → publish to Gettr

  → Layer 6b (topical embedding, after-publish): "Does a not-yet-sent daily_news card
    (2nd_eye / waiting for post) match what was just published?"
    YES → mark that card's Duplicate + append the Gettr link to Notes
    NO  → done
```

---

## 7. Scheduling & Auto-Pilot

### Schedule

Intervals are set from the dashboard and can be changed at any time, so treat these as a snapshot
rather than a fixed configuration. As of 2026-07-25:

| Task | Frequency | Configurable |
|---|---|---|
| DailyNews ingestion | Every 30 minutes | Yes (dashboard) |
| EpicFury ingestion | Every 15 minutes | Yes (dashboard) |
| DailyNews publication | Every 30 minutes | Yes (dashboard) |
| EpicFury publication | Every 15 minutes | Yes (dashboard) |
| Notion review poll | Every 30 seconds | No |

### Auto-Pilot Mode
When **auto-pilot is enabled** on the dashboard:
- DailyNews: Articles with score ≥ threshold are pushed directly to the publish queue; Notion cards are created with Decision = "Queued" for record-keeping (no human review step)
- EpicFury: Same behavior

When **auto-pilot is disabled**:
- Every article waits for an editor to set Decision = Approved (or Rejected/Publish Now)

**Both pipelines are currently running with auto-pilot ON**, so articles are being published without
a human review step. This is a dashboard setting, not a code default — check the dashboard for the
current state.

### Pause Mode
Each pipeline can be **paused independently** from the dashboard:
- Paused: ingestion still runs, but publication is stopped
- Useful during breaking news situations requiring manual oversight

---

## 8. Admin Dashboard

The bot includes a web-based admin dashboard running at `http://<server>:8080/`.

### Features

| Section | What it shows/controls |
|---|---|
| **Pipeline Graph** | Live view of the current run: which stage is active, article counts at each stage |
| **Queue Inspector** | Current publish queue contents (article title, score, media) for both pipelines |
| **Run History** | Log of every pipeline run: articles fetched, scored, passed, published |
| **Config Editor** | Edit `config.yaml` settings live (requires restart for most changes) |
| **Prompt Editor** | Edit scoring/post-generation/verification prompts (reloads without restart) |
| **Health Check** | Redis, Qdrant, Notion, and Gettr connectivity status |
| **Live Log** | Real-time log stream from the running process |
| **Threshold Sliders** | Adjust score threshold and dedup thresholds without restarting |
| **Auto-Pilot Toggle** | Enable/disable auto-approval per pipeline |
| **Manual Trigger** | Trigger an immediate RSS ingestion or publish run |

### Access
- Password-protected (bcrypt hash stored in config.yaml)
- Accessible only from authorized IP addresses (GCP firewall)

---

## 9. Data Flow Summary

### DailyNews End-to-End

```
Notion RSS DB (109 feeds)
    ↓ every 10 min
feedparser fetches articles (last 2h only)
    ↓
Redis URL hash check → skip seen articles
    ↓
Claude Haiku scores articles (0–10, CCP focus)
    → Drop if score < 6.0
    ↓
English source under 40 words? → drop
    ↓
[optional] Editor Review — triage → CCP exposure → ChinaX style
    → finished alternative post kept alongside the normal one; never drops anything
    ↓
Claude Haiku generates 55–75 word post
    → Refusal or still no usable text? → drop
    ↓
Gemma 4 verifies post (7 editorial criteria)
    → Drop if FAIL or REVISE
    ↓
OpenAI generates vector embeddings
    ↓
Within-batch cosine dedup (≥0.70 → drop)
    ↓
Qdrant cross-batch dedup (≥0.80, last 48h → drop)
    ↓
Notion card created + Redis hash stored (24h TTL)
    ↓
Auto-pilot ON  → Push to Redis publish queue → Notion card created as Queued (no review)
Auto-pilot OFF → Notion: Pending → Editor reviews
    → Approved → Topical LLM dedup check (last 24h)
                  → Duplicate? → duplicated=True + Rejected
                  → Unique? → Push to Redis publish queue → Notion: Queued
    → Rejected → Redis deleted → Notion: Discarded
    → Publish Now → immediate publish attempt
    ↓ (every hour)
PublishAgent reads queue (highest score first)
    ↓
Post under 55 words? → Notion: Discarded → try next article
    ↓
Redis article-ID check
    → URL already published? → Notion: Discarded → try next article
    ↓
Redis SHA1 post hash check
    → Already posted? → Notion: Discarded → try next article
    ↓
GCP media upload (image/video)
    → All fail / image too small? → Notion: Discarded → try next article
    ↓
Gettr API: publish post with media
    → Success → [optional] same image + editor version posted to the TEST Gettr account
                 (best effort — a failure here changes nothing above)
             → both dedup keys written (10d) → Notion: Published ✓ — STOP for this hour
```

### EpicFury End-to-End

```
X accounts (11) + Websites (17)
    ↓ every 10 min
Keyword-filtered content fetched
    ↓
Redis URL hash check → skip seen articles
    ↓
Claude Haiku scores articles (0–10, Iran/military focus)
    → Drop if score < 6.0
    ↓
Claude Haiku generates 55–75 word post
    ↓
[No Gemma verification]
    ↓
OpenAI embeddings → within-batch dedup → cross-batch Qdrant dedup
    ↓
Notion card created — Pending (manual) or Queued (auto-pilot)
    ↓
Editor approves (no topical LLM dedup for EpicFury)
    ↓
Redis publish queue (FIFO order)
    ↓ (every hour)
PublishAgent reads ALL queued articles
    ↓
[No 55-word floor — EpicFury is exempt]
    ↓
Redis article-ID check → skip if this URL was already published
    ↓
Redis SHA1 post hash check → skip if already posted
    ↓
GCP media upload
    → Fails? → Use OG preview instead (no drop)
    ↓
Gettr API: publish
    → Fail? → Requeue (max 3 attempts total)
    → Success → continue to next article in queue
```

---

## Appendix: Key Numbers

Values marked **(live)** are set from the dashboard and change without a code or config edit — the
figures shown are as of 2026-07-25. Everything else is fixed.

| Parameter | Value |
|---|---|
| RSS fetch window | Last 2 hours |
| Score threshold — DailyNews | **(live)** 6.5 / 10.0 |
| Score threshold — EpicFury | **(live)** 6.0 / 10.0 |
| Ingestion interval | **(live)** 30 min DailyNews, 15 min EpicFury |
| Publish interval | **(live)** 30 min DailyNews, 15 min EpicFury |
| Auto-pilot | **(live)** ON for both pipelines |
| Post length target | 55–75 words |
| Post length hard maximum | 75 words |
| Minimum source-article length | 40 words (English articles only) |
| Minimum publishable post length | 55 words (DailyNews only) |
| URL dedup cache | 3 hours (10,800s) |
| Post dedup cache | 10 days (864,000s) — both the SHA1 and article-ID keys |
| Minimum image size | 120,000 px² area and 120 px short side |
| Redis article data TTL | 24 hours |
| Within-batch similarity threshold | **(live)** 0.70 DailyNews, 0.65 EpicFury |
| Cross-batch similarity threshold | **(live)** 0.70 DailyNews, 0.65 EpicFury |
| Cross-batch window | 48 hours |
| Topical dedup window | 24 hours |
| EpicFury max retry attempts | 3 |
| Notion poll interval | 30 seconds |
