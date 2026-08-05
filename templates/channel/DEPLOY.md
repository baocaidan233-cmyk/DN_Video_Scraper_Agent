# Deploy a new channel — runbook

Adding a channel is now **fully config-driven: no code edits**. `main.py` builds every
entry in the top-level `channels:` list of `config.yaml` (see `core/channel_runtime.py`).
You provision external resources, drop in prompt/source files, add one YAML entry, restart.

Read `README.md` first for what a "channel" is and the EF behaviour it inherits.

> **Conventions.** `__CHANNEL__` is the machine slug: **lowercase letters/digits only**, no
> spaces/hyphens/underscores (it seeds Redis key prefixes and must satisfy `slug.isalnum()`).
> Example: `worldpulse`. Use the exact same slug in the config entry and in the file names below.

## 0. Token table

| Token                    | Meaning                                             | Example              |
|--------------------------|-----------------------------------------------------|----------------------|
| `__CHANNEL__`            | slug (lowercase alphanumeric)                       | `worldpulse`         |
| `__CHANNEL_TITLE__`      | human title (dashboard header)                      | `World Pulse`        |
| `__TOPIC__`              | one-line editorial beat (used in prompts)           | `global climate policy` |
| `__PORT__`               | dashboard port — free & unique, not 8080/8081       | `8082`               |
| `__GETTR_USER_ID__` / `__GETTR_JWT__` | the channel's Gettr account creds      | —                    |
| `__NOTION_REVIEW_DB_ID__`| 32-char id of the new review database               | —                    |
| `__QDRANT_URL__` / `__QDRANT_API_KEY__` | Qdrant cluster + rw key on the new collection | —          |
| `__X_*__`, `__TWITTERAPI_KEY__`, `__SOCIALDATA_KEY__` | scraper creds (may reuse existing) | —     |
| `__KEYWORD_n__`, `__X_ACCOUNT_n__`, `__WEBSITE_n__` | channel's sources & keywords | —           |

## 1. Provision external resources (by hand / API — code does not create these)

1. **Gettr account** — the account the channel posts to; get its `user_id` + long-lived JWT.
2. **Notion review database** — duplicate the EpicFury review DB (same properties; critically the
   `Decision` select with: Pending, Approved, Rejected, Publish Now, Queued, Published, Discarded).
   Share it with the Notion integration whose token you'll use. Copy its 32-char id.
3. **Qdrant collection** — create `__CHANNEL___embeddings`, vector size **1536**, distance **Cosine**;
   ensure the api_key has `rw` on it. (Omit the `qdrant:` block to share the DailyNews collection —
   not recommended; dedup would mix channels.)
4. **X scraping** — reuse the shared twitterapi.io / socialdata.tools keys, or provision a dedicated
   X login. `data/__CHANNEL___x_cookies_1.txt` is created at runtime.
5. **Dashboard port** — pick a free `__PORT__` and open it in the GCP firewall for remote access.

## 2. Copy prompt + source files (names MUST match the slug)

```bash
CH=__CHANNEL__
cp templates/channel/prompts/score_articles.txt        prompts/${CH}_score_articles.txt
cp templates/channel/prompts/generate_post_system.txt  prompts/${CH}_generate_post_system.txt
cp templates/channel/prompts/generate_post_user.txt    prompts/${CH}_generate_post_user.txt
cp templates/channel/sources.md                        sources/${CH}_sources.md
```

`channel_runtime` looks for `prompts/<slug>_{score_articles,generate_post_system,generate_post_user}.txt`
by default. Edit the three prompt files (replace `__TOPIC__`, `__CHANNEL_TITLE__`, the `__LENS_n__`)
and the sources file (X handles + website URLs). `generate_post_user.txt` has no placeholders.

## 3. Add the channel entry to `config.yaml`

Take `templates/channel/config.snippet.yaml`, replace every `__PLACEHOLDER__`, and append the
entry under a top-level `channels:` list (create the key once if absent). That's the only config
change — no `core/config.py` edit, no `main.py` edit.

Sanity checks (a bad entry fails fast at startup via a config validator):
- `slug` is lowercase alphanumeric and unique across channels.
- `dashboard_port` is unique and not 8080/8081.
- `telegram.bot_token` is **non-empty** even though `enabled: false` — it gates pipeline start.
- Redis keys are auto-derived from the slug; you do **not** set them.

## 4. Restart & verify

```bash
python3 -c "from core.config import load_config; c=load_config('config.yaml'); \
  print('channels:', [ch.slug for ch in c.channels])"
sudo systemctl restart dailynews-agent
```

Validation criteria — all must pass:

1. **Process up**: `systemctl status dailynews-agent` → `active (running)`; no traceback in
   `journalctl -u dailynews-agent -n 80`.
2. **Init logged**: journal shows `Initializing channel __CHANNEL__` then `Channel __CHANNEL__ initialized`
   (not "...init failed" or "credentials not set").
3. **Dashboard up**: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:__PORT__/login` → 200/302,
   and the page header reads `📡 __CHANNEL_TITLE__`.
4. **Ingestion runs**: within one cycle the channel scrapes X/websites; a card appears in the new
   Notion review DB; the dashboard's Sources/Prompts editors show this channel's files.
5. **Publish path**: set a card's Decision=Publish Now in Notion → it posts to the channel's Gettr
   account within ~60s. Check the History tab shows runs under `__CHANNEL__` / `__CHANNEL___publish`
   (isolated from other channels).

If step 2 shows "credentials not set", re-check `gettr.user_token` and a non-empty `telegram.bot_token`.

## 5. Prompt tuning (no restart)

The three prompt files are hot-reloadable from this channel's dashboard (Prompts tab →
`pipeline.reload_prompts()`). The Sources tab edits `sources/<slug>_sources.md` live.

---

### Rollback / disable

Set `enabled: false` on the channel entry (keeps config, stops the pipeline; dashboard still serves),
or delete the entry entirely, then restart. Delete the copied prompt/source files, the Qdrant
collection, and the Notion DB manually if you want a full teardown. Redis keys expire on their TTLs.
