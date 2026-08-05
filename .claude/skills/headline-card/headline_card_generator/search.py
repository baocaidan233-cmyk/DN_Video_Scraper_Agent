"""Free person-photo search, no API key needed.

Uses DuckDuckGo's own image-search flow: fetch the HTML search page to pull
out a `vqd` token, then call the JSON results endpoint with it. This is an
internal DuckDuckGo endpoint, not a published/supported API — it could
change or stop working without notice. If it breaks, fall back to your own
stock-photo source, or a plain card with no photo (see card.make_plain_card).
"""

from __future__ import annotations

import re

import httpx

_VQD_RE = re.compile(r'vqd="?([\d-]+)"?')
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def search_person_photo(client: httpx.AsyncClient, name: str) -> str | None:
    """First DuckDuckGo image-search hit for `name`, or None if the name is
    empty / nothing comes back / the endpoint errors."""
    if not name:
        return None
    try:
        html_resp = await client.get(
            "https://duckduckgo.com/", params={"q": name, "iax": "images", "ia": "images"},
            headers={"User-Agent": _USER_AGENT},
        )
        html_resp.raise_for_status()
        match = _VQD_RE.search(html_resp.text)
        if not match:
            return None
        vqd = match.group(1)

        json_resp = await client.get(
            "https://duckduckgo.com/i.js",
            params={"q": name, "o": "json", "vqd": vqd},
            headers={"User-Agent": _USER_AGENT},
        )
        json_resp.raise_for_status()
        for item in json_resp.json().get("results", []):
            image_url = item.get("image")
            if image_url:
                return image_url
        return None
    except Exception:
        return None


async def download_image(
    client: httpx.AsyncClient, url: str, max_bytes: int = 15 * 1024 * 1024, referer: str = ""
) -> bytes | None:
    """Downloads `url`, returns None on any failure or if it exceeds
    max_bytes (some CDNs silently return a tiny placeholder image instead of
    a real 404 — checking size catches that case too, not just network
    errors)."""
    try:
        headers = {"User-Agent": _USER_AGENT}
        if referer:
            headers["Referer"] = referer
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        content = response.content
        if len(content) > max_bytes:
            return None
        return content
    except Exception:
        return None
