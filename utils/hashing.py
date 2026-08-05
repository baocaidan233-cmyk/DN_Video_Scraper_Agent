"""
Hashing utilities faithful to the n8n workflow logic.

- sha256_url_hash: replicates link_url_hash node in resss_copy_v2.json
  SHA256 of URL with query params stripped, returns first 16 hex chars.

- sha1_post_hash: replicates Cal Post Hash node in v1.0_subflow_dailynews_to_gettr.json
  SHA1 of (post_content[:500] + first_media_url), returns first 12 hex chars.
"""

import hashlib
from urllib.parse import urlparse, urlunparse


def sha256_url_hash(url: str) -> str:
    """
    Strip query string and fragment from URL, then SHA256 hash it.
    Returns the first 16 hex characters (matching n8n link_url_hash node).
    """
    parsed = urlparse(url)
    clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return digest[:16]


def sha1_post_hash(post_content: str, first_media_url: str = "") -> str:
    """
    SHA1 of (post_content[:500] + first_media_url).
    Returns first 12 hex characters (matching n8n Cal Post Hash node).
    """
    combined = post_content[:500] + first_media_url
    digest = hashlib.sha1(combined.encode("utf-8")).hexdigest()
    return digest[:12]
