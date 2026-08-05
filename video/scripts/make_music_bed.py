#!/usr/bin/env python3
"""
make_music_bed.py — synthesize a subtle, self-produced news music bed.

This generates an ambient pad chord with ffmpeg. Because we generate it from
scratch, it carries NO third-party licensing risk — record it as
"self-produced (synthesized)" in SOURCES.md. Understated by design; the
renderer ducks it under the voice anyway.

Usage:
    python3 make_music_bed.py --duration 26 --out work/bed.m4a [--key a|c|d]
                              [--mood calm|tense]
"""
import argparse
import subprocess
from pathlib import Path

# root chords (Hz): a gentle major triad + octave root for body
CHORDS = {
    "a": [110.00, 164.81, 220.00, 277.18],   # A major-ish
    "c": [130.81, 196.00, 261.63, 329.63],   # C major
    "d": [146.83, 220.00, 293.66, 369.99],   # D major
}


def build_cmd(duration: float, out: str, key: str, mood: str,
              target: float = -20.0, fadeout: float = 2.5):
    freqs = CHORDS.get(key, CHORDS["a"])
    # each partial as a quiet sine; tense mood adds a low, slightly detuned drone
    gains = [0.10, 0.08, 0.07, 0.05]
    srcs, labels = [], []
    for i, (f, g) in enumerate(zip(freqs, gains)):
        srcs.append(
            f"sine=frequency={f}:sample_rate=44100:duration={duration+2},"
            f"volume={g}[p{i}]"
        )
        labels.append(f"[p{i}]")
    if mood == "tense":
        srcs.append(
            f"sine=frequency={freqs[0]*0.99:.2f}:sample_rate=44100:"
            f"duration={duration+2},volume=0.06[pd]"
        )
        labels.append("[pd]")

    mix = (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0[mix]")
    # tremolo (gentle movement) + lowpass (remove harshness) + soft echo (space)
    # loudnorm brings the bed to an AUDIBLE, consistent level; the fades (applied
    # after normalization so they aren't undone) ease it in and gradually dim it
    # out so the video ends cleanly.
    fo = max(fadeout, 0.5)
    shape = (
        "[mix]tremolo=f=0.15:d=0.3,"
        "lowpass=f=2800,"
        "aecho=0.8:0.9:60:0.25,"
        f"loudnorm=I={target}:TP=-2:LRA=11,"
        f"afade=t=in:st=0:d=1.2,"
        f"afade=t=out:st={max(duration - fo, 0):.2f}:d={fo:.2f},"
        f"atrim=0:{duration},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[bed]"
    )
    fc = ";".join(srcs + [mix, shape])
    return ["ffmpeg", "-y", "-filter_complex", fc, "-map", "[bed]",
            "-c:a", "aac", "-b:a", "160k", out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--key", default="a", choices=list(CHORDS))
    ap.add_argument("--mood", default="calm", choices=["calm", "tense"])
    ap.add_argument("--target", type=float, default=-20.0,
                    help="integrated loudness (LUFS) for the bed")
    ap.add_argument("--fadeout", type=float, default=2.5,
                    help="seconds of gradual dim at the end")
    args = ap.parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(args.duration, args.out, args.key, args.mood,
                    args.target, args.fadeout)
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[music] wrote self-produced bed {args.out} ({args.duration:.1f}s, "
          f"key={args.key}, mood={args.mood}, {args.target} LUFS, "
          f"fadeout={args.fadeout}s)")


if __name__ == "__main__":
    main()
