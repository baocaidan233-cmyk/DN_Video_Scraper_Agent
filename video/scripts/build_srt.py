#!/usr/bin/env python3
"""
build_srt.py — turn narration text + measured audio duration into a synced SRT.

macOS `say` (and basic TTS) don't return word timings, so we split the script
into sentence-level cues and allocate each cue a slice of the real audio
duration proportional to its word count. This yields subtitles that track the
read closely without pretending to have per-word timestamps we don't have.

Rules: <= ~42 chars/line, <= 2 lines/cue, no cue shorter than MIN_CUE seconds,
sentences that are too long are split on clause boundaries.

Usage:
    python3 build_srt.py --text "..." --duration 24.6 --out work/subs.srt
"""
import argparse
import re
from pathlib import Path

MAX_LINE = 44
MAX_CHARS_PER_CUE = 90      # ~2 lines
MIN_WORDS = 4               # merge shorter fragments into a neighbor
MIN_CUE = 1.2               # seconds
LEAD_IN = 0.0              # subtitles start with the audio


def split_sentences(text: str):
    text = re.sub(r"\s+", " ", text.strip())
    # split after . ? ! keeping delimiter
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def split_long(sentence: str):
    """Break an over-long sentence on commas/clauses into cue-sized chunks."""
    if len(sentence) <= MAX_CHARS_PER_CUE:
        return [sentence]
    chunks, cur = [], ""
    for token in re.split(r"(?<=[,;:])\s+", sentence):
        if cur and len(cur) + 1 + len(token) > MAX_CHARS_PER_CUE:
            chunks.append(cur.strip())
            cur = token
        else:
            cur = f"{cur} {token}".strip()
    if cur:
        chunks.append(cur.strip())
    # if a single clause is still too long, hard-wrap on words
    final = []
    for c in chunks:
        while len(c) > MAX_CHARS_PER_CUE:
            cut = c.rfind(" ", 0, MAX_CHARS_PER_CUE)
            cut = cut if cut > 0 else MAX_CHARS_PER_CUE
            final.append(c[:cut].strip())
            c = c[cut:].strip()
        if c:
            final.append(c)
    return final


def merge_short(chunks):
    """Fold tiny orphan fragments (e.g. 'aircraft,') into an adjacent cue."""
    out = []
    for c in chunks:
        if (out and len(c.split()) < MIN_WORDS
                and len(out[-1]) + 1 + len(c) <= MAX_CHARS_PER_CUE):
            out[-1] = f"{out[-1]} {c}"
        elif (out and len(out[-1].split()) < MIN_WORDS
              and len(out[-1]) + 1 + len(c) <= MAX_CHARS_PER_CUE):
            out[-1] = f"{out[-1]} {c}"
        else:
            out.append(c)
    return out


def wrap_two_lines(text: str) -> str:
    if len(text) <= MAX_LINE:
        return text
    cut = text.rfind(" ", 0, MAX_LINE)
    if cut <= 0:
        return text
    return text[:cut].strip() + "\n" + text[cut:].strip()


def ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build(text: str, duration: float):
    cues = []
    for sent in split_sentences(text):
        cues.extend(split_long(sent))
    cues = merge_short(cues)
    if not cues:
        return []

    weights = [max(len(c.split()), 1) for c in cues]
    total_w = sum(weights)
    span = max(duration - LEAD_IN, 0.5)

    out, t = [], LEAD_IN
    for i, (cue, w) in enumerate(zip(cues, weights)):
        share = span * (w / total_w)
        start = t
        end = t + share
        # enforce a minimum readable duration where possible
        if end - start < MIN_CUE:
            end = start + MIN_CUE
        t = end
        out.append((start, end, wrap_two_lines(cue)))

    # clamp the final cue to the audio end and rescale if we overshot
    if out and out[-1][1] > duration:
        scale = duration / out[-1][1]
        out = [(s * scale, e * scale, txt) for (s, e, txt) in out]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cues = build(args.text, args.duration)
    lines = []
    for i, (start, end, txt) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{ts(start)} --> {ts(end)}")
        lines.append(txt)
        lines.append("")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"[srt] wrote {args.out} ({len(cues)} cues over {args.duration:.1f}s)")


if __name__ == "__main__":
    main()
