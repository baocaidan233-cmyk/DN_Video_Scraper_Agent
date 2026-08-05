#!/usr/bin/env python3
"""
render_news_video.py — assemble a 20-30s motion-news clip with FFmpeg.

Reads a production.json (see templates/production.schema.json) and builds one
FFmpeg filter_complex: Ken Burns zoompan on stills, xfade transitions, an
animated lower-third chyron, a branded end card, time-synced subtitles, and a
music bed ducked under the voice with sidechaincompress.

All text (chyron, end card, subtitles) is rendered to PNG with Pillow
(text_render.py) and composited via `overlay`, because many FFmpeg builds ship
without drawtext/libass. Validated on FFmpeg 8.1 (Homebrew, no freetype/ass).

Usage:
    python3 render_news_video.py production.json
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import text_render as T

# LOCAL PATCH (dailynews-agent): frame size, fps and x264 preset come from
# production.json instead of being module constants, so the service can publish
# 960x720 (4:3) without editing code. Defaults match what the pipeline renders.
DEF_W, DEF_H, DEF_FPS = 960, 720, 30
DEF_PRESET = "veryfast"
SUPERSAMPLE_W = 2400          # Ken Burns source width before zoompan
_MAX_CREDIT_NAMES = 3         # burned-in credit line; the rest live in SOURCES.md
XFADE = 0.5
CHYRON_IN = 0.4
CHYRON_FADE = 0.5
CHYRON_OUT_FADE = 0.6
HL_COLOR = (255, 215, 0)   # karaoke highlight (yellow/gold)


def srt_time(s: str) -> float:
    h, m, rest = s.split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def parse_srt(path: str):
    cues = []
    blocks = re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8").strip())
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        ti = 0
        if "-->" not in lines[0]:
            ti = 1
        if ti >= len(lines) or "-->" not in lines[ti]:
            continue
        start, end = [x.strip() for x in lines[ti].split("-->")]
        text = "\n".join(lines[ti + 1:])
        cues.append((srt_time(start), srt_time(end), text))
    return cues


def auto_credit(scenes):
    """Build a compact credit line from assets whose license needs attribution.

    Only CC-licensed assets require a credit; public-domain and self-produced
    assets are skipped. Returns '' if nothing needs crediting.
    """
    names, licenses = [], set()
    for s in scenes:
        lic = (s.get("license") or "").lower()
        if "cc" not in lic:                     # PD / N/A / self-produced -> skip
            continue
        attr = (s.get("attribution") or "").strip()
        if attr and attr not in names and attr.lower() != "—":
            names.append(attr)
        if "sa" in lic:
            licenses.add("CC BY-SA")
        elif "by" in lic:
            licenses.add("CC BY")
    if not names:
        return ""
    lic_str = ", ".join(sorted(licenses)) if licenses else "CC"
    # LOCAL PATCH (dailynews-agent): cap the name list. Five assets from five
    # different authors produced a credit line wider than a 960px frame, and it
    # ran off the edge. The full attribution always remains in SOURCES.md.
    shown, extra = names[:_MAX_CREDIT_NAMES], len(names) - _MAX_CREDIT_NAMES
    tail = f" +{extra} more" if extra > 0 else ""
    return f"Images: {', '.join(shown)}{tail} / Wikimedia Commons ({lic_str})"


def words_from_timing(timing_path, content_end):
    """Turn TTS boundary events into per-word {text,start,end}.

    Word-level events are used directly; sentence-level events are split into
    words with time allocated proportional to word length, anchored to the
    sentence's real start+duration. End of each word = start of the next, so
    the highlight advances with no gaps.
    """
    import re as _re
    events = json.loads(Path(timing_path).read_text())
    words = []
    for ev in events:
        if ev.get("level") == "word":
            words.append({"text": ev["text"], "start": ev["start"],
                          "_dur": ev["dur"]})
            continue
        toks = [t for t in _re.split(r"\s+", ev["text"].strip()) if t]
        if not toks:
            continue
        weights = [len(t) + 1 for t in toks]
        tot = sum(weights)
        t = ev["start"]
        for tok, wgt in zip(toks, weights):
            share = ev["dur"] * (wgt / tot)
            words.append({"text": tok, "start": t, "_dur": share})
            t += share
    words.sort(key=lambda w: w["start"])
    for i, w in enumerate(words):
        nxt = words[i + 1]["start"] if i + 1 < len(words) else content_end
        w["end"] = max(min(nxt, content_end), w["start"] + 0.12)
    return [{"text": w["text"], "start": w["start"], "end": w["end"]}
            for w in words if w["start"] < content_end]


def build(prod: dict):
    W = int(prod.get("width", DEF_W))
    H = int(prod.get("height", DEF_H))
    FPS = int(prod.get("fps", DEF_FPS))
    preset = prod.get("preset", DEF_PRESET)
    # Supersampled Ken Burns source, cropped to the OUTPUT aspect ratio first.
    # zoompan stretches its window to `s=WxH` without preserving aspect, so a
    # 16:9 still fed straight into a 4:3 output would come out squashed.
    ss_w = SUPERSAMPLE_W
    ss_h = int(round(SUPERSAMPLE_W * H / W)) // 2 * 2

    scenes = prod["scenes"]
    chyron = prod.get("chyron", {})
    audio = prod.get("audio", {})
    subs = prod.get("subtitle_file")
    out = prod["output_path"]
    accent = chyron.get("accent_color", "0xE60023")

    tmp = Path(out).parent / ".rtmp"
    tmp.mkdir(parents=True, exist_ok=True)

    inputs, filters, labels = [], [], []
    idx = 0

    # ---- scenes -> normalized 1920x1080@30 segments ----
    for s in scenes:
        dur = float(s["duration"])
        stype = s["type"]

        if stype == "endcard":
            png = str(tmp / f"endcard_{idx}.png")
            T.end_card(png, s.get("handle", prod.get("handle", "@DailyNews")),
                       W, H, s.get("bg_color", "0x0d1117"),
                       s.get("accent_color", accent),
                       logo=chyron.get("logo"))
            inputs += ["-loop", "1", "-t", f"{dur}", "-i", png]
            filters.append(
                f"[{idx}:v]scale={W}:{H},setsar=1,fps={FPS},format=yuv420p,"
                f"setpts=PTS-STARTPTS[s{idx}]"
            )
        elif stype == "image":
            inputs += ["-loop", "1", "-t", f"{dur}", "-i", s["path"]]
            frames = int(round(dur * FPS))
            if s.get("kenburns", "in") == "out":
                z = f"z='1.4-on*{0.4/frames:.6f}'"
            else:
                z = f"z='min(zoom+{0.4/frames:.6f},1.4)'"
            filters.append(
                f"[{idx}:v]scale={ss_w}:{ss_h}:force_original_aspect_ratio=increase,"
                f"crop={ss_w}:{ss_h},zoompan={z}:d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},"
                f"setsar=1,format=yuv420p,setpts=PTS-STARTPTS[s{idx}]"
            )
        elif stype == "outro":
            # a pre-rendered animated outro clip (see make_outro.py)
            inputs += ["-i", s["path"]]
            filters.append(
                f"[{idx}:v]trim=start=0:end={dur},setpts=PTS-STARTPTS,"
                f"scale={W}:{H},fps={FPS},format=yuv420p,setsar=1[s{idx}]"
            )
        else:  # video clip
            start = s.get("in", 0)
            end = start + dur
            inputs += ["-i", s["path"]]
            filters.append(
                f"[{idx}:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},"
                f"format=yuv420p,setsar=1[s{idx}]"
            )
        labels.append((f"s{idx}", dur))
        idx += 1

    # ---- xfade chain ----
    cur, acc = labels[0][0], labels[0][1]
    for i in range(1, len(labels)):
        nxt, ndur = labels[i]
        offset = max(acc - XFADE, 0)
        trans = scenes[i].get("transition", "fade")
        out_lbl = f"xf{i}"
        filters.append(
            f"[{cur}][{nxt}]xfade=transition={trans}:duration={XFADE}:"
            f"offset={offset:.3f}[{out_lbl}]"
        )
        cur = out_lbl
        acc = acc + ndur - XFADE
    total = acc
    filters.append(f"[{cur}]format=yuv420p[base]")
    stage = "base"

    tail_dur = next((float(s["duration"]) for s in scenes
                     if s["type"] in ("endcard", "outro")), 0.0)
    content_end = total - tail_dur

    # ---- animated chyron: rises+fades IN, holds, then slides+fades OUT ----
    if chyron.get("headline"):
        png = str(tmp / "chyron.png")
        T.lower_third(png, chyron["headline"], logo=chyron.get("logo"),
                      W=W, H=H, accent=accent)
        inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", png]
        li = idx
        idx += 1
        out_start = max(content_end - CHYRON_OUT_FADE - 0.2,
                        CHYRON_IN + CHYRON_FADE + 0.5)
        filters.append(
            f"[{li}:v]format=rgba,"
            f"fade=t=in:st={CHYRON_IN}:d={CHYRON_FADE}:alpha=1,"
            f"fade=t=out:st={out_start:.3f}:d={CHYRON_OUT_FADE}:alpha=1[chy]"
        )
        # y: rise 40->0 on entry; then drop 0->200 (off-screen) on exit
        y = (f"'40*(1-min(max((t-{CHYRON_IN})/{CHYRON_FADE},0),1))"
             f"+200*min(max((t-{out_start:.3f})/{CHYRON_OUT_FADE},0),1)'")
        filters.append(f"[{stage}][chy]overlay=x=0:y={y}:format=auto[vch]")
        stage = "vch"

    # ---- subtitles ----
    has_chy = bool(chyron.get("headline"))
    timing = audio.get("timing")
    # Karaoke adds ONE looped full-frame PNG input and ONE overlay per spoken
    # word — ~70 extra inputs and a 70-deep alpha chain for a 30s read. Far too
    # expensive for the unattended pipeline, so it can be switched off outright.
    if prod.get("karaoke") is False:
        timing = None
    if timing and Path(timing).exists():
        # karaoke: per-word highlight driven by real TTS timings
        words = words_from_timing(timing, content_end)
        frames = T.karaoke_frames(str(tmp / "kara"), "w", words, W, H,
                                  has_chyron=has_chy, hi_color=HL_COLOR)
        for fi, fr in enumerate(frames):
            inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", fr["png"]]
            si = idx
            idx += 1
            out_lbl = f"vk{fi}"
            filters.append(
                f"[{stage}][{si}:v]overlay=x=0:y=0:"
                f"enable='between(t,{fr['start']:.3f},{fr['end']:.3f})'"
                f"[{out_lbl}]"
            )
            stage = out_lbl
    elif subs and Path(subs).exists():
        # fallback: static sentence-level cues
        for ci, (cs, ce, ctext) in enumerate(parse_srt(subs)):
            png = str(tmp / f"sub_{ci:03d}.png")
            T.subtitle_png(png, ctext, W, H, has_chyron=has_chy, accent=accent)
            inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", png]
            si = idx
            idx += 1
            out_lbl = f"vs{ci}"
            filters.append(
                f"[{stage}][{si}:v]overlay=x=0:y=0:"
                f"enable='between(t,{cs:.3f},{ce:.3f})'[{out_lbl}]"
            )
            stage = out_lbl

    # ---- burned attribution credit line (tiny, top-right, content only) ----
    if prod.get("burn_credits", True):
        credit = prod.get("credits_text") or auto_credit(scenes)
        if credit:
            png = str(tmp / "credit.png")
            T.credit_png(png, credit, W, H)
            inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", png]
            ci = idx
            idx += 1
            c_out = max(content_end - 0.5, 0.6)
            filters.append(
                f"[{ci}:v]format=rgba,fade=t=in:st=0.4:d=0.5:alpha=1,"
                f"fade=t=out:st={c_out:.3f}:d=0.5:alpha=1[crd]"
            )
            filters.append(
                f"[{stage}][crd]overlay=x=0:y=0:"
                f"enable='between(t,0,{content_end:.3f})'[vcrd]"
            )
            stage = "vcrd"

    filters.append(f"[{stage}]format=yuv420p[vid]")

    # ---- audio: voice + ducked bgm ----
    voice, bgm = audio.get("voice"), audio.get("bgm")
    a_out = None
    if voice:
        inputs += ["-i", voice]
        v_idx = idx
        idx += 1
        filters.append(
            f"[{v_idx}:a]loudnorm=I=-16:TP=-1.5:LRA=11,apad,asplit=2[vc1][vc2]"
        )
        if bgm:
            inputs += ["-stream_loop", "-1", "-i", bgm]
            b_idx = idx
            idx += 1
            # bed is audible (it swells when no one is speaking) but ducks under
            # the voice; a final fade dims it as the video ends.
            filters.append(f"[{b_idx}:a]volume=0.85[bg]")
            filters.append(
                "[bg][vc2]sidechaincompress=threshold=0.03:ratio=8:"
                "attack=20:release=350[duck]"
            )
            filters.append(
                "[vc1][duck]amix=inputs=2:duration=first:"
                "dropout_transition=0[amx]"
            )
            filters.append(
                f"[amx]afade=t=out:st={max(total - 1.6, 0):.3f}:d=1.6[aout]"
            )
        else:
            filters.append("[vc1]acopy[aout]")
        a_out = "aout"

    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", ";".join(filters),
           "-map", "[vid]"]
    if a_out:
        cmd += ["-map", f"[{a_out}]"]
    cmd += ["-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", preset, "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-s", f"{W}x{H}",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", out]
    return cmd, total, tmp


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: render_news_video.py production.json")
    prod = json.loads(Path(sys.argv[1]).read_text())
    cmd, total, tmp = build(prod)
    print(f"[render] target duration ~{total:.1f}s", file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit("[render] ffmpeg failed")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[render] wrote {prod['output_path']}", file=sys.stderr)


if __name__ == "__main__":
    main()
