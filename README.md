# dailynews-agent

An async Python agent that ingests news, scores and rewrites it with LLMs, routes it through human
review in Notion, and publishes to [Gettr](https://gettr.com). It replaces a set of 8 n8n workflows
with a single supervised process.

Two content pipelines run concurrently in one process, each with its own sources, prompts, review
board, Gettr account, and web dashboard:

| Pipeline | Beat | Sources | Gettr | Dashboard |
|---|---|---|---|---|
| **DailyNews** | China / CCP-focused world news | RSS feeds (feed list lives in a Notion DB) | `@dailynews` | `:8080` |
| **EpicFury** | Military / Middle East breaking news | X (Twitter) accounts + news websites | `@epicfury` | `:8081` |

Additional channels can be added from `config.yaml` alone, with no code changes — see
[Adding a channel](#adding-a-channel).

---

## How it works

```
                    ┌──── INGESTION (interval set live; 15–30 min) ────┐

  RSS feeds / X accounts / websites
        ↓  fetch (last 2h only)
  URL dedup           atomic Redis SET NX EX, 3h TTL
        ↓
  Claude Haiku scoring        0–10, drop below the live threshold (~6.0–6.5)
        ↓
  Post generation             55–75 words, English only, every article rewritten
        ↓                     English source under 40 words → dropped
  Gemma verification          DailyNews only — FAIL/REVISE → dropped
        ↓
  Semantic dedup              within-batch cosine → cross-batch Qdrant, 48h window
                              (both thresholds set live, ~0.65–0.70)
        ↓
  Notion review card          Pending (manual) or Queued (auto-pilot)

                    └──────────────────────────────────────────────────┘

                    ┌──── PUBLISHING (interval set live; 15–30 min) ────┐

  Editor sets Decision in Notion
        ↓  Approved → topical LLM dedup (DailyNews, 24h window)
  Redis publish queue
        ↓  DailyNews: best score first, stop after 1 success
        ↓  EpicFury:  FIFO, try all, requeue failures up to 3×
  Pre-publish gates           55-word floor (DN only) → article-ID dedup → SHA1 dedup
        ↓
  Media upload to GCP CDN     DailyNews: no image = no post
        ↓                     EpicFury: falls back to an OG link preview
  POST to Gettr → Notion card marked Published

                    └─────────────────────────────────────────────┘
```

Six independent deduplication layers guard against posting the same story twice: URL hash,
within-batch cosine, cross-batch Qdrant, article-ID, SHA1 post hash (now a write-only signal for
the legacy n8n workflow), and a publish-time topical embedding check against the human editors'
`daily_news` Notion board. `docs/bot-workflows.md` explains how they interact.

### Stack

| Concern | Choice |
|---|---|
| Concurrency | plain `asyncio` loops — no scheduler framework, to keep RAM low |
| Message bus | Redis (Upstash) — agents pass article IDs, never full objects |
| Scoring / rewriting | Claude Haiku 4.5 + OpenAI `gpt-4o-mini` |
| Content verification | Google Gemma 4 31B (DailyNews only) |
| Embeddings / vectors | OpenAI `text-embedding-3-small` → Qdrant Cloud |
| Human review | Notion databases (Telegram review is retired but still in the tree) |
| Media hosting | Gettr's GCS resumable upload flow, fully streaming |
| Dashboard | aiohttp + server-sent events + an Alpine.js single-page app |
| History | SQLite (`data/history.db`) |

---

## Requirements

- Python 3.10+ (running 3.10.12 on Ubuntu)
- Redis (Upstash or any TLS-capable Redis)
- Accounts / API keys: Notion, OpenAI, Anthropic, Qdrant Cloud, Google AI Studio (Gemma), Gettr

## Install

```bash
git clone <repo> && cd dailynews-agent
pip3 install -r requirements.txt
```

## Configure

Credentials, Redis key prefixes, dashboard ports and prompt paths live in `config.yaml`. There is no
`.env`. Start from the template:

```bash
cp config.example.yaml config.yaml    # then fill in every REPLACE_ME
```

`config.yaml` holds live secrets and is gitignored — never commit it.

**Three layers of precedence, lowest to highest:**

1. `core/config.py` — pydantic defaults, used only for keys absent from `config.yaml`
2. `config.yaml` — credentials and structural settings
3. `data/schedule.json` / `data/schedule_ef.json` — **wins for thresholds, intervals, autopilot,
   pause and the verify toggle**

Layer 3 is the one that surprises people. It's written on first startup and updated whenever you
change a slider in the dashboard, and from then on the corresponding `config.yaml` values are
inert. So the score threshold, dedup thresholds, ingest/publish intervals and autopilot state in
`config.yaml` are startup fallbacks, not what's running. **To see live values, read those JSON files
or the dashboard.** Editing them by hand requires a restart to take effect.

Minimum to fill in:

| Key | What |
|---|---|
| `notion.api_key`, `notion.rss_database_id` | RSS source database (DailyNews feed list) |
| `notion_review.review_database_id` | Article review board (DailyNews) |
| `notion_review_epicfury.review_database_id` | Article review board (EpicFury) |
| `redis.url` | Redis connection string |
| `openai.api_key` | Embeddings + primary post generation |
| `claude.api_key` | Scoring + topical dedup |
| `qdrant.url`, `qdrant.api_key` | Vector store |
| `gemma.api_key` | Google AI Studio key for content verification |
| `gettr.user_id`, `gettr.user_token` | Gettr credentials (and `gettr_epicfury.*`) |
| `dashboard.password_hash` | See below |

Then:

```bash
# 1. Create the Notion review board properties (once per review database)
python3 setup_notion_review_db.py
python3 setup_notion_review_db.py --section notion_review_epicfury

# 2. Set the dashboard password, paste the hash into config.yaml
python3 -m dashboard.setup_password

# 3. Open the dashboard ports to your IP only (GCP Console → VPC → firewall): TCP 8080, 8081
```

## Run

The process is managed by **systemd** (`dailynews-agent.service`, `Restart=always`, enabled at
boot). It serves both dashboards and runs every loop in one process.

```bash
systemctl status dailynews-agent          # health
sudo systemctl restart dailynews-agent    # required after ANY .py edit
journalctl -u dailynews-agent -f          # live logs
```

⚠️ **Do not start a manual `nohup python3 main.py`.** It collides on port 8080 with the managed
process, and killing the managed process just makes systemd relaunch it 10 seconds later.

Healthy looks like `active (running)` plus:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login   # 200 or 302
```

### What needs a restart, and what doesn't

| Change | Action |
|---|---|
| Any `.py` file | `sudo systemctl restart dailynews-agent` |
| `config.yaml` credentials / settings | `sudo systemctl kill -s SIGUSR1 dailynews-agent` (does **not** reload code) |
| `prompts/*.txt` | Nothing — the dashboard's prompt editor calls `reload_prompts()` |
| Thresholds, intervals, auto-pilot, pause | Nothing — set them live in the dashboard |
| `data/schedule*.json` edited by hand | Restart (it is only read at startup) |

## Dashboard

`http://<host>:8080/` for DailyNews, `:8081` for EpicFury. Redirects to `/login`; session tokens are
HMAC-SHA256, with the password hash in `config.yaml`.

Live pipeline graph (SSE-driven), run history, queue inspector, config and prompt editors, health
checks, log tail. Per-pipeline controls for ingestion and publish intervals, score and similarity
thresholds, the Gemma verify toggle, auto-pilot, and pause.

**The dashboard is the control surface for all of those** — changes take effect immediately and are
persisted to `data/schedule.json` / `data/schedule_ef.json`, which override `config.yaml` on every
subsequent start (see [Configure](#configure)).

### Auto-pilot

Defaults to off when no schedule file exists, in which case every article waits for an editor to set
`Decision` in Notion. **Both pipelines currently run with it ON** — check the dashboard or the
schedule JSON rather than assuming.

When on, qualifying articles go straight to the publish queue and their Notion card is created as
**`Queued`** for the record. The card is deliberately *not* created as `Approved` — the poll loop
only looks at `Approved`/`Rejected`/`Publish Now`, so `Queued` is what keeps auto-pilot from
enqueueing every article twice.

## Tests

```bash
python3 -m pytest tests/ -v
```

Covers the pure utility functions — hashing, text cleaning, URL extraction. No network, no Redis, no
mocks.

---

## Adding a channel

Extra EF-style channels need **no code changes**. Add an entry to the top-level `channels:` list in
`config.yaml` with a unique `slug` and `dashboard_port`; `main.py` picks it up and
`core/channel_runtime.build_channel()` wires the whole pipeline. Redis prefixes and run-history
types are derived from the slug so channels can't collide, and prompt/source paths default to
`prompts/<slug>_*.txt` and `sources/<slug>_sources.md`.

Recipe and copy-paste templates: **`templates/channel/`** (`README.md` + `DEPLOY.md`).
Field reference: `DESIGN.md` §19.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every article in a batch scored `0.0` | Scoring JSON truncated. `claude.max_tokens` must cover a whole batch — keep it at 1500 with `batch_size: 5`. |
| The same article posted repeatedly | Check `url_hash_ttl_s >= rss.filter_feed_hours * 3600`, and that Qdrant + Anthropic are both reachable — several dedup layers fail open. |
| Nothing publishes, queue looks full | Check pause state and auto-pilot in the dashboard; check whether cards are stuck at `Pending`. |
| DailyNews drops lots of articles silently | Expected. No image, an image under 120,000 px² / 120 px, or a post under 55 words all mean a drop — there is no text-only DailyNews post. |
| Posts arriving as one short sentence | The 55-word publish floor should prevent this; check `_DN_MIN_WORDS` in `agents/publish_agent.py` and the generation retry in `services/claude_client.py`. |
| Process vanished, then came back | systemd restarted it. Check `journalctl` for an OOM kill — the box has 3.8 GiB RAM plus a 4 GB swapfile added for exactly this. |
| `pgrep`/`pkill -f "python.*main.py"` behaves oddly | The pattern matches the shell running it. Use systemd instead. |

Dashboard health tab and `GET /api/health` report Redis, Qdrant, and API status.

---

## Repository layout

```
main.py                  Entry point — asyncio.gather of every loop
config.yaml              All credentials and settings
setup_notion_review_db.py  One-time Notion review-board property creation

agents/     rss_agent, x_agent, website_agent, similarity_agent,
            notion_review_agent, publish_agent, source_reader
core/       config (pydantic + hot reload), models, pipeline,
            channel_runtime, redis_client
services/   claude_client, openai_client, gemma_client, qdrant_client,
            gcp_client, gettr_client, notion_client, notion_topical_dedup,
            metadata_client, pollinations_client
dashboard/  aiohttp app, SSE, auth, SQLite history, handlers/, templates/
utils/      hashing, text_cleaner
prompts/    Scoring / post-generation / verification prompts (live-reloadable)
sources/    EpicFury source lists (markdown)
templates/  Channel scaffolding
tests/      pytest — pure functions only
data/       history.db, main.log, schedule*.json  (runtime, not in git)
```

## Documentation

| File | Read it for |
|---|---|
| `README.md` | This file — setup, running, operating |
| `DESIGN.md` | Full system design: every stage, data model, endpoint, constant |
| `CLAUDE.md` | Invariants, gotchas, and DailyNews-vs-EpicFury differences (written for Claude Code, useful to humans) |
| `docs/bot-workflows.md` | Plain-language walkthrough of both pipelines, for non-engineers |
| `docs/gettr-posting.md` | Gettr API reference — auth, payload shapes, media upload |
| `docs/media_fetching_improvements.md` | Media / OG-image fetching design notes |
| `templates/channel/` | Adding a new channel |

When pipeline behavior changes, `CLAUDE.md`, `DESIGN.md`, and `docs/bot-workflows.md` all need
updating — they describe the same flow at three different altitudes.
