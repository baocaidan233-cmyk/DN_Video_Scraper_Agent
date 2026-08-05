# video/ — vendored `news-motion-video` skill

Source: <https://github.com/nfsctech/short-news-video>, `news-motion-video/`
Vendored at commit `dd3cf24f99f0f3a53b421e946dec722af65bbf56`.

Only `news-motion-video/` was copied (scripts, templates, brand assets). The rest
of the upstream repo is ~40 MB of sample renders, which is why this is a plain
copy and not a submodule.

Two consumers share this directory:

- **Production** — `services/video_client.py` shells out to `scripts/make_news_video.py`
  when a post has no usable image. This is the path the dashboard switch controls.
- **Interactive** — `.claude/skills/news-motion-video/SKILL.md` for ad-hoc use from
  Claude Code, exactly as upstream intended.

## Host requirements

- `ffmpeg` + `ffprobe` (apt `ffmpeg`, 4.4.2 here; upstream needs 4.3+ for
  `overlay` / `zoompan` / `xfade` / `sidechaincompress`)
- Python: `Pillow`, `requests`, `edge-tts` (all in `requirements.txt`)
- Fonts: `text_render.py` falls back to `/usr/share/fonts/truetype/dejavu/`, present.

## Local patches

Every change is marked with a `LOCAL PATCH (dailynews-agent)` comment in the file.
Re-vendoring upstream means re-applying these.

| File | Patch | Why |
|---|---|---|
| `scripts/text_render.py` | `_scale(W, H)` / `_px()` applied to every layout constant; `chyron_geometry()` + `_subtitle_bottom()` anchor the lower third and subtitles to the **bottom** of the frame | Upstream hardcodes a 1920x1080 layout (`bar_top = 880`, font 54, …). At 960x720 the bar landed mid-frame and the text was ~2x oversized. |
| `scripts/render_news_video.py` | `width`/`height`/`fps`/`preset` read from `production.json`; Ken Burns source is scale-to-cover **cropped to the output aspect** before `zoompan`; `karaoke: false` disables the per-word path | `zoompan` stretches its window to `s=WxH` without preserving aspect, so a 16:9 still in a 4:3 render came out squashed. Karaoke costs one looped full-frame PNG input **and one `overlay`** per spoken word — ~70 extra inputs for a 30s read. |
| `scripts/generate_tts.py` | new `openai` provider; `auto` order is edge → openai → say | macOS `say` does not exist here. OpenAI is wired up but **unusable on the current key** (see below), so edge-tts is the live provider. |
| `scripts/make_news_video.py` | `--brand-dir`, `--width`, `--height`, `--preset`, `--karaoke/--no-karaoke`; narration written to `voice.mp3`; new fields threaded into `production.json` | Per-channel brands (`brand/dn`, `brand/ef`, `brand/<slug>`) instead of one global `brand/`. |
| `scripts/make_outro.py` | `--width`/`--height` with a module-level `S` scale and an `sc()` helper | Same 1920x1080 hardcoding. Note `sc()` is deliberately not named `px()` — `main()` already binds a local `px`. |
| `scripts/gen_still.py` | `--width`/`--height` | Cards are rendered at the renderer's 2400px supersample size **in the output aspect**, so they are neither upscaled nor side-cropped. |

`scripts/make_logo.py` is untouched and macOS-only (hardcoded `/System/Library/Fonts`).
It is not used — brand logos are committed files.

## Deliberate behavioural differences from upstream

- **`--article-url` is never passed.** Every asset is therefore Wikimedia-verified
  (credit line burned in automatically) or self-produced, so `rights_verified` is
  always true and nothing needs a human to read `SOURCES.md` before publishing.
  `VideoClient` aborts the render if any scene comes back unverified.
- **Karaoke subtitles are off** (`karaoke: false` in both brand files). Sentence-level
  cues cost ~5 overlays instead of ~70.
- **4:3, 960x720, x264 `veryfast`.** Measured on this box (2 vCPU / 3.9 GB):
  ~91s wall and ~814 MB peak RSS for a 25s video, of which ~77s is ffmpeg.

## OpenAI TTS is wired but not usable on the current key

`tts-1`, `tts-1-hd` and `gpt-4o-mini-tts` all return
`403 Project proj_qhIwKHSjcj1OP3jT42ewxBZX does not have access`. Speech models are
per-project opt-in in the OpenAI dashboard; embeddings and `gpt-4o-mini` work fine on
the same key. To switch back to OpenAI later: enable a speech model on the project,
then set `provider: "openai"` and an OpenAI voice name (e.g. `onyx`) in the brand file.

**edge-tts is an unofficial Microsoft endpoint.** If it starts failing, video
generation stops and each pipeline silently reverts to its normal no-image behavior
(DailyNews drops the article). Grep the logs for `[tts]` / `video generation failed`.
