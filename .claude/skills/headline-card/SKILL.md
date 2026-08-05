---
name: headline-card
description: >-
  Generate a branded 960×1200 headline poster (title + optional tag badge +
  channel logo) for the DailyNews (dn) or EpicFury (ef) pipeline. Finds a
  person photo via DuckDuckGo when given a name, else falls back to a
  plain-background card so a post never goes out image-less. Use when a
  DN/EF article has no usable image and you need to synthesize one, or when
  the user asks to "make a headline card / poster / 配图" for a story.
---

# headline-card

Packages the `headline-card-generator` project as a skill for both pipelines.
It renders a poster image you can upload as the article's `media_urls[0]`.

## When to use

- A DailyNews or EpicFury article has no image (DN would otherwise **drop** it;
  EF would fall back to an OG preview) and you want a real branded image.
- The user asks for a headline card / news poster for a given headline, with
  the channel's logo.

## Prerequisites (check once)

1. **Python deps** (already present on this box; installs into the venv if not):
   `pip install -r .claude/skills/headline-card/requirements.txt`
   Adds `httpx` and `PyYAML` — note the main agent uses `aiohttp`, so `httpx`
   is specific to this skill.
2. **A font to draw text.** This box has *no* system font by default, so
   rendering fails until one exists. Install the free CJK font (covers Latin +
   Chinese) — the vendored `fonts.py` auto-detects it afterward:
   `sudo apt-get install -y fonts-noto-cjk`
   Or point at any `.ttf/.ttc/.otf` via `--font PATH`, the `HEADLINE_CARD_FONT`
   env var, or `font_path:` in `channels.yaml`.
3. **Channel logos**: `assets/dn_logo.png` ships with the DailyNews logo.
   **EpicFury intentionally has no logo** (`logo_path: ""` in `channels.yaml`).
   To rebrand DN, replace `assets/dn_logo.png` (transparent PNG looks best — the
   current file is an opaque JPG, so it renders as a solid square).

## Usage

```bash
cd /home/leon/dailynews-agent
python3 .claude/skills/headline-card/generate.py \
    --pipeline dn \
    --title "Company X unveils new product, industry takes notice" \
    --person "Elon Musk"           # explicit image-search term (optional)
    # --auto                       # no --person: extract the subject from the title (LLM)
    # --description "..."          # optional; improves --auto extraction
    # --tag BREAKING               # overrides channels.yaml default_tag
    # --out /tmp/card.png          # default: <skill>/output_<pipeline>.png
    # --font /path/to/font.ttf     # overrides font auto-detection
```

`--pipeline` selects branding from `channels.yaml` (`dn` = DailyNews,
`ef` = EpicFury): logo file, logo position, and default tag. On success the
script prints **only the absolute PNG path** to stdout — capture that as the
image to publish. It exits non-zero with an `error:` line on a font/config
problem.

### Choosing the image (person vs keywords)

The photo search needs a search term. Three ways to supply it, in precedence:

1. `--person "Full Name"` — explicit, no LLM call. Use when you already know
   the subject (e.g. the pipeline identified it).
2. `--auto` — extract it from the title with an LLM (`extract.py`). Returns a
   `person` when the story centers on one named individual, else `keywords`
   for a non-person subject (company, country, weapon system, event), and a
   single best `query` used for the search. Prints `auto: subject=...` to
   stderr. Provider defaults to **OpenAI `gpt-4o-mini`** (the model this
   project already uses); `OPENAI_API_KEY` or `openai.api_key`. Set
   `HEADLINE_CARD_PROVIDER=anthropic` for `claude-haiku-4-5` instead.
3. Neither — renders a plain branded card (no search).

`extract.py` is also runnable standalone: `python3 extract.py "headline text"`.

## How it fits the pipelines

Both pipelines upload `media_urls[0]` as the post image (`_publish_with_media`
in `agents/publish_agent.py`). Point that at the generated PNG:

- **DailyNews**: no image = dropped article. Generate a card, set it as the
  article's media, and the article survives instead of being dropped.
- **EpicFury**: no image = OG-preview fallback. A generated card gives a
  richer branded image instead.

Choose `--pipeline dn` vs `ef` so the right channel logo/branding is used.

## Caveats

- **DuckDuckGo photo search is an unofficial, undocumented endpoint** — it can
  break without notice and returns no key-authed guarantee. The skill degrades
  to a plain card (never errors) when search fails; watch stderr for `note:`.
  (It is unreachable from the current host, so cards render plain here.)
- **`--auto` can infer a WRONG name.** When a headline names only a *role*
  ("Taiwan President", "the CEO"), the LLM guesses a specific name and may be
  stale/incorrect (observed: "Taiwan President" → an out-of-date name). A wrong
  name means a wrong person's photo. For role-only headlines, prefer keywords
  or a plain card; verify the name before trusting a person photo.
- **Title wrapping is per-character** (`_wrap_by_pixel` in `card.py`) — designed
  for CJK. For English headlines it can break mid-word. Keep titles short
  (photo card shows ~4 lines, plain card ~5); trim rather than rely on wrapping.
- Never post a photo of a real person in a misleading context — the fetched
  photo must actually match the story's subject. If unsure, use a plain card.

## Files

```
SKILL.md                     this file
generate.py                  CLI wrapper: --pipeline dn|ef, branding + photo/plain fallback
extract.py                   --auto subject extraction (person vs keywords) via LLM
channels.yaml                per-channel logo / position / default tag / font
assets/                      dn_logo.png (shipped); EF has no logo by design
headline_card_generator/     vendored package (card.py, search.py, fonts.py)
requirements.txt             Pillow, httpx, PyYAML, anthropic
```
