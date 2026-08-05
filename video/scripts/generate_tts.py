#!/usr/bin/env python3
"""
generate_tts.py — synthesize English narration to an audio file.

Providers:
  1. edge-tts   (free Microsoft neural voices — needs `pip install edge-tts`
                 and network; what the dailynews-agent pipeline uses. UNOFFICIAL
                 endpoint: if it ever starts failing, video generation stops and
                 the pipeline silently falls back to its normal no-image
                 behavior, so watch the logs for `[tts]` errors.)
  2. openai     (official API, needs OPENAI_API_KEY *and* a project with a
                 speech model enabled — tts-1 / gpt-4o-mini-tts are per-project
                 opt-in. Emits no word-boundary timings, so subtitles are
                 sentence-level rather than karaoke.)
  3. macOS `say`(offline, always available on macOS; robotic but real speech)

All emit a real audio file whose true duration we measure with ffprobe, so
downstream subtitle timing is accurate regardless of provider.

LOCAL PATCH (dailynews-agent): the `openai` provider and the Linux-safe `auto`
order are additions; edge/say are upstream and kept for interactive use.

Usage:
    python3 generate_tts.py --text "..."  --out work/voice.mp3 [--voice NAME]
                            [--rate WPM] [--provider auto|openai|edge|say]

Prints the measured duration (seconds) to stdout.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Sensible default voices per provider.
EDGE_DEFAULT = "en-US-GuyNeural"      # calm male news read
SAY_DEFAULT = "Daniel"                 # British male, clear; fall back below
SAY_FALLBACKS = ["Daniel", "Alex", "Samantha", "Fred"]
OPENAI_DEFAULT = "onyx"                # deep, level male news read
OPENAI_MODEL = os.environ.get("VIDEO_TTS_MODEL", "gpt-4o-mini-tts")


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def have_edge() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def edge_tts_run(text: str, out: str, voice: str, rate_wpm: int) -> None:
    """Synthesize with edge-tts and write a sentence-timing sidecar.

    The free endpoint returns SentenceBoundary events (accurate per-sentence
    start+duration). We save those to <out>.timing.json so the renderer can
    interpolate word times for a karaoke-style subtitle highlight.
    """
    import asyncio
    import json
    import edge_tts
    # edge-tts takes a relative rate like "+10%"; map ~180 wpm baseline.
    pct = int(round((rate_wpm - 180) / 180 * 100))
    rate = f"{pct:+d}%"
    tmp = str(Path(out).with_suffix(".mp3"))
    sentences = []

    async def _go():
        comm = edge_tts.Communicate(text, voice, rate=rate)
        with open(tmp, "wb") as f:
            async for ch in comm.stream():
                t = ch.get("type")
                if t == "audio":
                    f.write(ch["data"])
                elif t in ("SentenceBoundary", "WordBoundary"):
                    sentences.append({
                        "text": ch["text"],
                        "start": ch["offset"] / 1e7,
                        "dur": ch["duration"] / 1e7,
                        "level": "word" if t == "WordBoundary" else "sentence",
                    })

    asyncio.run(_go())
    if tmp != out:
        subprocess.run(["ffmpeg", "-y", "-i", tmp, out],
                       check=True, capture_output=True)
        Path(tmp).unlink(missing_ok=True)
    if sentences:
        Path(out + ".timing.json").write_text(json.dumps(sentences, indent=2))
        print(f"[tts] captured {len(sentences)} {sentences[0]['level']} "
              f"boundaries -> {out}.timing.json", file=sys.stderr)


def have_openai() -> bool:
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # noqa: F401
        return True
    except Exception:
        return False


def openai_run(text: str, out: str, voice: str, rate_wpm: int) -> None:
    """Synthesize with the OpenAI speech API.

    No word-boundary events are returned, so no timing sidecar is written and
    the renderer falls back to sentence-level subtitle cues — which is exactly
    what the unattended pipeline wants (karaoke costs one ffmpeg input per word).
    """
    from openai import OpenAI

    # brand.json may still carry an edge-tts voice name; don't hand that to the
    # OpenAI API, it only knows its own short names (onyx, alloy, ash, ...).
    if "neural" in voice.lower() or "-" in voice:
        print(f"[tts] voice {voice!r} is not an OpenAI voice; using "
              f"{OPENAI_DEFAULT}", file=sys.stderr)
        voice = OPENAI_DEFAULT

    speed = max(0.5, min(1.5, rate_wpm / 175.0))
    kwargs = dict(model=OPENAI_MODEL, voice=voice, input=text,
                  response_format="mp3")
    client = OpenAI()
    try:
        resp = client.audio.speech.create(speed=speed, **kwargs)
    except Exception as e:
        # Not every speech model accepts `speed`; retry once without it rather
        # than failing the whole render over a pacing nicety.
        if "speed" not in str(e):
            raise
        print(f"[tts] model rejected speed={speed:.2f} ({e}); retrying at "
              f"default pace", file=sys.stderr)
        resp = client.audio.speech.create(**kwargs)

    Path(out).write_bytes(resp.read())
    print(f"[tts] provider=openai model={OPENAI_MODEL} voice={voice}",
          file=sys.stderr)


def pick_say_voice(requested: str | None) -> str:
    installed = subprocess.run(["say", "-v", "?"], capture_output=True,
                               text=True).stdout
    names = {line.split()[0] for line in installed.splitlines() if line.strip()}
    if requested and requested in names:
        return requested
    for v in SAY_FALLBACKS:
        if v in names:
            return v
    # last resort: first available voice
    return next(iter(names)) if names else SAY_DEFAULT


def say_run(text: str, out: str, voice: str | None, rate_wpm: int) -> None:
    v = pick_say_voice(voice)
    aiff = str(Path(out).with_suffix(".aiff"))
    subprocess.run(["say", "-v", v, "-r", str(rate_wpm), "-o", aiff, text],
                   check=True)
    subprocess.run(["ffmpeg", "-y", "-i", aiff, out],
                   check=True, capture_output=True)
    Path(aiff).unlink(missing_ok=True)
    print(f"[tts] provider=say voice={v} rate={rate_wpm}wpm", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", type=int, default=175, help="words per minute")
    ap.add_argument("--provider", choices=["auto", "openai", "edge", "say"],
                    default="auto")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    provider = args.provider
    if provider == "auto":
        if have_edge():
            provider = "edge"
        elif have_openai():
            provider = "openai"
        else:
            provider = "say"

    if provider == "openai":
        if not have_openai():
            sys.exit("[tts] openai provider needs OPENAI_API_KEY and the "
                     "`openai` package")
        openai_run(args.text, args.out, args.voice or OPENAI_DEFAULT, args.rate)
    elif provider == "edge":
        if not have_edge():
            sys.exit("[tts] edge-tts not installed (pip install edge-tts)")
        try:
            edge_tts_run(args.text, args.out, args.voice or EDGE_DEFAULT,
                         args.rate)
            print(f"[tts] provider=edge voice={args.voice or EDGE_DEFAULT}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[tts] edge-tts failed ({e}); falling back to say",
                  file=sys.stderr)
            if not shutil.which("say"):
                raise
            say_run(args.text, args.out, args.voice, args.rate)
    else:
        if not shutil.which("say"):
            sys.exit("[tts] macOS `say` not available and edge-tts not "
                     "installed. Install one: `pip install edge-tts`.")
        say_run(args.text, args.out, args.voice, args.rate)

    dur = probe_duration(args.out)
    print(f"[tts] wrote {args.out} ({dur:.2f}s)", file=sys.stderr)
    print(f"{dur:.3f}")


if __name__ == "__main__":
    main()
