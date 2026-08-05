# Social-media news channel template

A **channel** is one self-contained news feed: it scrapes its own X accounts + websites,
scores/rewrites with its own editorial prompts, keeps its own dedup namespace, routes to its
own Notion review board, and publishes to its own Gettr account on its own dashboard port.

This template is modelled on the **EpicFury (EF)** pipeline — *not* DailyNews. That means:

| Property                | Value (inherited from EF)                                  |
|-------------------------|------------------------------------------------------------|
| Sources                 | X/Twitter accounts + websites (a `sources/*.md` file)      |
| Publish order           | FIFO, try every queued item, requeue failures ×3           |
| No-image behaviour      | Fall back to an OG link-preview post (never drops)         |
| Notion callbacks        | **None** — no `_on_posted` / `_on_dropped` / `_notion_fallback` |
| Gemma CCP filter        | Off                                                        |
| Topical dedup           | Off                                                        |
| `pipeline_type`         | `"epicfury"` (drives all of the above in `publish_agent.py`)|

> The `pipeline_type="epicfury"` string is what selects EF behaviour throughout
> `agents/publish_agent.py` and `agents/notion_review_agent.py`. Every channel built from this
> template passes that same string — it is a **behaviour mode**, not the name of one channel.
> Do not invent a new mode string; the code only understands `"epicfury"` and DailyNews-default.

## Fully config-driven — no code edits

`main.py` builds every entry in the top-level `channels:` list of `config.yaml` via
`core/channel_runtime.build_channel()`. Adding a channel means: provision external
resources, drop in prompt/source files named after the slug, append one YAML entry,
restart. No changes to `core/config.py` or `main.py`. DailyNews and EpicFury remain
hardcoded and untouched; new channels ride the generic path.

Redis key prefixes are derived from the channel `slug` automatically, so channels can
never collide as long as their slugs differ.

## What's in this package

```
templates/channel/
  README.md                     ← you are here
  DEPLOY.md                     ← the step-by-step runbook Claude Code follows
  config.snippet.yaml           ← one `channels:` entry to append to config.yaml
  sources.md                    ← X-accounts + websites source list template
  prompts/
    score_articles.txt          ← relevance scoring prompt (0–10)
    generate_post_system.txt    ← post-writing system prompt
    generate_post_user.txt      ← post-writing user template
```

## How to use it

Open **DEPLOY.md** and follow it top to bottom. Everywhere you see a `__PLACEHOLDER__`
token, replace it with the value for the new channel (see the token table in DEPLOY.md).
Nothing in this directory is imported at runtime — copy files *out* of it into the live tree.
