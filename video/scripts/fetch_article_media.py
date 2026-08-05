#!/usr/bin/env python3
"""
fetch_article_media.py — pull the photos that ship WITH a source news article.

Given the URL of a story reported by another outlet, this downloads the lead
image (og:image / twitter:image) and the in-article <img> photos, filters out
logos/icons/tracking pixels, and returns them ready to drop into
production.json as scenes.

IMPORTANT — rights: these are third-party news/agency photos. They are recorded
with license "Unverified (source article)" and rights_verified=false, which
forces a warning atop SOURCES.md. The operator must confirm rights (or fair-use
editorial basis) before publishing. This script never strips existing credits.

Usage:
    python3 fetch_article_media.py --url "https://outlet.com/story" \
        --n 6 --out work/assets/source [--manifest work/source_media.json]

Prints a JSON list of asset records (also written to --manifest).
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

import requests


def encode_url(u):
    """Percent-encode non-ASCII characters so requests/headers don't choke."""
    p = urlsplit(u)
    return urlunsplit((p.scheme, p.netloc, quote(p.path),
                       quote(p.query, safe="=&%+"), p.fragment))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# URL fragments that signal non-editorial images we should skip
SKIP = ("logo", "icon", "sprite", "avatar", "placeholder", "1x1", "pixel",
        "tracking", "/ads/", "ad_", "banner", "favicon", "spacer", "blank",
        "author", "byline", "emoji", "share", "social")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MIN_W = 500          # ignore anything narrower than this once downloaded
MIN_AREA = 500 * 350


class Extractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lead = []      # og:image / twitter:image (highest priority)
        self.imgs = []      # in-body <img> in document order

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "meta":
            key = (d.get("property") or d.get("name") or "").lower()
            if key in ("og:image", "og:image:url", "og:image:secure_url",
                       "twitter:image", "twitter:image:src") and d.get("content"):
                self.lead.append(d["content"])
        elif tag == "img":
            src = (d.get("src") or d.get("data-src") or d.get("data-original")
                   or d.get("data-lazy-src") or "")
            srcset = d.get("srcset") or d.get("data-srcset") or ""
            if srcset:
                # pick the largest candidate in the srcset
                cands = [c.strip().split(" ")[0] for c in srcset.split(",") if c.strip()]
                if cands:
                    src = cands[-1] or src
            if src:
                self.imgs.append(src)


def looks_bad(url):
    u = url.lower()
    return any(s in u for s in SKIP) or u.endswith(".svg") or u.startswith("data:")


def dims(path):
    import subprocess
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", path],
                       capture_output=True, text=True).stdout.strip()
    try:
        w, h = [int(x) for x in o.split(",")[:2]]
        return w, h
    except Exception:
        return 0, 0


def download(url, out_dir, stem, session):
    try:
        r = session.get(url, timeout=25, stream=True)
        r.raise_for_status()
    except Exception as e:
        print(f"[article] skip {url[:70]}: {e}", file=sys.stderr)
        return None
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext not in IMG_EXT:
        ct = r.headers.get("content-type", "")
        if "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        else:
            return None
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem)[:40] or "img"
    dest = out_dir / f"{safe}{ext}"
    n = 1
    while dest.exists():
        dest = out_dir / f"{safe}_{n}{ext}"
        n += 1
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return str(dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--out", default="work/assets/source")
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    page_url = encode_url(args.url)
    domain = urlsplit(page_url).netloc or "source article"

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": page_url})
    try:
        html = sess.get(page_url, timeout=25).text
    except Exception as e:
        print(f"[article] failed to fetch page: {e}", file=sys.stderr)
        print("[]")
        return

    ex = Extractor()
    ex.feed(html)

    # ordered, de-duplicated candidate list: lead image(s) first
    seen, cands = set(), []
    for u in ex.lead + ex.imgs:
        full = urljoin(page_url, u)
        if full in seen or looks_bad(full):
            continue
        seen.add(full)
        cands.append(full)

    # download and keep the ones big enough to be real photos
    downloaded = []
    for i, u in enumerate(cands):
        if len(downloaded) >= args.n:
            break
        p = download(u, out, f"source_{i:02d}", sess)
        if not p:
            continue
        w, h = dims(p)
        if w < MIN_W or w * h < MIN_AREA or not (0.4 <= (w / h if h else 0) <= 2.7):
            Path(p).unlink(missing_ok=True)
            continue
        downloaded.append((p, w * h, u))

    # lead image stays first; the rest sorted by resolution (desc)
    if downloaded:
        head, tail = downloaded[0], sorted(downloaded[1:], key=lambda x: -x[1])
        downloaded = [head] + tail

    assets = []
    for p, _area, u in downloaded:
        assets.append({
            "type": "image", "path": p,
            "source": args.url,
            "image_url": u,
            "license": "Unverified (source article)",
            "attribution": "",
            "provider": domain,
            "rights_verified": False,
        })

    manifest = args.manifest or str(out / "source_media.json")
    Path(manifest).write_text(json.dumps(assets, indent=2))
    print(json.dumps(assets, indent=2))
    print(f"[article] {len(assets)} source photo(s) from {domain} -> {manifest}",
          file=sys.stderr)
    if not assets:
        print("[article] WARNING: no usable photos extracted from the article "
              "(JS-heavy or blocked). Fall back to WebFetch or licensed stills.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
