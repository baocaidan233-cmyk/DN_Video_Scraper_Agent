#!/usr/bin/env python3
"""
make_news_video.py — orchestrate the full news-short pipeline.

Steps 1-2 (condense story, write headline + narration) are done by the model
and passed IN as --headline / --script. This script performs the mechanical
steps 3-8: source media (or self-produced fallback), synth narration, build
synced subtitles, synth a music bed, assemble production.json, render, and
write all deliverables.

Example:
    python3 make_news_video.py \
        --headline "City council approves flood defenses" \
        --script "The city council today approved ..." \
        --handle "@DailyNews" \
        --logo work/logo.png \
        --query "flood barrier river city" \
        --workdir work --out-dir deliverables

Deliverables written to --out-dir:
    final.mp4, headline.txt, voiceover.txt, subs.srt, production.json, SOURCES.md
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
PY = sys.executable


def load_brand(brand_dir=None):
    """Load persistent brand defaults (brand.json) if present.

    Lets the operator run without re-supplying the logo, handle, voice, etc.
    every time — the brand is applied automatically. CLI flags override.

    LOCAL PATCH (dailynews-agent): `brand_dir` selects a per-channel brand
    (video/brand/dn, video/brand/ef, video/brand/<slug>) instead of the single
    upstream brand/ directory. Relative asset paths resolve inside it.
    """
    base = Path(brand_dir) if brand_dir else PKG / "brand"
    bf = base / "brand.json"
    if not bf.exists():
        return {}
    b = json.loads(bf.read_text())
    for key in ("logo", "outro"):                  # resolve relative paths
        v = b.get(key)
        if v and not Path(v).is_absolute():
            b[key] = str((base / v).resolve())
    return b

MIN_SCENE = 2.8        # each content scene at least this long (Ken Burns room)
MAX_SCENES = 8
MIN_PHOTOS = 5         # every video uses at least this many photos (brand rule)
END_CARD = 3.0         # branded tail (static end card fallback)
OUTRO_DUR = 3.0        # animated outro clip length
XFADE = 0.5


def run(cmd, capture=False):
    print("  $ " + " ".join(str(c) for c in cmd[:6]) + (" ..." if len(cmd) > 6 else ""),
          file=sys.stderr)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode not in (0, 2):   # 2 = sources.py "unverified" signal
        if capture:
            sys.stderr.write(r.stdout or "")
            sys.stderr.write(r.stderr or "")
        raise SystemExit(f"step failed ({r.returncode}): {cmd[1] if len(cmd)>1 else cmd}")
    return r


def tts(text, out, voice, provider, rate):
    cmd = [PY, str(HERE / "generate_tts.py"), "--text", text, "--out", out,
           "--rate", str(rate), "--provider", provider]
    if voice:
        cmd += ["--voice", voice]
    r = run(cmd, capture=True)
    sys.stderr.write(r.stderr or "")
    dur = float((r.stdout or "0").strip().splitlines()[-1])
    return dur


def fetch(queries, n, out_dir):
    cmd = [PY, str(HERE / "fetch_media.py"), "--n", str(n), "--out", out_dir]
    for q in queries:
        cmd += ["--query", q]
    r = run(cmd, capture=True)
    sys.stderr.write(r.stderr or "")
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def fetch_article(url, n, out_dir):
    """Pull the photos that ship with the source article (rights unverified)."""
    cmd = [PY, str(HERE / "fetch_article_media.py"), "--url", url,
           "--n", str(n), "--out", out_dir]
    r = run(cmd, capture=True)
    sys.stderr.write(r.stderr or "")
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def gen_still(out, label, width, height):
    run([PY, str(HERE / "gen_still.py"), "--out", out, "--label", label,
         "--width", str(width), "--height", str(height)], capture=True)
    return {"type": "image", "path": out, "source": "self-produced",
            "license": "N/A (generated)", "attribution": "—",
            "provider": "self-produced", "rights_verified": True}


def gen_outro(logo, out, accent, width, height):
    run([PY, str(HERE / "make_outro.py"), "--logo", logo, "--out", out,
         "--accent", accent, "--duration", str(OUTRO_DUR),
         "--width", str(width), "--height", str(height)], capture=True)
    return out


def content_durations(voice_dur, n):
    """Distribute voice duration across n content scenes (with xfade overlap)."""
    # sum_content - XFADE*(n-1) == voice_dur  ->  sum = voice_dur + XFADE*(n-1)
    total = voice_dur + XFADE * (n - 1)
    each = max(total / n, MIN_SCENE)
    return [round(each, 2)] * n


def main():
    # --brand-dir must be resolved before the main parser is built, because the
    # brand it points at supplies the defaults for most other flags.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--brand-dir", default=None,
                     help="per-channel brand directory (default: video/brand)")
    brand = load_brand(pre.parse_known_args()[0].brand_dir)

    ap = argparse.ArgumentParser(parents=[pre])
    ap.add_argument("--headline", required=True)
    ap.add_argument("--script", required=True)
    ap.add_argument("--handle", default=brand.get("handle", "@DailyNews"))
    ap.add_argument("--logo", default=brand.get("logo"))
    ap.add_argument("--query", default=None, action="append",
                    help="media search subject; repeat for several. Each is searched "
                         "SEPARATELY — Commons ANDs all terms, so one long phrase "
                         "matches nothing while single entity names match plenty.")
    ap.add_argument("--article-url", default=None,
                    help="URL of the source story; its photos are used FIRST")
    ap.add_argument("--min-photos", type=int,
                    default=brand.get("min_photos", MIN_PHOTOS),
                    help="minimum number of photos per video")
    ap.add_argument("--fallback-labels", default="",
                    help="comma-separated labels for self-produced stills")
    ap.add_argument("--story", default="", help="original story, for records")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--out-dir", default="deliverables")
    ap.add_argument("--voice", default=brand.get("voice"))
    ap.add_argument("--provider", default=brand.get("provider", "auto"),
                    choices=["auto", "edge", "say"])
    ap.add_argument("--rate", type=int, default=brand.get("rate", 175))
    ap.add_argument("--music-key", default="a", choices=["a", "c", "d"])
    ap.add_argument("--music-mood", default=brand.get("music_mood", "calm"),
                    choices=["calm", "tense"])
    ap.add_argument("--music-fadeout", type=float,
                    default=brand.get("music_fadeout", 2.5),
                    help="seconds of gradual music dim at the end")
    ap.add_argument("--accent", default=brand.get("accent", "0xE60023"))
    ap.add_argument("--outro-accent",
                    default=brand.get("outro_accent", "0xFF1E1E"),
                    help="accent for the animated outro (bright red)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip network media; use self-produced graphics")
    ap.add_argument("--no-outro", action="store_true",
                    help="use a static end card instead of the animated outro")
    ap.add_argument("--no-credits", action="store_true",
                    help="do not burn the attribution credit line")
    ap.add_argument("--n-scenes", type=int, default=4)
    ap.add_argument("--width", type=int, default=brand.get("width", 960))
    ap.add_argument("--height", type=int, default=brand.get("height", 720))
    ap.add_argument("--preset", default=brand.get("preset", "veryfast"),
                    help="x264 preset")
    ap.add_argument("--karaoke", dest="karaoke", action="store_true",
                    default=brand.get("karaoke", False),
                    help="per-word subtitle highlight (expensive; needs edge-tts)")
    ap.add_argument("--no-karaoke", dest="karaoke", action="store_false")
    args = ap.parse_args()

    work = Path(args.workdir)
    assets = work / "assets"
    out = Path(args.out_dir)
    for d in (work, assets, out):
        d.mkdir(parents=True, exist_ok=True)

    # ---- 5. narration ----
    print("[1/7] narration (TTS)", file=sys.stderr)
    voice_path = str(work / "voice.mp3")
    vdur = tts(args.script, voice_path, args.voice, args.provider, args.rate)
    timing_path = voice_path + ".timing.json"        # karaoke timings (edge-tts)
    has_timing = Path(timing_path).exists()

    # ---- 6. subtitles ----
    print("[2/7] subtitles", file=sys.stderr)
    srt_path = str(work / "subs.srt")
    run([PY, str(HERE / "build_srt.py"), "--text", args.script,
         "--duration", f"{vdur:.3f}", "--out", srt_path], capture=True)

    # ---- 3. visuals: source-article photos FIRST, then supplement ----
    print("[3/7] visuals", file=sys.stderr)
    target = min(max(args.min_photos, args.n_scenes), MAX_SCENES)
    scene_assets = []

    # (a) photos that ship with the source article (rights unverified -> warned)
    if args.article_url:
        src = fetch_article(args.article_url, target, str(assets / "source"))
        scene_assets += src
        print(f"    source-article photos: {len(src)}", file=sys.stderr)

    # (b) supplement with open-license stills relevant to the story
    if len(scene_assets) < target and args.query and not args.no_fetch:
        need = target - len(scene_assets)
        sup = fetch(args.query, need, str(assets))
        scene_assets += sup
        print(f"    licensed supplements: {len(sup)}", file=sys.stderr)

    # (c) last resort: self-produced graphic cards, so we always hit the minimum
    labels = [s.strip() for s in args.fallback_labels.split(",") if s.strip()]
    if not labels:
        labels = [args.headline]
    li = 0
    # Cards are rendered at the renderer's Ken Burns supersample size, in the
    # output aspect ratio, so they are neither upscaled nor side-cropped.
    card_w = 2400
    card_h = int(round(card_w * args.height / args.width)) // 2 * 2
    while len(scene_assets) < args.min_photos:
        lbl = labels[li % len(labels)]
        scene_assets.append(
            gen_still(str(assets / f"card{li+1}.png"), lbl, card_w, card_h))
        li += 1
    scene_assets = scene_assets[:target]
    unverified = sum(1 for s in scene_assets if not s.get("rights_verified"))
    print(f"    total photos: {len(scene_assets)} ({unverified} rights-unverified)",
          file=sys.stderr)

    # ---- build content scenes + branded tail (animated outro or end card) ----
    durs = content_durations(vdur, len(scene_assets))
    kb = ["in", "out"]
    trans = ["fade", "dissolve", "slideleft", "smoothleft"]
    scenes = []
    for i, a in enumerate(scene_assets):
        sc = dict(a)
        sc["duration"] = durs[i]
        sc["kenburns"] = kb[i % 2]
        if i > 0:
            sc["transition"] = trans[i % len(trans)]
        scenes.append(sc)

    have_logo = bool(args.logo and Path(args.logo).exists())
    brand_outro = brand.get("outro")
    if have_logo and not args.no_outro:
        if brand_outro and Path(brand_outro).exists():
            print("[4/7] outro (prebuilt brand asset)", file=sys.stderr)
            outro_path = brand_outro          # reuse the saved brand outro
        else:
            print("[4/7] animated outro", file=sys.stderr)
            outro_path = str(work / "outro.mp4")
            gen_outro(args.logo, outro_path, args.outro_accent,
                      args.width, args.height)
        tail = {"type": "outro", "path": outro_path, "duration": OUTRO_DUR,
                "transition": "fade", "source": "self-produced (animated outro)",
                "license": "N/A", "provider": "self-produced",
                "rights_verified": True}
    else:
        tail = {"type": "endcard", "duration": END_CARD, "transition": "fade",
                "handle": args.handle, "bg_color": "0x0d1117",
                "accent_color": args.accent, "source": "self-produced",
                "license": "N/A", "provider": "self-produced",
                "rights_verified": True}
    scenes.append(tail)

    total_video = sum(s["duration"] for s in scenes) - XFADE * (len(scenes) - 1)

    # ---- music bed (audible, auto-length, gradual fade to the end) ----
    print("[5/7] music bed", file=sys.stderr)
    bed_path = str(work / "bed.m4a")
    run([PY, str(HERE / "make_music_bed.py"), "--duration",
         f"{total_video:.2f}", "--out", bed_path, "--key", args.music_key,
         "--mood", args.music_mood, "--fadeout", f"{args.music_fadeout}"],
        capture=True)

    # ---- assemble production.json ----
    audio = {
        "voice": voice_path,
        "bgm": bed_path,
        "bgm_source": "self-produced (synthesized ambient bed)",
        "bgm_license": "N/A (generated, no third-party rights)",
        "tts_provider": args.provider,
        "tts_voice": args.voice or "(provider default)",
    }
    if has_timing:
        audio["timing"] = timing_path            # enables karaoke subtitles
    prod = {
        "headline": args.headline,
        "handle": args.handle,
        "story": args.story,
        "voiceover_script": args.script,
        "output_path": str(out / "final.mp4"),
        "width": args.width,
        "height": args.height,
        "preset": args.preset,
        "karaoke": bool(args.karaoke),
        "scenes": scenes,
        "chyron": {"headline": args.headline, "accent_color": args.accent},
        "audio": audio,
        "subtitle_file": srt_path,
        "burn_credits": (not args.no_credits)
        and brand.get("burn_credits", True),
    }
    if have_logo:
        prod["chyron"]["logo"] = args.logo
    prod_path = str(work / "production.json")
    Path(prod_path).write_text(json.dumps(prod, indent=2))

    # ---- 7. render ----
    print("[6/7] render", file=sys.stderr)
    run([PY, str(HERE / "render_news_video.py"), prod_path])

    # ---- 8. deliverables ----
    print("[7/7] deliverables", file=sys.stderr)
    (out / "headline.txt").write_text(args.headline + "\n")
    (out / "voiceover.txt").write_text(args.script + "\n")
    shutil.copy(srt_path, out / "subs.srt")
    shutil.copy(prod_path, out / "production.json")
    sources_rc = run([PY, str(HERE / "make_sources.py"), prod_path,
                      "--out", str(out / "SOURCES.md")], capture=True)

    print("\n=== DONE ===", file=sys.stderr)
    print(f"video      : {out/'final.mp4'}")
    print(f"headline   : {out/'headline.txt'}")
    print(f"voiceover  : {out/'voiceover.txt'}")
    print(f"subtitles  : {out/'subs.srt'}")
    print(f"production : {out/'production.json'}")
    print(f"sources    : {out/'SOURCES.md'}")
    if sources_rc.returncode == 2:
        print("\n⚠️  Some assets are UNVERIFIED — see the warning atop "
              "SOURCES.md before publishing.")


if __name__ == "__main__":
    main()
