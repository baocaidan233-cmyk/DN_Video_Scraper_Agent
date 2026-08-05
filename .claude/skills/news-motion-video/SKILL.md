---
name: news-motion-video
description: Turn a short written news story into a polished 20-30s motion-news video (1920x1080+) with an animated chyron, open-license imagery, English narration, ducked music, time-synced subtitles, and a branded follow end card. Use when a user supplies a news story or script plus an account logo and handle and wants a finished MP4 for a news-style social account. Trigger on requests like "make a news short from this story", "turn this into a 30-second news clip", "motion-news video with our logo". Do NOT use for long-form video, non-news product ads, or when the user only wants a script or a voiceover with no video.
---

# News motion video

Produce a broadcast-style short from a written story. The output must be accurate to the source, legally clean on every asset, and readable on a phone.

## Standing brand preferences (AUTO-APPLY — do not ask the user again)

The operator has set these once and does **not** want to be asked about them on future videos. Machine-readable defaults live in **`brand/brand.json`** and the orchestrator loads them automatically; apply them to every video unless the user explicitly overrides one.

- **Logo — always use `brand/logo.png`.** Never ask the user to supply a logo again. (Claude Code cannot save chat-pasted images to disk, so this file is a faithful *recreation* of the user's DAILY NEWS logo. If the user ever drops a new file at `brand/logo.png`, it silently replaces it — but do not request it.)
- **Handle:** `@DailyNews`. **Accent:** red `#E60023`; **outro accent:** `#FF1E1E`.
- **Voice:** neural via edge-tts (`en-US-AndrewNeural`) at a natural news pace. Do **not** ship the robotic macOS `say` voice unless edge-tts is truly unavailable — `say` is fallback only.
- **Subtitles:** karaoke style — each word highlights **yellow** as it is spoken.
- **Chyron:** animates **in → holds → out** (slides/fades away before the tail).
- **Ending:** the **animated 3s outro**, prebuilt once as **`brand/outro.mp4`** and reused automatically every video (regenerated via `make_outro.py` only if that file is missing). Its exact spec (do not water it down):
  - *Step 1 (0–1s) Dynamic roll-in:* logo enters fast from the left **spinning on its vertical axis (real 3D perspective, not a flat squash)**, tilted ~12° clockwise, trailing **3–5 bright red speed streaks (~1/3 screen wide)** with motion blur that decreases as it slows; strong ease-out.
  - *Step 2 (1–2s) Locks into place:* rotation straightens to head-on, a **2–3px elastic overshoot** settles, an **elliptical red glow blooms** beneath (expands → peaks → holds ~28%; NOT a hard line), a faint **reflection** fades within ~0.5s, a subtle **left-to-right highlight sweep** crosses the logo, then a short still hold. Restrained/premium (CNN/Bloomberg/Apple-keynote feel).
  - *Step 3 (2–3s):* red **FOLLOW** pill fades up from below (+~25px) with a soft glow and gentle easing, then holds.
  Deep-black `#0A0A0A`, red `#FF1E1E`. (Static end card only if no logo, or `--no-outro`.)
- **Music:** always include the self-produced bed — **audible** (~−20 LUFS), **ducked** under the voice, **swelling** where no one speaks, and **gradually dimmed to silence** at the end. Auto-fit to the video length.
- **Source photos first + ≥5 photos.** The user now supplies a story **plus a link** to the outlet that reported it. That article's own photos come FIRST — pull them with `fetch_article_media.py` (or WebFetch for JS-heavy pages), use them in order (lead image first), then **supplement** with open-license stills relevant to the story until the video has **at least 5 photos**. Source-article photos are third-party/agency images → mark `rights_verified: false` (they trigger the SOURCES.md warning; the operator confirms rights before publishing). Never strip credits already burned into a supplied photo. The orchestrator does all of this via `--article-url` + `--query` (min photos from `brand.json` `min_photos`).
- **Attribution:** **automatically burn a tiny, non-interfering credit line** (top-right, small, semi-transparent, fades out before the outro) for any CC-licensed media. The user does not want to manage credits by hand. Public-domain / self-produced assets need no credit.
- **Editorial stance:** narration states only facts supported by the source; **attribute analysis/opinion** ("analysts say…") rather than presenting it as fact. Keep the account's brand voice; never impersonate another outlet.
- **Format:** 20–30s, 16:9, 1920×1080.

*Decision log (why these are set): the operator reviewed successive drafts and asked for — a real (non-robotic) human voice; a chyron that enters, stays, then leaves; karaoke word-highlight subtitles; audible background music that auto-fits and gradually dims to the end; an animated logo-roll-in + FOLLOW outro; burned-in tiny attribution credits; and a persistent brand logo that never needs re-supplying. All are now defaults above and in `brand/brand.json`.*

## What you need from the user before starting

Required: the story or script, the account logo (image file), and the handle (e.g. `@DailyNews`).
Optional, with sensible defaults: preferred voice, visual style, aspect ratio (default 16:9), brand accent color, target duration (default ~24s).

If the logo or handle is missing, ask once, briefly. Do not invent a logo or scrape one. If the user has no logo, the pipeline drops in a clearly-marked placeholder badge.

## Hard rules (do not skip)

1. **Accuracy over flash.** The headline and narration must not add facts, quotes, causes, or numbers that aren't in the source. If the story is thin, keep the video short rather than padding it. Never attribute invented quotes to real people.
2. **Media must be legally reusable.** Only pull from the allow-listed open sources below, or from files the user supplied. Never scrape arbitrary news photos, article stills, screenshots, or paywalled/stock media you don't have a license for. `fetch_media.py` records source + license for every external asset and discards anything it can't verify as free. If an asset's rights can't be verified, do not use it silently — flag it (`rights_verified: false` forces a warning atop `SOURCES.md`).
3. **No irrelevant filler footage.** Every clip must relate to the story. A generic city-at-night loop over a court-ruling story is not acceptable. When no relevant open-license media exists, use self-produced graphic cards (keyworded), not unrelated stock.
4. **This produces news about real events using the user's own brand.** That's fine. What's not fine is impersonating another outlet's identity or presenting opinion as fact.

## Runtime requirements

Shells out to **FFmpeg** and a **TTS** provider. Confirm FFmpeg first:

```bash
ffmpeg -version | head -1 || echo "MISSING: ffmpeg"
```

Key portability note: **all text (chyron, end card, subtitles) is rendered to PNG with Pillow and composited via the `overlay` filter.** This means the Skill works even on FFmpeg builds *without* `drawtext`/libfreetype or the `subtitles`/libass filter — which includes the common Homebrew build. Requirements:

- **FFmpeg** with `overlay`, `zoompan`, `xfade`, `sidechaincompress` (all standard since 4.3). Validated on 8.1.
- **Python 3** with **Pillow** (`pip install Pillow`).
- A **TTS** provider (auto-detected):
  - **edge-tts** (`pip install edge-tts`) — free Microsoft neural voices, best quality, needs network. Unofficial endpoint, so treat as best-effort.
  - **macOS `say`** — offline fallback, always present on macOS. Robotic but real speech with real timing.

## Workflow

Steps 1-2 are your (the model's) job. Steps 3-8 are mechanical and run through `scripts/make_news_video.py`, which orchestrates the individual scripts and writes every deliverable.

### 1. Condense the story
Extract the one central news point. Write one headline: short (aim ≤ 8 words for chyron readability), accurate, specific, no clickbait. Show it to the user before rendering — cheapest place to catch an accuracy problem.

### 2. Write the narration
55-80 words for a 20-30s read at ~2.7 words/sec. Lead with the news point. Plain declarative sentences a TTS voice reads cleanly (avoid parentheticals and long subordinate clauses).

### 3-8. Run the pipeline
Pass the headline and script in; the orchestrator sources media, synthesizes narration, builds synced subtitles, synthesizes a ducked music bed, assembles `production.json`, renders, and writes deliverables:

```bash
python3 scripts/make_news_video.py \
  --headline "Dutch open major new flood barrier" \
  --script "Dutch water authorities today opened ..." \
  --handle "@DailyNews" \
  --logo path/to/logo.png \
  --query "flood barrier river netherlands ijssel" \
  --fallback-labels "Flood defenses,Climate adaptation" \
  --workdir work --out-dir deliverables
```

What each step does under the hood:

- **Media — source photos first (`fetch_article_media.py`), then supplement (`fetch_media.py`).** When a source-story URL is given, extract that article's photos first (lead/og:image, then in-body photos; junk like logos/icons/pixels filtered out) and mark them `rights_verified: false`. Then supplement with **Wikimedia Commons** open-license stills (filtered to PD / CC0 / CC BY / CC BY-SA with license + attribution captured) until the video has **≥5 photos**. Openverse is a secondary source but currently 403s unauthenticated requests; Wikimedia is reliable. Anything unverifiable from Wikimedia is discarded; source-article photos are kept but warned. Remaining shortfall is filled with self-produced keyworded cards (`gen_still.py`, zero licensing risk).
- **Narration (`generate_tts.py`)** — synthesizes the read, measures the true audio duration with ffprobe. With edge-tts it also writes a **timing sidecar** (`voice.m4a.timing.json`) from the TTS boundary events, which drives karaoke subtitles.
- **Subtitles (`build_srt.py` + karaoke)** — `subs.srt` is always delivered (sentence-level cues distributed across the measured duration). When a timing sidecar exists, the **burned-in** subtitles are rendered **karaoke-style**: each word highlights (yellow) as it is spoken. Otherwise they fall back to static sentence cues.
- **Music (`make_music_bed.py`)** — synthesizes a subtle ambient bed (self-produced → no third-party rights), **normalized to an audible level (~−20 LUFS)**, auto-fit to the video length, with a **gradual fade-out** at the end. The renderer ducks it under the voice with `sidechaincompress` and lets it swell where no one is speaking (e.g. the outro).
- **Animated outro (`make_outro.py`)** — a ~3s branded end sequence rendered frame-by-frame with Pillow: the logo **rolls in from the left with motion blur + a slight 3D rotation**, **settles with a bounce and a soft red glow underneath**, then the **FOLLOW button fades up** with a gentle glow. Deep-black, high-contrast, black/red/white. Falls back to a static end card if no logo is supplied (`--no-outro` forces the static card).
- **Render (`render_news_video.py`)** — one FFmpeg `filter_complex`: Ken Burns `zoompan` on stills, `xfade` transitions, the animated lower-third chyron (accent stripe + headline + logo) that **rises/fades in, holds, then slides/fades out** before the tail, the karaoke subtitles, the animated outro, voice normalized to −16 LUFS and music ducked beneath it then dimmed to the end. H.264/AAC, 1920×1080, 30fps, faststart.
- **Manifest (`make_sources.py`)** — `SOURCES.md` with every asset's source/license/attribution; a prominent warning atop the file if anything is unverified (exit code 2).

For 9:16 or 1:1, adjust the `W`/`H` constants at the top of `render_news_video.py`.

## Outputs (delivered by the orchestrator)

- **`final.mp4`** — the finished video, 1920×1080.
- **`headline.txt`** — the final headline.
- **`voiceover.txt`** — the narration script as read.
- **`subs.srt`** — the subtitle file.
- **`production.json`** — the editable structured project file for re-renders (edit and re-run `render_news_video.py` alone).
- **`SOURCES.md`** — every visual + music asset with source URL, license, required attribution.
- **A rights warning** — surfaced in chat and atop `SOURCES.md` whenever any asset is unverified.

## Known limitations (tell the user when relevant)

- FFmpeg + Pillow motion graphics are clean and readable, not After Effects. "Animated chyron" here means a professional fade/rise lower-third, not particle work or 3D.
- Music ducking is dynamic (sidechain) but assumes voice and bed are separate files; it can't un-mix a track with music baked in.
- TTS quality depends on the provider: edge-tts is neural but unofficial; macOS `say` is offline and reliable but robotic. For anchor-grade delivery, wire in a cloud TTS the user has a key for (OpenAI/ElevenLabs/Azure).
- License metadata is only as good as the source API. Wikimedia is reliable; always keep the recorded URL so a human can re-check. CC BY / CC BY-SA require the captured attribution to be shown or credited on publish.
- This Skill composites; it does not fact-check. Accuracy is bounded by the source story you're given.
