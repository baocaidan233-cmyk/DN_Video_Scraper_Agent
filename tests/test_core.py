"""
Tests for pure utility functions — no network, no Redis, no mocking needed.
Run: python3 -m pytest tests/ -v
"""

import base64
import hashlib
import re

import pytest

from utils.hashing import sha256_url_hash, sha1_post_hash
from utils.text_cleaner import clean_control_chars
from agents.publish_agent import _extract_first_url, _extract_og, _resolve_google_news_url_sync


# ---------------------------------------------------------------------------
# sha256_url_hash
# ---------------------------------------------------------------------------

class TestSha256UrlHash:
    def test_strips_query_string(self):
        assert sha256_url_hash("https://example.com/a?foo=1") == sha256_url_hash("https://example.com/a")

    def test_strips_fragment(self):
        assert sha256_url_hash("https://example.com/a#section") == sha256_url_hash("https://example.com/a")

    def test_strips_query_and_fragment(self):
        assert sha256_url_hash("https://example.com/a?x=1#y") == sha256_url_hash("https://example.com/a")

    def test_returns_16_chars(self):
        assert len(sha256_url_hash("https://example.com/article/123")) == 16

    def test_deterministic(self):
        url = "https://example.com/news/article"
        assert sha256_url_hash(url) == sha256_url_hash(url)

    def test_different_urls_differ(self):
        assert sha256_url_hash("https://example.com/a") != sha256_url_hash("https://example.com/b")


# ---------------------------------------------------------------------------
# sha1_post_hash
# ---------------------------------------------------------------------------

class TestSha1PostHash:
    def test_returns_12_chars(self):
        assert len(sha1_post_hash("hello world", "https://img.example.com/1.jpg")) == 12

    def test_content_truncated_at_500(self):
        long_content = "x" * 600
        assert sha1_post_hash(long_content, "") == sha1_post_hash("x" * 500, "")

    def test_content_at_boundary(self):
        at_500 = "x" * 500
        beyond_500 = "x" * 500 + "extra content beyond boundary"
        assert sha1_post_hash(at_500, "") == sha1_post_hash(beyond_500, "")

    def test_empty_inputs_dont_raise(self):
        result = sha1_post_hash("", "")
        assert len(result) == 12

    def test_deterministic(self):
        assert sha1_post_hash("some post", "https://img.example.com/x.jpg") == \
               sha1_post_hash("some post", "https://img.example.com/x.jpg")

    def test_media_url_included_in_hash(self):
        assert sha1_post_hash("same post", "https://img1.example.com/a.jpg") != \
               sha1_post_hash("same post", "https://img2.example.com/b.jpg")


# ---------------------------------------------------------------------------
# clean_control_chars
# ---------------------------------------------------------------------------

class TestCleanControlChars:
    def test_removes_null_byte(self):
        assert "\x00" not in clean_control_chars("hello\x00world")

    def test_preserves_newline(self):
        assert clean_control_chars("line1\nline2") == "line1\nline2"

    def test_preserves_tab(self):
        assert clean_control_chars("col1\tcol2") == "col1\tcol2"

    def test_preserves_carriage_return(self):
        assert clean_control_chars("text\rmore") == "text\rmore"

    def test_preserves_emoji(self):
        assert clean_control_chars("🔥 Breaking news") == "🔥 Breaking news"

    def test_none_returns_empty_string(self):
        assert clean_control_chars(None) == ""

    def test_empty_string_returns_empty(self):
        assert clean_control_chars("") == ""

    def test_removes_other_control_chars(self):
        result = clean_control_chars("a\x01b\x0Bc\x1Fd")
        assert result == "abcd"


# ---------------------------------------------------------------------------
# _extract_first_url
# ---------------------------------------------------------------------------

class TestExtractFirstUrl:
    def test_url_in_middle_of_text(self):
        text = "Check this out https://example.com/article and more"
        assert _extract_first_url(text) == "https://example.com/article"

    def test_no_url_returns_none(self):
        assert _extract_first_url("No URLs here, just plain text.") is None

    def test_multiple_urls_returns_first(self):
        text = "First https://first.com/a then https://second.com/b"
        assert _extract_first_url(text) == "https://first.com/a"

    def test_url_at_start(self):
        result = _extract_first_url("https://example.com/news is breaking")
        assert result == "https://example.com/news"

    def test_empty_string_returns_none(self):
        assert _extract_first_url("") is None


# ---------------------------------------------------------------------------
# _extract_og
# ---------------------------------------------------------------------------

class TestExtractOg:
    def test_standard_attribute_order(self):
        html = '<meta property="og:title" content="My Article Title">'
        assert _extract_og(html, "og:title") == "My Article Title"

    def test_reversed_attribute_order(self):
        html = '<meta content="My Article Title" property="og:title">'
        assert _extract_og(html, "og:title") == "My Article Title"

    def test_name_variant_twitter_title(self):
        html = '<meta name="twitter:title" content="Twitter Title">'
        assert _extract_og(html, "og:title", name_variants=("twitter:title",)) == "Twitter Title"

    def test_og_title_preferred_over_name_variant(self):
        html = '''
            <meta property="og:title" content="OG Title">
            <meta name="twitter:title" content="Twitter Title">
        '''
        assert _extract_og(html, "og:title", name_variants=("twitter:title",)) == "OG Title"

    def test_not_found_returns_none(self):
        html = '<meta property="og:description" content="Some desc">'
        assert _extract_og(html, "og:title") is None

    def test_empty_html_returns_none(self):
        assert _extract_og("", "og:title") is None


# ---------------------------------------------------------------------------
# _resolve_google_news_url_sync
# ---------------------------------------------------------------------------

class TestResolveGoogleNewsUrlSync:
    def _make_google_news_url(self, real_url: str) -> str:
        encoded = base64.urlsafe_b64encode(real_url.encode()).rstrip(b"=").decode()
        return f"https://news.google.com/articles/{encoded}"

    def test_non_google_news_url_returns_none(self):
        assert _resolve_google_news_url_sync("https://example.com/article") is None

    def test_google_news_without_articles_path_returns_none(self):
        assert _resolve_google_news_url_sync("https://news.google.com/topstories") is None

    def test_decodes_real_url(self):
        real_url = "https://reuters.com/world/article-123"
        google_url = self._make_google_news_url(real_url)
        result = _resolve_google_news_url_sync(google_url)
        assert result == real_url

    def test_does_not_return_google_url(self):
        # If decoded URL still points to google.com, return None
        google_url = self._make_google_news_url("https://google.com/something")
        assert _resolve_google_news_url_sync(google_url) is None

    def test_invalid_base64_returns_none(self):
        assert _resolve_google_news_url_sync("https://news.google.com/articles/!!!invalid!!!") is None
