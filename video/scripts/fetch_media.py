#!/usr/bin/env python3
"""
fetch_media.py — find and download open-license images for a story.

Sources (both expose machine-readable license data):
  * Wikimedia Commons  — filtered to PD / CC0 / CC BY / CC BY-SA
  * Openverse          — returns license + attribution per result

For every downloaded asset it records source URL, license, and attribution.
Anything whose license can't be verified as reusable is DISCARDED, not kept
with a warning. Nothing here scrapes arbitrary news photos.

Usage:
    python3 fetch_media.py --query "flooding Jakarta" --n 4 --out work/assets \
        [--source wikimedia|openverse|auto] [--manifest work/media.json]

Writes the downloaded files to --out and prints a JSON list of asset records
(also written to --manifest) that can be dropped into production.json scenes.
"""
import argparse
import json
import mimetypes
import re
import sys
from pathlib import Path

import requests

UA = ("news-motion-video-skill/1.0 (https://example.org; contact: "
      "short@gettr.com) python-requests")

# licenses we accept as freely reusable (lowercased substrings)
FREE_LICENSES = ("cc0", "public domain", "pdm", "cc-by", "cc by", "cc-by-sa",
                 "cc by-sa")
# licenses we explicitly reject even if "cc"
REJECT = ("nc", "nd", "noncommercial", "no derivatives")

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_free(license_str: str) -> bool:
    l = (license_str or "").lower()
    if any(bad in l for bad in REJECT):
        return False
    return any(good in l for good in FREE_LICENSES)


def download(url: str, dest_dir: Path, stem: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25,
                         stream=True)
        r.raise_for_status()
    except Exception as e:
        print(f"[media] download failed {url}: {e}", file=sys.stderr)
        return None
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext not in IMG_EXT:
        ct = r.headers.get("content-type", "")
        ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".jpg"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem)[:60] or "asset"
    dest = dest_dir / f"{safe}{ext}"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{safe}_{n}{ext}"
        n += 1
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return str(dest)


def fetch_wikimedia(query: str, n: int, out: Path):
    """Search Commons files and pull imageinfo with license extmetadata."""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": "6",
        "gsrlimit": str(n * 3), "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime|size",
        "iiurlwidth": "1920",
    }
    try:
        r = requests.get(api, params=params, headers={"User-Agent": UA},
                         timeout=25)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
    except Exception as e:
        print(f"[media] wikimedia query failed: {e}", file=sys.stderr)
        return []

    results = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = (meta.get("LicenseShortName", {}).get("value")
                        or meta.get("License", {}).get("value") or "")
        mime = info.get("mime", "")
        if not mime.startswith("image/"):
            continue
        if not is_free(license_name):
            continue
        artist = meta.get("Artist", {}).get("value", "")
        artist = re.sub(r"<[^>]+>", "", artist).strip()
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        results.append({
            "title": page.get("title", ""),
            "url": url,
            "descriptionurl": info.get("descriptionurl", ""),
            "license": license_name,
            "attribution": artist or "See Wikimedia Commons page",
            "provider": "Wikimedia Commons",
        })
        if len(results) >= n:
            break
    return results


def fetch_openverse(query: str, n: int, out: Path):
    api = "https://api.openverse.org/v1/images/"
    params = {"q": query, "page_size": str(n * 2),
              "license_type": "commercial,modification"}
    try:
        r = requests.get(api, params=params, headers={"User-Agent": UA},
                         timeout=25)
        r.raise_for_status()
        items = r.json().get("results", [])
    except Exception as e:
        print(f"[media] openverse query failed: {e}", file=sys.stderr)
        return []
    results = []
    for it in items:
        lic = f"{it.get('license','')} {it.get('license_version','')}".strip()
        if not is_free(lic):
            continue
        url = it.get("url")
        if not url:
            continue
        results.append({
            "title": it.get("title", ""),
            "url": url,
            "descriptionurl": it.get("foreign_landing_url", ""),
            "license": lic.upper(),
            "attribution": it.get("attribution")
            or it.get("creator", "") or "See source page",
            "provider": it.get("source", "Openverse"),
        })
        if len(results) >= n:
            break
    return results


def search_all(queries, n, source, per_query_cap):
    """Search each query in turn, deduped, until `n` assets are found.

    LOCAL PATCH (dailynews-agent): Commons ANDs every term against file
    metadata, so one conceptual phrase ("Vatican Beijing Catholic bishops
    China") reliably matches NOTHING while the individual entities in it
    ("Pope Francis", "Vatican City") each match plenty. Searching a list of
    concrete subjects instead of one phrase is the difference between a video
    of real photographs and a video of five plain text cards.

    `per_query_cap` also buys visual variety: better one photo of each of five
    subjects than five near-identical shots of the first one.
    """
    found, seen = [], set()
    for q in queries:
        if len(found) >= n:
            break
        want = min(per_query_cap, n - len(found))
        hits = []
        if source in ("wikimedia", "auto"):
            hits = fetch_wikimedia(q, want, None)
        if len(hits) < want and source in ("openverse", "auto"):
            hits += fetch_openverse(q, want - len(hits), None)
        kept = 0
        for h in hits:
            key = h.get("descriptionurl") or h["url"]
            if key in seen:
                continue
            seen.add(key)
            found.append(h)
            kept += 1
            if kept >= want:
                break
        print(f"[media] {q!r}: {kept} new", file=sys.stderr)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, action="append",
                    help="repeatable; each is searched separately")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--per-query", type=int, default=2,
                    help="max assets to take from any single query")
    ap.add_argument("--out", default="work/assets")
    ap.add_argument("--source", choices=["wikimedia", "openverse", "auto"],
                    default="auto")
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    found = search_all(args.query, args.n, args.source, args.per_query)

    assets = []
    for f in found:
        path = download(f["url"], out, f["title"] or args.query[0])
        if not path:
            continue
        assets.append({
            "type": "image",
            "path": path,
            "source": f.get("descriptionurl") or f["url"],
            "license": f["license"],
            "attribution": f["attribution"],
            "provider": f["provider"],
            "rights_verified": True,
        })

    manifest = args.manifest or str(out / "media.json")
    Path(manifest).write_text(json.dumps(assets, indent=2))
    print(json.dumps(assets, indent=2))
    print(f"[media] {len(assets)} verified assets -> {manifest}",
          file=sys.stderr)
    if not assets:
        print("[media] WARNING: no verified open-license media found for "
              "this query. Fall back to self-generated graphics.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
