#!/usr/bin/env python3
"""
make_sources.py — write SOURCES.md (licensing manifest) from production.json.

Lists every visual + the music bed with source URL, license, and required
attribution. If any asset has rights_verified == false, a prominent warning is
placed at the TOP of the file and the exit code is 2 so a caller can surface it.

Usage:
    python3 make_sources.py production.json --out SOURCES.md
"""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("production")
    ap.add_argument("--out", default="SOURCES.md")
    args = ap.parse_args()

    prod = json.loads(Path(args.production).read_text())
    scenes = prod.get("scenes", [])
    audio = prod.get("audio", {})

    unverified = [s for s in scenes
                  if s.get("type") in ("image", "video")
                  and not s.get("rights_verified", False)
                  and s.get("provider", "").lower() != "self-produced"]

    lines = ["# Sources & Licensing", ""]
    lines.append(f"Headline: **{prod.get('headline','')}**")
    lines.append(f"Handle: {prod.get('handle','')}")
    lines.append("")

    if unverified:
        lines += [
            "> ⚠️ **RIGHTS WARNING — DO NOT PUBLISH AS-IS.**",
            "> The following assets could not be verified as freely reusable. "
            "Confirm rights or replace them before publishing:",
        ]
        for s in unverified:
            lines.append(f"> - `{Path(s.get('path','')).name}` "
                         f"({s.get('source','unknown source')})")
        lines.append("")

    lines.append("## Visual assets")
    lines.append("")
    lines.append("| # | File | Provider | License | Attribution | Source | Verified |")
    lines.append("|---|------|----------|---------|-------------|--------|----------|")
    n = 0
    for s in scenes:
        if s.get("type") not in ("image", "video"):
            continue
        n += 1
        lines.append(
            f"| {n} | {Path(s.get('path','')).name} "
            f"| {s.get('provider','—')} "
            f"| {s.get('license','—')} "
            f"| {s.get('attribution','—')} "
            f"| {s.get('source','—')} "
            f"| {'✅' if s.get('rights_verified') else '⚠️ NO'} |"
        )
    if n == 0:
        lines.append("| — | (no external visual assets; all self-produced) | | | | | |")

    lines.append("")
    lines.append("## Music")
    lines.append("")
    bgm_src = audio.get("bgm_source", "—")
    bgm_lic = audio.get("bgm_license", "—")
    lines.append(f"- Bed: `{Path(audio.get('bgm','')).name or '—'}`")
    lines.append(f"- Source: {bgm_src}")
    lines.append(f"- License: {bgm_lic}")

    lines.append("")
    lines.append("## Narration")
    lines.append(f"- Provider: {audio.get('tts_provider','—')}")
    lines.append(f"- Voice: {audio.get('tts_voice','—')}")
    lines.append("- Script is delivered separately as `voiceover.txt`.")
    lines.append("")

    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"[sources] wrote {args.out} "
          f"({n} visual assets, {len(unverified)} unverified)")
    if unverified:
        sys.exit(2)


if __name__ == "__main__":
    main()
