# Posting to Gettr.com — Technical Reference

How this codebase posts articles (text, image, video, or URL-preview) to Gettr.
Source of truth: `services/gettr_client.py`, `services/gcp_client.py`, `agents/publish_agent.py`.

## Auth

Every request carries an `x-app-auth` header (JSON string, not a bearer token):

```
x-app-auth: {"user": "<GETTR_USER_ID>", "token": "<GETTR_USER_TOKEN>"}
```

Media-upload calls (a separate host) instead use plain headers:
```
authorization: <GETTR_USER_TOKEN>
userid: <GETTR_USER_ID>
```

Credentials live in `GettrConfig` (`core/config.py`): `user_id`, `user_token`, `api_url` (default `https://gettr.com/api/u/post`).

## 1. Posting text-only (no media)

`POST {api_url}` — `multipart/form-data`, single field `content` whose value is a **JSON string** (not the multipart part being JSON-typed — it's literally a JSON-encoded string field):

```json
{
  "data": {
    "_t": "post",
    "acl": {"_t": "acl"},
    "txt": "post body text",
    "udate": 1751500000000,
    "cdate": 1751500000000,
    "uid": "<user_id>",
    "dsc": "OG description (optional)",
    "previmg": "https://.../preview.jpg (optional)",
    "prevsrc": "https://source-article-url (optional)",
    "ttl": "OG title (optional)"
  },
  "aux": null,
  "serial": "post"
}
```

Rules:
- `udate`/`cdate` = current time in **milliseconds**.
- Omit `dsc`/`previmg`/`prevsrc`/`ttl` entirely if empty/None — sending empty strings makes Gettr return an empty body and reject the post.
- This is Gettr's link-preview post type: given `prevsrc` (and optionally `previmg`/`ttl`/`dsc`), Gettr renders an OG-style preview card.

Implementation: `_build_post_without_media()` + `GettrClient.post_without_media()` in `services/gettr_client.py`.

## 2. Posting with media (image and/or video)

Media must be uploaded to Gettr's CDN **first**; the post payload then references the returned URLs. Two stages:

### 2a. Upload one media file (`services/gcp_client.py: GcpClient.upload_media`)

1. **Get upload channel**
   `GET https://upload.gettr.com/media/get_upload_channel?scene=getter`
   Headers: `filename`, `authorization`, `userid`, `user-agent`.
   Response: `{"gcs": {"url": "<gcp-init-url>"}, "notify_url": "..."}` (older docs call the key `gcp` instead of `gcs` — check both).

2. **Initiate a GCS resumable upload session**
   `POST <gcp-init-url>` with `x-goog-resumable: start`, `content-type: <mime>`, body `{"unuse": 0}`.
   Read the **`Location` response header** — that's the actual upload session URL.

3. **Upload the bytes**
   `PUT <location>` with `content-type: <mime>`, body = raw file bytes (streamed directly from the source URL to GCS, no local buffering).

4. **Notify Gettr the upload is done**
   `GET https://upload.gettr.com/<notify_url>?uploadedurl=<location-without-query>&result=ok`
   Headers: `authorization`, `userid`, `origin: https://gettr.com`.
   ⚠️ Must be **GET** — POST returns 403.
   Response is the media metadata object, e.g.:
   ```json
   {"ori": "https://cdn.gettr.com/.../orig.jpg", "screen": "https://cdn.gettr.com/.../thumb.jpg",
    "m3u8": "...", "nm_uri": "...", "width": 1280, "height": 720, "duration": 12.3}
   ```
   Treat `message == "ERR_UPLOAD_FAILURE"` (or missing `ori`/`screen` with nonzero `status`) as a failed upload.

MIME type is derived from the source URL's extension (`_MIME_MAP` in `gcp_client.py`); unknown extensions default to `image/jpeg` (never `application/octet-stream` — Gettr's CDN rejects that with `ERR_UPLOAD_FAILURE`).

### 2b. Build the post payload from uploaded media metadata

`_build_post_with_media()` in `services/gettr_client.py`. Given a list of uploaded-media dicts:

- First item with `m3u8` set, or `media_type == "video"`, is treated as **the** video (`video_el`); Gettr posts support at most one embedded video.
- All non-video items go into `imgs: [...]` (using `screen`, falling back to `ori`).
- Extra videos beyond the first are demoted to their thumbnail (`screen`/`ori`) and added to `imgs` too, rather than dropped.
- `main` = the primary media's `screen`/`ori` (video's if present, else `media[0]`'s).
- Video fields: `vid`/`pvid` = `m3u8` (HLS) → fall back to `nm_uri` → fall back to `ori`. `ovid`/`nmvid` = `nm_uri`. `vid_dur`/`vid_wid`/`vid_hgt` from the video metadata (or, for a single image, `im_width`/`im_height`).

Resulting payload shape:

```json
{
  "data": {
    "_t": "post",
    "acl": {"_t": "acl"},
    "txt": "post body text",
    "udate": 1751500000000,
    "cdate": 1751500000000,
    "uid": "<user_id>",
    "imgs": ["https://cdn.gettr.com/.../thumb.jpg"],
    "main": "https://cdn.gettr.com/.../thumb.jpg",
    "vid": null, "ovid": null, "pvid": null, "nmvid": null,
    "vid_dur": null, "vid_wid": null, "vid_hgt": null
  },
  "aux": null,
  "serial": "post"
}
```
(`None`-valued keys are dropped except `acl`/`imgs`, which are always kept — `imgs` may legitimately be `[]`.)

Then: `POST {api_url}` exactly as in the text-only case — same multipart `content` field, same `x-app-auth` header.

Implementation: `GettrClient.post_with_media(post_content, media_list)`.

## 3. The actual post request (shared by both cases)

```python
form = aiohttp.FormData()
form.add_field("content", json.dumps({"data": {...}, "aux": None, "serial": "post"}))

resp = await session.post(api_url, headers={"x-app-auth": auth_json}, data=form)
```

- Raise if the response body is empty (Gettr sometimes 200s with an empty body on rejected posts).
- Raise if `resp.status not in (200, 201)`.
- On success, the post ID is at `result["result"]["data"]["_id"]`.

## Which path this codebase picks

- **Article already has `media_urls`** → upload each via `GcpClient.upload_media()`, then `post_with_media()`.
  - Relative CDN paths (e.g. `group7/getter/...`, already hosted on Gettr) skip re-upload — used directly as `{"ori": url, "screen": url, "media_type": "image"}`.
- **No `media_urls`, DailyNews pipeline** → resolve an image URL (`url_to_image` or OG metadata), upload it, `post_with_media()` with a single image. **If no image can be found or all uploads fail, the article is dropped — there is no text-only DailyNews post.**
- **No `media_urls`, EpicFury pipeline** → `post_without_media()` with OG preview metadata (title/description/image/source link fetched via `_fetch_og_via_caps_gettr` / `_fetch_og_metadata`). Also used as EpicFury's fallback when all media uploads fail.

See `CLAUDE.md` → "PUBLISH ROUTING" for the exact dispatch table between `_publish_with_media` / `_publish_dailynews` / `_publish_without_media`.
