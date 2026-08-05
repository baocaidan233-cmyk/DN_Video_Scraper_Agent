# Media Fetching Improvements (n8n Gap Fixes)

This document describes three gaps found between the Python agent and the original n8n
workflows, and exactly how they were fixed. Apply the same pattern to any agent that
publishes to Gettr with media uploads or OG-preview metadata.

---

## Gap 1 — Download Proxy Fallback (`gcp_client.py`)

**Problem:** When streaming a media URL directly to GCP fails (protected CDN, paywall,
bot challenge), the agent retried the same direct download up to 3 times. It always
failed for sources that require a server-side proxy to download.

**n8n behaviour:** `download_media` → on any error → `HTTP Request1` POSTs to
`http://n8n-svr.gettr.fyi:7771/api/v1/media/download` with `{"url": <media_url>}` and
`X-API-Key` header. The proxy fetches the file server-side and returns raw bytes, which
are then PUT to the GCP resumable upload session URL.

### Changes required

**`core/config.py` — `GcpConfig`:**
```python
download_proxy_url: str = "http://n8n-svr.gettr.fyi:7771/api/v1/media/download"
download_proxy_api_key: str = "<your-api-key>"
```

**`services/gcp_client.py` — add method to `GcpClient`:**
```python
async def _download_via_proxy(self, media_url: str) -> bytes:
    async with self._session.post(
        self._gcp.download_proxy_url,
        json={"url": media_url},
        headers={"X-API-Key": self._gcp.download_proxy_api_key or ""},
        timeout=aiohttp.ClientTimeout(total=self._gcp.download_timeout_s),
    ) as resp:
        resp.raise_for_status()
        return await resp.read()
```

**`services/gcp_client.py` — inside `upload_media`, replace the direct stream call:**
```python
# Before (direct only):
await self._stream_to_gcp(media_url, location, content_type, download_cookie)

# After (direct → proxy fallback):
try:
    await self._stream_to_gcp(media_url, location, content_type, download_cookie)
except Exception as direct_exc:
    if not self._gcp.download_proxy_url:
        raise
    logger.warning("Direct download failed for %s: %s — retrying via proxy", media_url[:80], direct_exc)
    data = await self._download_via_proxy(media_url)
    async with self._session.put(
        location,
        data=data,
        headers={"content-type": content_type},
        timeout=aiohttp.ClientTimeout(total=self._gcp.resumable_upload_timeout_s),
    ) as gcp_resp:
        gcp_resp.raise_for_status()
```

**Key detail:** The proxy fallback reuses the same GCP `location` URL from the session
initiated earlier. Do not re-initiate the GCP session — it stays valid. If the proxy
PUT also fails, the outer retry loop in `upload_media` catches it and tries the whole
flow again.

---

## Gap 2 — caps.gettr.com Scraping Proxy (`metadata_client.py`)

**Problem:** When fetching OG image metadata for "OTHERS" articles (everything except
YouTube and x.com/facebook.com), the agent fetched the article URL directly. Many news
sites serve bot challenges or Cloudflare pages to scrapers, returning no OG tags.

**n8n behaviour:** For "OTHERS" URLs, the workflow calls
`https://caps.gettr.com/<full_article_url>` with headers `origin: https://gettr.com` and
`referer: https://gettr.com/`. Gettr's own proxy fetches the page and returns real HTML,
bypassing many anti-bot gates. If `og:image` is empty after this, it falls through to
urlmeta.org → self-hosted fallback.

### Changes required

**`services/metadata_client.py` — add OG image regex and helper at module level:**
```python
import re

_OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]+(?:property|name)\s*=\s*["\']og:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']'
    r'|<meta\b[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*(?:property|name)\s*=\s*["\']og:image["\']',
    re.IGNORECASE | re.DOTALL,
)
_TW_IMAGE_RE = re.compile(
    r'<meta\b[^>]+(?:property|name)\s*=\s*["\']twitter:image(?::src)?["\'][^>]*content\s*=\s*["\']([^"\']+)["\']'
    r'|<meta\b[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*(?:property|name)\s*=\s*["\']twitter:image(?::src)?["\']',
    re.IGNORECASE | re.DOTALL,
)

def _og_image_from_html(html: str) -> Optional[str]:
    for pat in (_OG_IMAGE_RE, _TW_IMAGE_RE):
        m = pat.search(html)
        if m:
            return m.group(1) or m.group(2) or None
    return None
```

**`services/metadata_client.py` — add method to `MetadataClient`:**
```python
async def _fetch_caps_gettr(self, url: str) -> Optional[str]:
    try:
        caps_url = f"https://caps.gettr.com/{url}"
        async with self._session.get(
            caps_url,
            headers={"origin": "https://gettr.com", "referer": "https://gettr.com/"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            html = await resp.text(errors="replace")
        return _og_image_from_html(html)
    except Exception as e:
        logger.debug("MetadataClient: caps.gettr.com failed for %s: %s", url, e)
        return None
```

**`services/metadata_client.py` — update `fetch_image_url` routing for OTHERS:**
```python
# Before (for all non-YouTube, non-x.com):
return await self._fetch_urlmeta_with_fallback(url)

# After (caps.gettr.com first, urlmeta as fallback):
img = await self._fetch_caps_gettr(resolved_url)   # resolved_url = after URL resolver (Gap 6)
if img:
    return img
return await self._fetch_urlmeta_with_fallback(resolved_url)
```

**Key detail:** The caps.gettr.com URL format appends the full article URL directly:
`https://caps.gettr.com/https://www.reuters.com/...`. This is intentional. Do NOT
encode the article URL — pass it as a literal path segment.

Caps.gettr.com is NOT used for x.com or facebook.com URLs (these go directly to urlmeta).

---

## Gap 6 — Self-Hosted URL Resolver (`metadata_client.py`, `publish_agent.py`)

**Problem:** The agent resolved Google News URLs using base64 decode then HTTP redirect
follow. n8n uses a self-hosted service to resolve ALL non-YouTube URLs (not just Google
News), which handles redirect chains that base64 decode cannot (e.g. bitly, regional
news sites with redirects).

**n8n behaviour:** For all non-YouTube URLs with preview enabled, the workflow calls
`POST http://n8n-svr.gettr.fyi:7771/api/v1/url/final` with body `{"url": <url>}` and
`X-API-Key` header. The service returns `{"final_url": "..."}`. This runs before any
metadata fetch.

### Changes required

**`core/config.py` — `MetadataApiConfig`:**
```python
url_resolver_url: str = "http://n8n-svr.gettr.fyi:7771/api/v1/url/final"
```

**`services/metadata_client.py` — add method to `MetadataClient`:**
```python
async def _resolve_url(self, url: str) -> str:
    if not self._cfg.url_resolver_url:
        return url
    try:
        async with self._session.post(
            self._cfg.url_resolver_url,
            json={"url": url},
            headers={"X-API-Key": self._cfg.self_hosted_api_key or ""},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                resolved = data.get("final_url") or url
                if resolved != url:
                    logger.debug("MetadataClient: URL resolved %s → %s", url[:60], resolved[:60])
                return resolved
    except Exception as e:
        logger.debug("MetadataClient: URL resolver failed for %s: %s", url, e)
    return url
```

**`services/metadata_client.py` — call resolver in `fetch_image_url` before metadata:**
```python
# After blacklist and YouTube checks, before caps.gettr.com and urlmeta:
resolved = await self._resolve_url(url)
# Use resolved in all subsequent calls instead of url
```

**`agents/publish_agent.py` — upgrade `_resolve_google_news_url_async`:**

Add module-level constants:
```python
_SELF_HOSTED_RESOLVER_URL = "http://n8n-svr.gettr.fyi:7771/api/v1/url/final"
_SELF_HOSTED_API_KEY = "<your-api-key>"
```

Change function signature and logic — try self-hosted resolver FIRST, then fall back
to base64 decode, then HTTP redirect:
```python
async def _resolve_google_news_url_async(
    session, google_url, user_agent,
    resolver_url=_SELF_HOSTED_RESOLVER_URL,
    resolver_api_key=_SELF_HOSTED_API_KEY,
) -> str:
    # 1. Self-hosted resolver
    if resolver_url:
        try:
            async with session.post(resolver_url, json={"url": google_url},
                                    headers={"X-API-Key": resolver_api_key or ""},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    final = data.get("final_url") or ""
                    if final and "google.com" not in final:
                        return final
        except Exception as e:
            logger.debug("Self-hosted resolver failed for %s: %s", google_url, e)

    # 2. Base64 decode
    real = _resolve_google_news_url_sync(google_url)
    if real:
        return real

    # 3. HTTP redirect follow
    try:
        async with session.get(google_url, headers={"user-agent": user_agent},
                               timeout=aiohttp.ClientTimeout(total=10),
                               allow_redirects=True) as resp:
            final_url = str(resp.url)
            if "google.com" not in final_url and final_url != google_url:
                return final_url
    except Exception:
        pass

    return google_url
```

**Key detail:** Call sites do not need to change — the resolver URL and API key are
module-level defaults. If the resolver is down or unavailable, all three methods
gracefully return the original URL, so no article is dropped due to resolver failure.

---

## Self-Hosted Server Endpoints Summary

All three gaps use the same server (`n8n-svr.gettr.fyi:7771`):

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/url/final` | POST `{"url": ...}` | Resolve redirect chain → `{"final_url": ...}` |
| `/api/v1/media/download` | POST `{"url": ...}` | Proxy-download media → raw bytes |
| `/api/v1/website/metadata` | POST `{"url": ...}` | Scrape OG metadata → `{"image": ..., "title": ..., "description": ...}` |

All endpoints require `X-API-Key` header.
