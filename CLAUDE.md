# dailynews-agent — Claude Code Reference

## RUNNING PROCESS

**Managed by systemd** — unit `dailynews-agent.service` (User=leon, `Restart=always`, enabled on boot). Do NOT launch a manual `nohup` instance — it collides on port 8080 with the systemd process, and killing the managed process just makes systemd relaunch it in 10s.

- Status / health: `systemctl status dailynews-agent` (up = `active (running)` + `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login` → 200/302)
- After any `.py` edit: `sudo systemctl restart dailynews-agent`
- Live logs: `journalctl -u dailynews-agent -f` (also still appended to `data/main.log`, which the dashboard tails)
- Hot-reload credentials only (SIGUSR1, does NOT reload Python code): `sudo systemctl kill -s SIGUSR1 dailynews-agent`
- Prompt files (`prompts/*.txt`): dashboard POST `/api/prompts/{name}` triggers `pipeline.reload_prompts()` — no restart needed

Notes: box has 3.8 GiB RAM + a 4 GB `/swapfile` (persisted in `/etc/fstab`, `vm.swappiness=10`) — added after an OOM killed the process. `pgrep`/`pkill -f "python.*main.py"` self-matches the shell running it (its cmdline contains the pattern) — prefer systemd over ad-hoc kills.

## THE TWO PIPELINES — KEY DIFFERENCES

| | DailyNews | EpicFury |
|---|---|---|
| Sources | RSS feeds (loaded from Notion DB) | X/Twitter + websites |
| Publish order | Best-score first, stop after 1 success | FIFO (reversed LPUSH), try all, requeue failures ×3 |
| No-image behavior | AI video if the switch is on and quota remains, else **drop** | AI video if switched on (default OFF), else OG preview post |
| Notion callbacks | `_on_posted`, `_on_dropped`, `_notion_fallback` all SET | **None of these 3 are ever set** |
| Score threshold | 6.0 | 6.0 (+ up to 1.0 video boost) |
| Gemma verification | Active | Not used |
| Topical dedup at publish | Yes — OpenAI embeddings vs `daily_news` only (see below) | No |
| 55-word publish floor | Enforced (drop) | Exempt |
| Hot Topics ranking | Yes | No |
| Editor review A/B branch | Optional (`editor_review.enabled`) | **Never** — skipped in socials mode |
| AI video fallback | Dashboard switch, default OFF | Same switch, default OFF (proving on DN first) |

The 3 late-binding callbacks are assigned in `main.py` lines 377–379 ONLY for the DailyNews `publish_agent`. EpicFury's `publish_ef` never gets them. All `pipeline_type != "epicfury"` branches in `publish_agent.py` are intentional — do NOT try to unify them.

## ADDING A CHANNEL (config-driven, no code edits)

Extra EF-style channels are added via the top-level `channels:` list in `config.yaml` — no
`config.py`/`main.py` edits. `main.py` loops over `config.channels` and calls
`core/channel_runtime.build_channel()`, which mirrors the EF init block: `pipeline_type="epicfury"`
behaviour, own Gettr/Notion/Qdrant/dashboard-port, Redis prefixes **derived from `slug`** (so no
collisions), and slug-based history run_types (`<slug>` / `<slug>_publish`). Prompt/source files
default to `prompts/<slug>_*.txt` and `sources/<slug>_sources.md`. DailyNews and EpicFury stay
hardcoded and untouched. Dashboard handlers take per-channel identity (`channel_title`,
`prompt_files`, `sources_path`, `run_types`) via `make_app`; when None they fall back to the legacy
`pipeline_type` behaviour. Full recipe + templates: `templates/channel/` (README + DEPLOY.md).

## RUNTIME VALUES — `config.yaml` IS NOT THE LIVE SOURCE OF TRUTH

`DashboardState.__init__` reads `data/schedule.json` (DN) / `data/schedule_ef.json` (EF) and those
values win. `main.py:403,417` then pushes them into the live agents:

```python
similarity_agent.set_thresholds(state.within_batch_threshold, state.cross_batch_threshold)
pipeline.set_filter_score_threshold(state.filter_score_threshold)
```

`__init__` also calls `save_schedule()` immediately, so the file is created on first ever startup.
**After that, the `config.yaml` values for these fields are dead** — they only serve as the
fallback used when a key is missing from the JSON.

| Field | Fallback when absent from JSON |
|---|---|
| `rss_interval_s` | **600 hardcoded** — not from config.yaml |
| `publish_interval_s` | **3600 hardcoded** — not from config.yaml |
| `rss_paused` / `publish_paused` | `False` |
| `filter_score_threshold` | `config.claude` / `config.epicfury` |
| `within_batch_threshold` / `cross_batch_threshold` | `config.qdrant*` |
| `notion_dedup_threshold` | `config.qdrant_notion` — **dead knob**, only sets `sim._notion_dedup_threshold`, which nothing reads since Stage-3 dedup was removed |
| `autopilot` | `False` |
| `verify_enabled` | `True` — gates the Gemma step at `core/pipeline.py:410`, so Gemma can be OFF live even with `gemma.enabled: true` in config |
| `x_scraper` | `"twitterapi"` |
| `video_gen_enabled` | `config.video_gen.enabled` (DN) / `config.epicfury.video_gen.enabled` (EF + channels) |
| `video_gen_max_24h` | same, `.max_24h` — **0 means off regardless of the switch** |

**To read the live values: `cat data/schedule.json data/schedule_ef.json`** (or the dashboard), never
`config.yaml`. They are tunable from the dashboard at any time, so treat any number written down
here as a snapshot.

As of 2026-07-25:

| | DailyNews | EpicFury | config.yaml says |
|---|---|---|---|
| Ingest interval | 30 min | 15 min | (n/a — hardcoded 600s fallback) |
| Publish interval | 30 min | 15 min | (n/a — hardcoded 3600s fallback) |
| Score threshold | **6.5** | 6.0 | 6.0 / 6.0 |
| Within-batch dedup | 0.70 | **0.65** | 0.70 / 0.70 |
| Cross-batch dedup | **0.70** | **0.65** | 0.80 / 0.75 |
| Autopilot | **ON** | **ON** | (n/a — `False` fallback) |
| Gemma verify | ON | ON (unused) | `enabled: true` |

## ARTICLE LIFECYCLE

```
[Source fetch]  (2h window — rss.filter_feed_hours)
    ↓ URL hash dedup  atomic Redis SET NX EX: newsrooms:dailynews_v2_test:title_hash:{sha256_16}  TTL 10800s
    ↓ Claude score  drop if < 6.0 (both pipelines) — batch 5, max_tokens 1500
    ↓ [DailyNews only, if editor_review.enabled] Editor review A/B branch
         3-prompt chain: intake triage → CCP exposure → ChinaX style → article.editor_post
         chain output is the FINISHED post — never re-run through post_gen
         triage QUALIFIES:no / any error → no variant. NEVER filters the article list.
    ↓ Post generation (55–75 words, English only, ALL content LLM-rewritten)
         English source body < 40 words → DROP (no post generated)
         non-English never word-filtered (CJK has no spaces → word_count meaningless)
    ↓ [DailyNews only] Gemma verification  FAIL/REVISE = drop (API error = PASS, fails open)
    ↓ Within-batch cosine dedup  threshold 0.70  (on qualifying articles only)
    ↓ Cross-batch Qdrant dedup  threshold 0.80, 48h window  (upserts survivors)
    ↓ NotionReviewAgent.enqueue_article()
         → Redis hash: review_test:pending:{article_id}  TTL 86400s
         → Notion page created (Decision: Pending)

Notion Decision state machine:
  Pending     → awaiting human (manual mode — page is CREATED as Pending)
  Approved    → poll detects → lpush to publish queue → Notion: Queued
                (topical dedup no longer runs here — see below; this transition is
                effectively dead under auto-pilot anyway, since autopilot never creates
                a "Pending" card in the first place)
  Queued      → in publish queue or being published.
                AUTOPILOT: page is CREATED directly as "Queued" (not "Approved") because
                enqueue_article() already lpush'd it. The poll ignores "Queued", so this is
                what prevents the autopilot double-enqueue bug. Never change this back to
                "Approved" — see agents/notion_review_agent.py:230.
  Publish Now → poll detects → publish_one() immediately → Notion: Queued
  Rejected    → Redis key deleted → Notion: Discarded
  Published   → Gettr succeeded → [DN only] _on_posted sets this + Published date + send_status=True
  Discarded   → silent bot drop → [DN only] _on_dropped sets this. Reasons:
                no image / image too small / image is a source logo / SHA1 dup /
                article-ID dup / post < 55 words OR user-rejected
                (the three image reasons only drop once the AI video fallback has
                 declined — switch off, quota spent, or generation failed)

DailyNews fallback: publish queue empty → _notion_fallback() queries Notion for
  (Decision=Approved OR Queued, Created in last 6h) sorted by Score desc → rebuild expired Redis hashes
```

## REDIS KEY SCHEMA

```
DailyNews (config.yaml redis.* section):
  url dedup:      newsrooms:dailynews_v2_test:title_hash:{sha256_16}   TTL 10800s
  review pending: review_test:pending:{article_id}                      TTL 86400s
  review queue:   review_test:queue                                     (List)
  publish queue:  publish_test:queue                                    (List)
  post dedup:     newsroom:dailynews_v2_test:post:{sha1_12}            TTL 864000s
  article dedup:  newsroom:dailynews_v2_test:post:article:{article_id} TTL 864000s
  video quota:    newsroom:dailynews_v2_test:post:videogen:24h         (ZSET) TTL 172800s
  image seen:     newsroom:dailynews_v2_test:post:imgseen:{sha256_16}  TTL 604800s

EpicFury (config.yaml epicfury.redis_* section):
  url dedup:      epicfury:title_hash:{sha256_16}                      TTL 10800s
  review pending: epicfury:review:pending:{article_id}                  TTL 86400s
  review queue:   epicfury:review:queue                                 (List)
  publish queue:  epicfury:publish:queue                                (List)
  post dedup:     epicfury:post:{sha1_12}                               TTL 864000s
  article dedup:  epicfury:post:article:{article_id}                    TTL 864000s
  video quota:    epicfury:post:videogen:24h                            (ZSET) TTL 172800s
  image seen:     epicfury:post:imgseen:{sha256_16}                     TTL 604800s
```

The last two are derived from `post_hash_key_prefix`, so every pipeline (and every
config-driven channel) gets its own automatically. **`videogen:24h` is a sorted set**,
member = article_id, score = epoch seconds — a true rolling window, pruned with
`ZREMRANGEBYSCORE` on every read. `imgseen:` is a plain counter feeding source-logo
detection (see `utils/logo_detect.py`).

**Article-ID dedup key** (`{post_hash_key_prefix}article:{article_id}`) — added because the
SHA1 key is `sha1(post_content[:500] + first_media_url)`, and re-ingestion re-runs the LLM, so
the post text (and therefore the SHA1) differs every cycle. The article-ID key is stable across
regeneration. Written in `_mark_posted()` alongside the SHA1 key; checked in `_process_one()`
*before* the SHA1 check. Applies to **both** pipelines (not gated on `pipeline_type`). Both
checks emit the same SSE step name `sha1_dedup` — that's intentional, the dashboard graph has
one node for publish-time dedup. The SHA1 key is still **written** in `_mark_posted()` (the
legacy n8n "waiting for post" workflow reads this same Redis namespace before it publishes),
but as of the topical-dedup rewrite it no longer **gates** DailyNews's own publish decision —
see "Topical dedup (DailyNews only)" below, which runs immediately after this SHA1 write-step
in `_process_one()` and does gate on a match (when `enforce_recent_skip` is on).

EF keys come in via `ef_redis_keys` dict built in `main.py` lines 420–429. DailyNews reads from `redis._config.*` directly. The field name difference (`post_hash_key_prefix` in RedisConfig vs `redis_post_hash_prefix` in EpicFuryConfig) is bridged at `main.py` line 426.

## PUBLISH ROUTING (most common mistake source)

```
_process_one() pre-checks, in order (before any media/publish work):

  1. 55-word floor  [DailyNews ONLY — pipeline_type != "epicfury"]
       len(post_content.split()) < _DN_MIN_WORDS (55)
         → _cleanup() + _notify_dropped() + return "skipped"          ← DROP
       Last line of defense: catches short LLM output AND the raw-title fallback.
       EpicFury is intentionally exempt (it may post short items / OG previews).
  2. Article-ID dedup  [both pipelines]
       EXISTS {post_hash_prefix}article:{article_id}
         → _cleanup() + _notify_dropped() + return "skipped"          ← DROP
  3. SHA1 post-hash  [both pipelines]  — NOT a gate any more, just computed here
       (the key is written in _mark_posted for the legacy n8n workflow to read)
  4. Topical dedup  [DailyNews ONLY, and only if _topical_dedup was wired]
       await self._topical_dedup.before_publish(post_content)
         → _cleanup() + _notify_dropped() + return "skipped"          ← DROP
       Only returns True when enforce_recent_skip is ON; default OFF = log-only.

then dispatches:

DailyNews (pipeline_type != "epicfury"):
  has media_urls → _publish_with_media()
    per-URL: source logos set aside, images too small dropped
    nothing uploaded → _try_video() → post video                       ← AI VIDEO
                    → else upload the set-aside logos                  ← LOGO IMAGE
                    → else _cleanup() + _notify_dropped()              ← DROP
  no media_urls  → _publish_dailynews()
    img_url is nulled by ANY of: absent · source logo · too small
      "too small" = area < 120_000 px² OR min(w,h) < 120 px
      (dimensions undeterminable → allowed through)
    then ONE exit:  _try_video() → post video                          ← AI VIDEO
                 → else, if it was a LOGO, post the logo after all     ← LOGO IMAGE
                 → else _cleanup() + _notify_dropped() + return None   ← DROP

EpicFury:
  has media_urls → _publish_with_media()
    all uploads fail → _try_video() (off by default) → _publish_without_media()
  no media_urls  → _publish_without_media()
    _try_video() first (off by default), else                          ← OG PREVIEW
```

`_try_video()` returns None — and the caller falls through to the behavior above —
whenever: the switch is off, `max_24h` is 0, the 24h quota is spent, generation fails
or times out, an asset came back rights-unverified, or the CDN upload fails. It never
raises. **A video post skips `_post_editor_twin`**: the A/B measures editorial voice,
and the twin can only re-upload from source URLs, which a generated video has none of.

The three "unusable image" cases in `_publish_dailynews` were deliberately collapsed
into one `if not img_url:` exit so the video attempt and the drop each exist in exactly
one place. Do not re-split them.

**The source-logo path is NOT symmetric with the other two.** Before the video fallback
existed, a masthead image was simply posted. So when no video is produced, the logo is
restored and posted — `logo_fallback` in `_publish_dailynews`, `logo_skipped` in
`_publish_with_media`. Without that, switching the feature OFF would start dropping
articles that used to publish. "No image" and "too small" always dropped, so they have
no restore path. Do not "simplify" this into one uniform branch.

`None` return → treated as `"skipped"` in `run()`. For DailyNews: stop current article, try next. There is NO text-only DailyNews post path.

## AI VIDEO FALLBACK (posts with no usable image)

Instead of dropping a story for lack of a picture, render a ~25s narrated motion-news
MP4 from the post text and publish that. Gated by a dashboard switch **and** a rolling
24h cap, because each video costs ~90s of near-100% CPU on both cores.

- **The generator is a vendored Claude Code skill**, `video/scripts/` (upstream
  `nfsctech/short-news-video`). `services/video_client.py` shells out to
  `make_news_video.py` with `nice -n 10`, a hard timeout, and a module-level
  `asyncio.Semaphore(1)` so DN, EF and every channel share ONE render slot. Read
  `video/UPSTREAM.md` before touching anything under `video/` — it lists every local
  patch, and re-vendoring upstream means re-applying them.
- **Output is 960x720 (4:3), x264 `veryfast`, sentence-level subtitles.** Karaoke
  subtitles are OFF on purpose: they cost one looped full-frame PNG ffmpeg input *and*
  one `overlay` per spoken word (~70 for a 30s read). Measured cost of the current
  settings: ~91s wall, ~814 MB peak RSS, 2-10 MB output.
- **Narration is edge-tts** (`en-US-AndrewNeural`), an **unofficial** Microsoft
  endpoint. An OpenAI provider exists in `generate_tts.py` but the current key's
  project has no speech model enabled (403 on `tts-1` / `gpt-4o-mini-tts`). If edge-tts
  breaks, videos silently stop and DailyNews goes back to dropping — grep logs for
  `[tts]` and `Video render exited`.
- **Imagery is Wikimedia-only and `--article-url` is never passed.** Every asset is
  therefore licence-verified or a self-produced card, credits are burned in
  automatically, and no human needs to read `SOURCES.md`. `VideoClient` discards any
  render containing an unverified asset.
- **`prompts/video_brief.txt` returns `subjects`, a LIST — not one query string.**
  Commons ANDs every term against file metadata, so one conceptual phrase
  ("Vatican Beijing Catholic bishops China") returns **zero** results while the
  entities inside it ("Pope Francis", "Vatican City") each return plenty. `fetch_media.py`
  searches each subject separately with a per-query cap. Getting this wrong produces a
  video of five plain text cards instead of photographs — it is the single highest-leverage
  thing in the whole feature.
- **Quota is charged on a successful RENDER**, before upload — the CPU is what is being
  rationed, so a later Gettr failure does not refund it.
- **Source-logo detection** (`utils/logo_detect.py`) makes a masthead count as "no
  image". Two signals: URL shape, and the same image URL appearing on >=3 articles
  (`imgseen:` counter). `services/pollinations_client.py` re-exports its constants —
  they originated there.
- **Per-channel branding** is `video/brand/<slug>/` (brand.json + logo.png + outro.mp4),
  falling back to `video/brand/dn/`. The outro is prebuilt offline per brand at the
  output resolution via `make_outro.py --width --height`; it is never rendered at
  publish time. EF has no logo, so EF videos end on the static end card.

## EDITOR REVIEW A/B BRANCH (DailyNews only)

Trials a different editorial voice by posting **every DailyNews story twice** — the standard
post to the live `gettr` account, the editor-revised post to the `gettr_test` account — so the
two treatments can be compared on the same story, same image, same moment.

- **Ingestion:** `core/pipeline.py` runs an `editor_review` step between `claude_score` and
  `post_gen`, gated on `not socials_mode and self._editor.enabled`. `services/editor_client.py`
  chains 3 prompts (`prompts/ai_editor_intake_triage_prompt.md` →
  `ai_editor_ccp_exposure_system_prompt.md` → `unveiled_chinax_style_prompt.md`) into
  `article.editor_post` — a **finished post**, used verbatim. It is deliberately NOT re-run
  through `generate_post`: a fourth rewrite under `generate_post_system.txt` strips the closing
  device and the prosecutorial voice, which is the thing the A/B is measuring.
- **The branch can never cost the live channel an article.** Triage `QUALIFIES: no`, an unparseable
  verdict, an empty body, or any exception → no variant, and `qualifying` is never filtered.
  Triage fails **open** (unrecognised verdict = qualifies), same convention as Gemma.
- The triage step returns a **structured brief**, not just a verdict. That brief is the CCP step's
  primary input (the source article is attached under it as a fact reference), matching the triage
  prompt's own "hand this brief, unmodified, to the drafting editor" instruction. If you change the
  triage prompt's output format, update `_QUALIFIES_RE` in `services/editor_client.py` with it.
- **Gemma verify runs on `llm_post` only.** The editor chain is *about* CCP exposure, so running
  the CCP-content filter over its output would defeat the experiment. Do not "fix" this.
- **Publishing is a twin, not a lane.** `PublishAgent._post_editor_twin()` fires from the two DN
  success paths (`_publish_dailynews`, `_publish_with_media`) *after* the live post succeeded.
  Best-effort: it swallows every exception, so a broken test account can never affect the live
  post, the Notion state, or the dedup keys. It has **no dedup keys of its own** — it is slaved
  to a post that already cleared every gate, so it cannot double-post.
- The image is **re-uploaded** for the test account: `GcpClient` requests its upload channel with
  the account's own Gettr auth, so the live account's CDN metadata is not reusable.
- The twin is **exempt from `_DN_MIN_WORDS`** — that floor protects the live channel only.
- `editor_post` travels in the existing `review_test:pending:{id}` Redis hash (a `ReviewItem`
  field). Nothing is written to Notion. Known gap: `_handle_decision` rebuilding an expired hash
  from Notion loses the variant, so that article posts to the live account only.
- **Config:** `editor_review` + `gettr_test` in `config.yaml`, editable in the dashboard Config
  tab. Adding a `gettr_test` block that was absent at boot needs a restart (the client objects are
  built in `main.py`); everything else hot-reloads. The 3 prompts are dashboard-editable and
  reload through `EditorReviewClient.reload_prompts()`, **not** `claude_client.reload_prompts()`.
- **Cost:** 3 LLM calls + 1 extra post-gen per qualifying article, but only one article per publish
  run actually goes out. `editor_review.max_per_run` caps it to the top N by score (0 = all).

## CALLBACK WIRING

```python
# main.py lines 377–379 — DailyNews ONLY
publish_agent._on_posted       = review_agent.update_posted_page     # after Gettr success
publish_agent._on_dropped      = review_agent.update_dropped_page    # after silent drop
publish_agent._notion_fallback = review_agent.get_fallback_articles
```

`publish_ef` never gets these. Do NOT add them to EF.

`notion_page_id` is NOT in `ReviewItem`. Written to Redis hash by `_create_notion_page()`, read back by `_process_one()` via `data.get("notion_page_id")`. Also stored in reconstructed hash by `_handle_decision()` when Redis key has expired.

## KNOWN GOTCHAS

- `url_hash_ttl_s` must always be **>=** `rss.filter_feed_hours * 3600`. If the TTL expires
  faster than the fetch window, the same article re-enters the pipeline every cycle (this is
  exactly how the 5×-duplicate-publish incident happened). Currently they are NOT equal:
  `filter_feed_hours: 2` (7200s) vs `url_hash_ttl_s: 10800` (3h). That direction is safe —
  the dedup key outlives the window. **The inline comment in `config.yaml` ("3 hours (matches
  filter_feed_hours)") is stale.** Never lower the TTL below the window.
- `gemma.enabled: true` — active CCP-content filter. Do not disable. On API error the client
  returns `PASS` (fails open) so a Gemma outage can't stall the pipeline.
- Redis URL dedup uses **atomic `SET key "1" NX EX ttl`** in one pipeline call
  (`RedisClient.batch_setnx_with_ttl`). It used to be `SETNX` + a separate `EXPIRE` pipeline;
  Upstash evicted keys between the two calls and every article passed dedup forever. Do not
  split it back into two commands.
- Post word count is enforced in **two places** and they must stay consistent: `claude_client.py`
  retries once outside 55–75 words (then uses the result anyway), and `publish_agent.py` hard-drops
  DailyNews posts under 55 words. Raising the generation minimum without raising `_DN_MIN_WORDS`
  is safe; lowering `_DN_MIN_WORDS` below the generator's minimum makes the floor dead code.
- `claude.max_tokens: 1500` / `batch_size: 5` — do NOT lower. At 512/10 the scoring JSON
  truncated and `_parse_scores` zeroed entire batches. The pydantic defaults in `core/config.py`
  are still the old 512/10; `config.yaml` is what makes it correct.
- `data/schedule.json` (DN) and `data/schedule_ef.json` (EF) persist thresholds/autopilot/paused
  state across restarts. Editing the files by hand has no effect until restart. **They also OVERRIDE
  `config.yaml` — see "RUNTIME VALUES" below. Never quote a config.yaml threshold as the live value.**
- **AI video needs `ffmpeg` and `edge-tts` on the host.** Neither is a Python-only
  dependency the venv can restore: `sudo apt-get install ffmpeg` and
  `pip3 install edge-tts`. If ffmpeg goes missing the renders fail, `_try_video`
  returns None, and DailyNews silently reverts to dropping image-less articles — the
  failure is invisible except in the logs. Same for a `video/brand/<slug>/outro.mp4`
  rendered at the wrong resolution: it will be stretched, not rejected.
- `telegram.enabled: false` — Telegram review disabled. `NotionReviewAgent` is the active review mechanism.
- `PublishAgent.run()` delegates to `_run_inner()` via `asyncio.Lock`. `is_running` checks `self._lock.locked()`. Never call `_run_inner()` directly.
- Topical dedup (`NotionTopicalDedupChecker`) runs at PUBLISH time in `PublishAgent._process_one`/`_mark_posted` (DailyNews only), not at Notion approval and not during ingestion — approval-time was a dead hook under auto-pilot, since autopilot never creates a "Pending"/"Approved" card. It uses OpenAI embedding cosine similarity against the `daily_news` Notion board only (not `agent_queue_dailynews` — nothing writes to that board manually any more, so comparing against it was comparing the bot against itself). `before_publish()` checks `send_status=True` cards within `recent_lookback_hours` (skip gated by `enforce_recent_skip`, default off — shadow mode); `after_publish()` checks not-yet-sent cards (`status` in `2nd_eye`/`waiting for post`) and marks `Duplicate`+`Notes` with the new Gettr link. A third loop, `run_gettr_crosscheck_loop()`, separately compares not-yet-sent cards against this pipeline's own recent Gettr posts every `gettr_crosscheck_interval_minutes`. `prompts/notion_topical_dedup.txt` is now unused (was the old Claude Haiku prompt) — left in place, harmless.

## MOST COMMONLY NEEDED FILES

```
Publish behavior:         agents/publish_agent.py
Pipeline stage logic:     core/pipeline.py
Editor A/B branch:        services/editor_client.py + publish_agent._post_editor_twin()
AI video fallback:        services/video_client.py + publish_agent._try_video()
                          video/ (vendored skill — read video/UPSTREAM.md first)
Source-logo detection:    utils/logo_detect.py
Callback wiring:          main.py lines 372–395
Notion review SM:         agents/notion_review_agent.py
Topical dedup:            services/notion_topical_dedup.py
Redis key resolution:     main.py lines 420–429 (ef_redis_keys dict)
Config structure:         core/config.py
Word-count enforcement:   services/claude_client.py (55–75 retry) + agents/publish_agent.py (_DN_MIN_WORDS)
```

## DOCUMENTATION MAP

| File | Audience | Contents |
|---|---|---|
| `README.md` | new dev / operator | What it is, install, run, operate, troubleshoot |
| `CLAUDE.md` (this file) | Claude Code | Gotchas, invariants, DN-vs-EF differences |
| `DESIGN.md` | engineer | Full system design — every stage, schema, endpoint |
| `docs/bot-workflows.md` | non-technical stakeholder | Plain-language walkthrough of both pipelines |
| `docs/gettr-posting.md` | engineer | Gettr API reference (auth, payloads, media upload) |
| `docs/media_fetching_improvements.md` | engineer | Media/OG-image fetching design notes |
| `templates/channel/` | operator | Recipe + templates for adding a new channel |
| `video/UPSTREAM.md` | engineer | Vendored video skill: provenance, every local patch, host deps |

When pipeline behavior changes, `CLAUDE.md` + `DESIGN.md` + `docs/bot-workflows.md` all need the
edit — they describe the same flow at three different altitudes.
