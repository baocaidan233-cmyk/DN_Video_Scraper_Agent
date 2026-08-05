# news-motion-video

Turn a short written news story into a polished 20–30s motion-news video:
open-license imagery with Ken Burns motion, an animated lower-third chyron,
English narration, a music bed ducked under the voice, time-synced subtitles,
and a branded FOLLOW end card. Output is a 1920×1080 H.264/AAC MP4 plus the
headline, script, SRT, an editable `production.json`, and a licensing manifest.

## Requirements

- **FFmpeg** (any build with `overlay`, `zoompan`, `xfade`, `sidechaincompress`
  — i.e. 4.3+. Validated on 8.1). No `drawtext`/libass required: all text is
  rendered to PNG with Pillow and composited via `overlay`.
- **Python 3** + **Pillow** — `pip install Pillow`
- **TTS**: macOS `say` (offline, built-in) **or** `pip install edge-tts`
  (free neural voices, needs network). Auto-detected.

## Quickstart

```bash
python3 scripts/make_news_video.py \
  --headline "Dutch open major new flood barrier" \
  --script "Dutch water authorities today opened a major new storm surge barrier on the IJssel river, after three years of construction. Officials say it can shield more than two hundred thousand residents from severe winter flooding. The four hundred million euro project is part of the Netherlands' national climate adaptation program." \
  --article-url "https://outlet.com/the-source-story" \
  --query "flood barrier river netherlands ijssel" \
  --fallback-labels "Flood defenses,Climate adaptation,IJssel river" \
  --workdir work --out-dir deliverables
```

Brand defaults (logo, handle, voice, colors, outro, min photos) load automatically from `brand/brand.json` — you don't pass them. `--article-url` pulls the source story's own photos **first**, then `--query` supplements with open-license stills to a **minimum of 5 photos** (source-article photos are flagged rights-unverified in `SOURCES.md`).

Deliverables land in `deliverables/`: `final.mp4`, `headline.txt`,
`voiceover.txt`, `subs.srt`, `production.json`, `SOURCES.md`.

## Re-rendering after edits

`production.json` is the source of truth. Tweak headline wording, scene order,
durations, transitions, or the accent color, then re-render just the video:

```bash
python3 scripts/render_news_video.py work/production.json
```

## Scripts

| Script | Role |
|--------|------|
| `make_news_video.py` | Orchestrator — runs steps 3–8 end to end |
| `fetch_media.py` | Open-license media search (Wikimedia primary, Openverse secondary) + license capture |
| `gen_still.py` | Self-produced graphic card fallback (zero licensing risk) |
| `generate_tts.py` | Narration via edge-tts or macOS `say`; measures true duration |
| `build_srt.py` | Time-synced subtitles from script + measured duration |
| `make_music_bed.py` | Self-produced ambient bed — audible (~−20 LUFS), auto-length, gradual fade-out |
| `make_outro.py` | Animated ~3s outro: logo rolls in (motion blur) → settles (red glow) → FOLLOW fades up |
| `make_logo.py` | Placeholder DAILY NEWS logo generator |
| `text_render.py` | Pillow renderers for chyron, end card, subtitles (incl. karaoke), stills |
| `render_news_video.py` | FFmpeg assembly: motion, transitions, chyron in/out, karaoke subs, outro, ducked+fading audio |
| `make_sources.py` | `SOURCES.md` licensing manifest + rights warning |

## Notes & limits

- Steps 1–2 (condense story → headline + narration) are done by the operator
  and passed in via `--headline` / `--script`. Everything after is mechanical.
- **Licensing:** CC BY / CC BY-SA assets require the captured attribution to be
  shown/credited on publish. Unverifiable assets are discarded, never used
  silently; if you force one in (`rights_verified: false`), `SOURCES.md` warns.
- **Openverse** currently 403s unauthenticated API calls; Wikimedia is the
  reliable source. Add a licensed stock provider to `fetch_media.py` if you have
  one (Pexels/Pixabay API, etc.).
- macOS `say` is reliable but robotic; wire in a cloud TTS for anchor-grade VO.
